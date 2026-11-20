"""喂食：把「吃什么」插件的推荐 / 点菜接成一次好感加成。

三件事，都很轻：

1. 收：「吃什么」在出菜之前往 event 上塞一条 extra，这里在
   ``after_message_sent`` 里读出来，记一次投喂、加一点好感。
2. 贴：那条回复还没发出去时（``on_decorating_result``），先把「好感 +N」
   拼到它的文本末尾，和食物图片同一条消息出去——等发完再改就来不及了。
3. 存：次数和最近一道菜写在 ``bonds.json`` 这个人记录的 ``fed`` 字段里，
   不新开文件——投喂本来就是关系的一部分。

**为什么能跨插件传值**：AstrBot 把同一条消息分发给所有插件时传的是同一个
``event`` 对象（``pipeline/process_stage/method/star_request.py`` 里
``call_handler(event, handler.handler)``），而 ``set_extra`` / ``get_extra``
就是这个对象上的一个字典。所以「吃什么」写、这里读，不需要网络也不需要 import。

**没装「吃什么」也不会报错**：extra 就是个没人写的键，读出来是 None，直接返回。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from astrbot.api import logger

from .bond import bond_key

# 「吃什么」插件往这个 key 里写：{"dish": "菜名", "mode": "order" | "random"}
# 约定写在两个插件的 README 里，改名字要两边一起改
FEED_EXTRA_KEY = "qunliaojiyi_feed"

# 已经结算过的标记。发消息前（decorating）要算一次以便把「好感 +N」
# 贴进同一条回复，发完之后（after_message_sent）还要兜底一次，
# 同一个 event 上不能记两遍账。
FEED_DONE_KEY = "_qunliao_feed_done"

BEIJING_TZ = timezone(timedelta(hours=8))


def _today(now: float | None = None) -> str:
    at = datetime.fromtimestamp(float(now if now is not None else time.time()), BEIJING_TZ)
    return at.strftime("%Y-%m-%d")


def today_str() -> str:
    """今天的日期键。面板和记录两侧必须同口径，所以只留这一个入口。"""
    return _today()


def build_note(gain: float, reason: str = "") -> str:
    """拼在「吃什么」那条回复末尾的那句话。

    三种情况：

    - 加了分：写「投喂好感度 +2」。写全「投喂好感度」而不是「好感」，
      群聊里冷不丁冒出一句「好感 +2」，看的人不知道是什么好感。
    - 撞了每日次数：写「今日投喂已到上限」。什么都不说会被当成加上了，
      用户第二天发现好感没动才来问；直接说明白，也省得他去翻面板。
    - 其他（加成设成 0、被单日净增上限吃掉）：不写。这类是配置或额度问题，
      在群里解释一通反而吵。

    数字用 ``:g``：2.0 写成「2」，5.5 保留成「5.5」。
    """
    if reason == "limit":
        return "（今日投喂已到上限）"
    if reason:
        return ""
    try:
        value = float(gain)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return f"（投喂好感度 +{value:g}）"


def settle(plugin, event) -> dict[str, Any] | None:
    """算一次投喂并落账。

    返回 None 表示这条消息跟投喂无关（没有标记 / 没开喂食 / 加成设成 0）。
    返回 dict 表示「这条已经处理过了」，字段含义：

    - ``reason`` 为空：真的加了分，``gain`` 是实际加进去的数字
    - ``reason == "limit"``：今天次数用完，没加分
    - ``reason == "cap"``：被单日净增上限吃掉，没加分

    后两种也算处理完，会打标记——调用方据此决定要不要在回复里说明。

    调用方有两处：发消息前的 decorating（要拿这个结果写进回复）
    和发完之后的 after_message_sent（兜底）。标记存在 event 上，
    所以只有第一次调用真的会算。
    """
    try:
        if event.get_extra(FEED_DONE_KEY):
            return None
        if not plugin.settings.get("feed_enabled"):
            return None
        payload = event.get_extra(FEED_EXTRA_KEY)
        if not isinstance(payload, dict):
            return None

        sender_id = str(event.get_sender_id() or "")
        if not sender_id or sender_id == str(event.get_self_id() or ""):
            return None

        mode = str(payload.get("mode") or "random").strip().lower()
        dish = str(payload.get("dish") or "").strip()[:24]
        # 只有明确点菜走 order 的加成，其余（含拼错的 mode）一律按随机推荐算，
        # 免得某个插件写错一个字段就白送高分
        is_order = mode == "order"
        gain_cfg = "feed_gain_order" if is_order else "feed_gain_random"
        try:
            gain = float(plugin.settings.get(gain_cfg) or 0.0)
        except (TypeError, ValueError):
            gain = 0.0
        if gain <= 0:
            return None

        key = bond_key(sender_id)
        record = dict(plugin.storage.bond_of(key) or {})
        fed = dict(record.get("fed") or {})
        today = _today()
        used = int(fed.get("today") or 0) if fed.get("day") == today else 0

        limit = int(plugin.settings.get("feed_daily_limit") or 0)
        if limit > 0 and used >= limit:
            # 这条日志单独立出来：面板上「点菜没加成」时，一眼要能看出
            # 是撞了每日次数，而不是加成算错或者功能没生效
            logger.info(
                f"[群聊记忆] 投喂未计入 · {sender_id} · {dish or '未记菜名'} · "
                f"今天已经喂过 {used} 次（上限 {limit}）"
            )
            # 标记照打：这条消息的投喂已经处理完了，结论是「不加」，
            # 免得 after_message_sent 再算一遍、再打一条一样的日志
            event.set_extra(FEED_DONE_KEY, True)
            return {"gain": 0.0, "dish": dish, "order": is_order, "reason": "limit"}

        given = plugin.bond.add_bonus(sender_id, gain)
        if given <= 0:
            # 额度被每日净增上限吃掉了（一天已经涨满 80 点）。
            # 不计数，免得「今天喂过了」把明天的额度也占掉。
            # 这种情况不在群里解释——说「今天涨满了」比不说更让人困惑
            event.set_extra(FEED_DONE_KEY, True)
            return {"gain": 0.0, "dish": dish, "order": is_order, "reason": "cap"}

        # add_bonus 已经写过一次盘，这里重新读一遍再补 fed 字段，
        # 避免拿旧 record 覆盖掉刚加的分
        record = dict(plugin.storage.bond_of(key) or {})
        fed = dict(record.get("fed") or {})
        fed.update(
            {
                "day": today,
                "today": used + 1,
                "total": int(fed.get("total") or 0) + 1,
                "last_at": round(time.time(), 1),
                "last_dish": dish,
                "last_mode": "order" if is_order else "random",
            }
        )
        record["fed"] = fed
        plugin.storage.set_bond(key, record)
        event.set_extra(FEED_DONE_KEY, True)

        logger.info(
            f"[群聊记忆] 投喂 · {sender_id} · {dish or '未记菜名'} · "
            f"+{given:g}（{'点菜' if is_order else '随机'}）"
        )
        return {
            "gain": round(float(given), 2),
            "dish": dish,
            "order": is_order,
            "reason": "",
        }
    except Exception as e:  # noqa: BLE001 - 喂食失败不该影响回复
        logger.debug(f"[群聊记忆] 记录投喂失败（可忽略）: {e}")
        return None


async def record_feed(plugin, event) -> None:
    """``after_message_sent`` 里调用。

    正常链路下 decorating 已经结算过了，这里会因为标记而直接返回；
    某些平台不走 decorating 时，这一步负责把账补上（只是不能再往
    已经发出去的回复里贴提示了）。
    """
    settle(plugin, event)


def snapshot(plugin, sender_id: str) -> dict[str, Any]:
    """面板用的投喂快照。"""
    try:
        raw = plugin.storage.bond_of(bond_key(sender_id)) or {}
        record = plugin.bond.settle(dict(raw), time.time()) if raw else {}
        fed = record.get("fed") or {}
        today = _today()
        used = int(fed.get("today") or 0) if fed.get("day") == today else 0
        limit = int(plugin.settings.get("feed_daily_limit") or 0)
        return {
            "enabled": bool(plugin.settings.get("feed_enabled")),
            "total": int(fed.get("total") or 0),
            "today": used,
            "limit": limit,
            "last_dish": str(fed.get("last_dish") or ""),
            "last_at": float(fed.get("last_at") or 0) or None,
        }
    except Exception:  # noqa: BLE001
        return {"enabled": False, "total": 0, "today": 0, "limit": 0, "last_dish": "", "last_at": None}

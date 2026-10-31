"""主动回复：判断「这一轮要不要自己开口」。

三条设计原则：

1. **只判断，不发送**。本模块返回「该说」以及要说的上下文，真正发请求、
   调 ``should_call_llm(False)`` 由 hooks.py 负责。这样判断逻辑可以单独测，
   也不用关心各平台发送 API 的差异。
2. **闸门按成本从低到高排列**。前面十几层全是本地判断（读配置、比时间戳、
   查内存里的状态），任何一层不过就直接返回，只有 llm_judge 模式才会发请求。
   群聊消息量很大，任何挂在消息链路上的网络请求都必须被前面的闸门挡到极低频。
3. **状态惰性维护**。按天重置、冷却推进都写在读状态的时候算，不挂定时器：
   AstrBot 插件被停用/重载时定时器要额外管理，而群聊本来就未必天天有消息，
   为空转的定时器付复杂度不划算。

主动回复默认关闭：插话的收益高度依赖群氛围，默认开着会让人以为插件坏了。
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from astrbot.api import logger

# 容器时区通常是 UTC，而「静默时段」「自然日」都是按用户作息定义的，
# 必须显式用北京时间，不能用 datetime.now() 的本地时区。
BEIJING_TZ = timezone(timedelta(hours=8))

# 群太冷 / 冷场的两个窗口。前者决定「值不值得插话」，后者是 rule 模式的触发条件。
COLD_WINDOW_SECONDS = 600
LULL_SECONDS = 300

# 退避最多把冷却放大到 4 倍：再大就等于永久闭嘴，用户会以为功能失效。
MAX_BACKOFF_FACTOR = 4
RECENT_MAX = 20

# llm_judge 跑在消息链路上，超时宁可判 SKIP 也不能拖住正常回复
JUDGE_TIMEOUT = 20.0
JUDGE_SYSTEM_PROMPT = "你是群聊里的普通成员。你只回答 REPLY 或 SKIP 这两个词，不输出任何其他内容。"

# rule 模式的提问句式：本地字符串匹配，成本为零。
# 只收「明显在向群里发问」的短句，别收「吗」这种到处都有的字，否则等于把规则模式变成常开。
QUESTION_HINTS = (
    "有没有人知道",
    "有没有懂",
    "求推荐",
    "怎么弄",
    "这是什么",
    "是不是",
    "谁能",
)

_MODE_ALIASES = {
    "probability": "probability",
    "rule": "rule",
    "llm_judge": "llm_judge",
    "judge": "llm_judge",
}


def _strict_reply(text: str) -> bool:
    """判定模型是否明确回答 REPLY。

    去标点后必须**整串等于** REPLY。用 startswith 会把「REPLY 不了，因为…」
    也当成命中，而模型一旦开始解释，本身就说明它在犹豫，这种情况应该判 SKIP。
    """
    cleaned = re.sub(r"[^0-9A-Za-z]+", "", text or "").upper()
    return cleaned == "REPLY"


def parse_quiet_hours(raw: str) -> tuple[int, int] | None:
    """把 ``"01:00-08:00"`` 解析成分钟数区间，失败返回 None（视为不静默）。

    支持跨天（``22:00-06:00``）：起止顺序决定是同一段还是跨午夜的两段。
    """
    text = str(raw or "").strip()
    if not text or "-" not in text:
        return None
    start_text, _, end_text = text.partition("-")
    try:
        start_h, start_m = (int(x) for x in start_text.strip().split(":"))
        end_h, end_m = (int(x) for x in end_text.strip().split(":"))
    except (TypeError, ValueError):
        return None
    if not (0 <= start_h <= 23 and 0 <= end_h <= 23):
        return None
    if not (0 <= start_m <= 59 and 0 <= end_m <= 59):
        return None
    return start_h * 60 + start_m, end_h * 60 + end_m


def in_quiet_hours(raw: str, now: datetime | None = None) -> bool:
    parsed = parse_quiet_hours(raw)
    if parsed is None:
        return False
    start, end = parsed
    if start == end:
        # 起止相同没有合理解释，按「不静默」处理，免得配错就整天空转
        return False
    current = now or datetime.now(BEIJING_TZ)
    minute = current.hour * 60 + current.minute
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


class ActiveReply:
    """主动回复判定器。一个插件实例持有一个，状态按会话分开存。"""

    def __init__(self, plugin, storage, settings) -> None:
        self.plugin = plugin
        self.storage = storage
        self.settings = settings

    # ── 对外入口 ────────────────────────────────────────────────────────

    async def maybe_active(self, event) -> dict | None:
        """判断这一轮要不要主动插话。

        返回 None 表示不插话；返回 dict 表示该说，形状::

            {"prompt": 最新一条消息, "conversation": 对话对象或 None,
             "mode": 模式, "reason": 命中原因}

        任何异常都吞掉并返回 None —— 主动回复只是锦上添花，
        它出问题绝不能影响用户正常触发的那一轮回复。
        """
        try:
            return await self._evaluate(event)
        except Exception as e:  # noqa: BLE001 - 判定失败一律当不插话
            logger.warning(f"[群聊记忆] 主动回复判断异常（已跳过）: {e}")
            return None

    def status(self, session_id: str) -> dict:
        """给面板和 ``/风格状态`` 用的状态快照。任何异常都返回保守的默认值。"""
        mode = self._mode()
        daily_limit = self._int_setting("active_reply_daily_limit")
        snapshot: dict[str, Any] = {
            "enabled": False,
            "mode": mode,
            "today_count": 0,
            "daily_limit": daily_limit,
            "next_roll_in": None,
            "no_reply_streak": 0,
            "last_reason": "",
            "last_at": None,
            "recent": [],
        }
        try:
            state = self._load_state(session_id)
            now = time.time()
            next_roll_at = float(state.get("next_roll_at") or 0.0)
            snapshot.update(
                {
                    "enabled": self._session_enabled(session_id, state),
                    "today_count": int(state.get("today_count") or 0),
                    "next_roll_in": (
                        max(0, int(next_roll_at - now)) if next_roll_at > now else None
                    ),
                    "no_reply_streak": int(state.get("no_reply_streak") or 0),
                    "last_reason": str(state.get("last_reason") or ""),
                    "last_at": float(state.get("last_at") or 0.0) or None,
                    "recent": list(state.get("recent") or [])[-RECENT_MAX:],
                }
            )
        except Exception as e:  # noqa: BLE001 - 面板读状态出错不该 500
            logger.warning(f"[群聊记忆] 读取主动回复状态失败: {e}")
        return snapshot

    def set_enabled(self, session_id: str, enabled: bool) -> dict:
        """开关某个群的主动回复。

        存在 active.json 的 ``enabled`` 字段里，而不是 overrides.json：
        单群开关和「今天说了几次、下次什么时候评估」是同一份状态，
        分两处存会出现一处改了另一处没跟上。闸门第 2 层优先读这个字段，
        没写过时才回落到 overrides 的 ``active`` 键。
        """
        try:
            state = self._load_state(session_id)
            state["enabled"] = bool(enabled)
            if enabled:
                # 重新打开时清掉冷却，否则用户点了「开启」还要干等半小时
                state["next_roll_at"] = 0.0
            self._save(session_id, state)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[群聊记忆] 切换主动回复开关失败: {e}")
        return self.status(session_id)

    # ── 闸门 ────────────────────────────────────────────────────────────

    async def _evaluate(self, event) -> dict | None:
        # 1. 总开关
        if not self.settings.get("active_reply_enabled"):
            return None

        session_id = str(getattr(event, "unified_msg_origin", "") or "")
        if not session_id:
            return None

        state = self._load_state(session_id)

        # 2. 会话开关（优先看 active.json 里的，其次看 overrides）
        if not self._session_enabled(session_id, state):
            return None

        # 3. 自己发的消息，包括上一轮自己插的话
        if self._is_self(event):
            return None

        text = ""
        try:
            text = (event.get_message_str() or "").strip()
        except Exception:  # noqa: BLE001 - 取不到文本就当作没有文本
            text = ""

        # 4. 指令消息交给命令处理，不参与插话
        if text.startswith("/"):
            return None
        # 5. 无文本（纯图片/表情/文件）
        if not text:
            return None
        # 6. 被 @ 或被唤醒的那一轮，交给正常回复链路
        if getattr(event, "is_at_or_wake_command", False):
            return None
        # 7. 本轮已经有结果了，再插一句就是重复回复
        if self._has_result(event):
            return None

        now = time.time()
        mode = self._mode()
        messages = self._messages(session_id)

        # llm_judge 的攒条数必须在冷却判断之前做：冷却期内往往一直有人说话，
        # 先判冷却再计数的话这批消息就白攒了，judge_stack 永远到不了。
        if mode == "llm_judge":
            state["judge_buffer"] = int(state.get("judge_buffer") or 0) + 1
            self._save(session_id, state)

        # 8. 静默时段
        if in_quiet_hours(str(self.settings.get("active_reply_quiet_hours") or "")):
            return None

        # 9. 群太冷：人太少的时候凑上去说话最像机器人
        min_messages = self._int_setting("active_reply_min_messages")
        if self._count_since(messages, now - COLD_WINDOW_SECONDS) < min_messages:
            return None

        # 10. 日限（today_count 已在 _load_state 里按自然日惰性重置）
        daily_limit = self._int_setting("active_reply_daily_limit")
        if daily_limit > 0 and int(state.get("today_count") or 0) >= daily_limit:
            return None

        # 11. 冷却。next_roll_at 是「下一次允许评估的时刻」，命中与否都会推进，
        #     所以骰子是每个冷却窗口掷一次，而不是每条消息掷一次——
        #     后者会让群越热闹越容易被打断，正好和「别烦人」的目标相反。
        next_roll_at = float(state.get("next_roll_at") or 0.0)
        if next_roll_at > now:
            return None

        # llm_judge 没攒够就不判，也不推进冷却，等下一条消息够了立刻判
        judge_stack = max(1, self._int_setting("active_reply_judge_stack"))
        if mode == "llm_judge" and int(state.get("judge_buffer") or 0) < judge_stack:
            return None

        streak = self._update_streak(state, messages, now)
        # 12. 退避：连续几次开口没人接，就把冷却放大，最多 4 倍
        state["next_roll_at"] = round(now + self._effective_cooldown(streak), 1)

        if mode == "probability":
            chance = self._clamp(float(self.settings.get("active_reply_chance") or 0.0), 0.0, 1.0)
            hit = random.random() < chance
            reason = "probability"
        elif mode == "llm_judge":
            hit, reason = await self._judge(session_id, state, messages)
        else:
            hit, reason = self._rule_hit(text, messages, now)

        state["recent"] = (
            list(state.get("recent") or [])
            + [
                {
                    "at": round(now, 1),
                    "mode": mode,
                    "hit": bool(hit),
                    "chars": len(text),
                }
            ]
        )[-RECENT_MAX:]
        state["last_reason"] = reason
        if hit:
            state["today_count"] = int(state.get("today_count") or 0) + 1
            state["last_at"] = round(now, 1)
        self._save(session_id, state)

        if not hit:
            return None
        if self.settings.is_debug():
            logger.info(f"[群聊记忆] 主动回复命中 · {session_id} · {mode}/{reason}")
        return {
            "prompt": text,
            "conversation": await self._get_conversation(session_id),
            "mode": mode,
            "reason": reason,
        }

    async def _get_conversation(self, session_id: str):
        """拿当前会话的 Conversation，让插话能接上之前聊的内容。

        ``AstrMessageEvent`` 上没有 ``conversation`` 属性（早期实现踩过这个坑），
        必须走 conversation_manager 两步查：先取当前对话 id，再取对话本体。
        拿不到就返回 None——不带上下文也能插话，只是模型不知道自己说过什么。
        """
        try:
            mgr = getattr(self.plugin.context, "conversation_manager", None)
            if mgr is None:
                return None
            cid = await mgr.get_curr_conversation_id(session_id)
            if not cid:
                return None
            return await mgr.get_conversation(session_id, cid)
        except Exception as e:  # noqa: BLE001 - 拿不到上下文不该阻止插话
            logger.debug(f"[群聊记忆] 获取会话上下文失败（忽略）: {e}")
            return None

    # ── rule 模式 ───────────────────────────────────────────────────────

    def _rule_hit(self, text: str, messages: list[dict], now: float) -> tuple[bool, str]:
        """纯本地规则，零成本。命中任一即算，但仍受冷却与日限约束。"""
        if any(hint in text for hint in QUESTION_HINTS):
            return True, "question"

        keywords = self.settings.get("active_reply_keywords") or []
        for keyword in keywords:
            word = str(keyword or "").strip()
            if word and word in text:
                return True, "keyword"

        # 冷场：这条消息之前已经安静了 5 分钟，说明是有人重新开了话题
        stamps = self._timestamps(messages)
        if len(stamps) >= 2 and now - stamps[-2] >= LULL_SECONDS:
            return True, "lull"
        return False, "rule"

    # ── llm_judge 模式 ──────────────────────────────────────────────────

    async def _judge(
        self, session_id: str, state: dict, messages: list[dict]
    ) -> tuple[bool, str]:
        """让模型判断「现在接一句话自不自然」。

        这条路径花钱，所以只在冷却窗口到点、且攒够 judge_stack 条消息时才走；
        任何失败（拿不到 provider、超时、输出不合格式）一律当 SKIP，
        并且不消耗日限 —— 判定失败说明不了「该说」，按不该说处理最保守。
        """
        state["judge_buffer"] = 0
        provider = await self._resolve_provider(session_id)
        if provider is None:
            return False, "judge_no_provider"

        prompt = self._build_judge_prompt(session_id, messages)
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt, contexts=[], system_prompt=JUDGE_SYSTEM_PROMPT
                ),
                timeout=JUDGE_TIMEOUT,
            )
        except Exception as e:  # noqa: BLE001 - 超时/网络错误都当 SKIP
            logger.debug(f"[群聊记忆] 主动回复判定失败（按跳过处理）: {e}")
            return False, "judge_error"

        self._record_judge_usage(getattr(resp, "usage", None), session_id)
        answer = str(getattr(resp, "completion_text", "") or "")
        if _strict_reply(answer):
            return True, "judge"
        return False, "judge_skip"

    def _build_judge_prompt(self, session_id: str, messages: list[dict]) -> str:
        lines: list[str] = []
        for msg in messages[-12:]:
            sender = str(msg.get("sender") or "?")[:24]
            content = str(msg.get("content") or "").replace("\n", " ")[:200]
            if content:
                lines.append(f"{sender}: {content}")
        history = "\n".join(lines) or "（暂无聊天记录）"
        style = self._style_hint(session_id)
        return (
            "你是群里的普通成员。下面是最近的聊天记录和本群的语言风格摘要。\n"
            f"{style}\n"
            "聊天记录：\n"
            f"{history}\n\n"
            "判断你现在接一句话是否自然。只回答 REPLY 或 SKIP，不要任何其他文字。"
        )

    def _style_hint(self, session_id: str) -> str:
        """风格摘要只取几条稳定风格，够模型判断语气就行，不要塞满。"""
        try:
            summary = self.plugin.manager.injector.summary(session_id)
            preview = [str(x) for x in (summary.get("stable_preview") or []) if x]
            if preview:
                return "本群语言风格：" + "、".join(preview[:5])
        except Exception:  # noqa: BLE001 - 摘要拿不到不影响判定
            pass
        return ""

    async def _resolve_provider(self, session_id: str):
        """和 learner 用同一套解析顺序：先看指定的 provider，再回落到当前会话默认。"""
        context = getattr(self.plugin, "context", None)
        if context is None:
            return None
        provider_id = str(self.settings.get("active_reply_provider_id") or "").strip()
        if provider_id:
            try:
                provider = context.get_provider_by_id(provider_id=provider_id)
                if provider is not None:
                    return provider
                logger.warning(f"[群聊记忆] 主动回复指定的 Provider 不存在: {provider_id}，回退默认")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[群聊记忆] 获取主动回复 Provider 失败: {e}")
        try:
            return context.get_using_provider(umo=session_id)
        except TypeError:
            # 老版本 AstrBot 的签名不带 umo
            try:
                return context.get_using_provider()
            except Exception:  # noqa: BLE001
                return None
        except Exception as e:  # noqa: BLE001
            logger.error(f"[群聊记忆] 获取默认 Provider 失败（主动回复）: {e}")
            return None

    def _record_judge_usage(self, usage: Any, session_id: str) -> None:
        """判定消耗也要记账，否则面板上的今日花费会对不上账。"""
        usage_store = getattr(self.plugin, "usage", None)
        record = getattr(usage_store, "record", None)
        if not callable(record):
            return
        try:
            result = record("judge", usage, session_id)
            # 兼容同步/异步两种实现：同步版直接忽略返回值
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)  # noqa: RUF006 - 丢在后台，失败了也没人管
        except Exception as e:  # noqa: BLE001 - 记账失败不能影响主流程
            logger.debug(f"[群聊记忆] 记录判定消耗失败（可忽略）: {e}")

    # ── 状态存取 ────────────────────────────────────────────────────────

    def _load_state(self, session_id: str) -> dict:
        """读状态并做惰性的按天重置。

        重置放在读的时候做，不靠定时器：插件被停用几天再启用时，
        定时器根本没跑过，而「今天说了几次」必须按真实的自然日算。
        """
        raw = self.storage.active_of(session_id)
        state = dict(raw) if isinstance(raw, dict) else {}
        today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
        if state.get("day") != today:
            state["day"] = today
            state["today_count"] = 0
            state["judge_buffer"] = 0
        state["today_count"] = int(state.get("today_count") or 0)
        state["no_reply_streak"] = int(state.get("no_reply_streak") or 0)
        state["judge_buffer"] = int(state.get("judge_buffer") or 0)
        if not isinstance(state.get("recent"), list):
            state["recent"] = []
        return state

    def _save(self, session_id: str, state: dict) -> None:
        state["recent"] = list(state.get("recent") or [])[-RECENT_MAX:]
        try:
            self.storage.save_active(session_id, state)
        except Exception as e:  # noqa: BLE001 - 存状态失败最多导致下次冷却算重
            logger.warning(f"[群聊记忆] 保存主动回复状态失败: {e}")

    def _session_enabled(self, session_id: str, state: dict) -> bool:
        """会话级开关。

        active.json 里的 ``enabled`` 是我们自己写的（``set_enabled``）；
        没写过时回落到 overrides 的 ``active`` 键，方便面板统一管理。
        """
        if "enabled" in state:
            return bool(state.get("enabled"))
        try:
            override = self.storage.session_override(session_id)
            return bool(override.get("active", True))
        except Exception:  # noqa: BLE001 - 读不到就按开启处理
            return True

    # ── 退避 / 冷却 ─────────────────────────────────────────────────────

    def _update_streak(self, state: dict, messages: list[dict], now: float) -> int:
        """更新「连续几次开口没人接」，返回本次生效的 streak。

        只数「上次主动发言之后、当前这条之前」的消息：当前这条消息正是触发
        本次评估的那一条，把它算成「对我们上次发言的回应」的话，
        streak 永远清不了零，退避也就永远不会生效。
        """
        last_at = float(state.get("last_at") or 0.0)
        if last_at <= 0:
            state["no_reply_streak"] = 0
            return 0

        stamps = self._timestamps(messages)
        current_ts = stamps[-1] if stamps else now
        answered = any(last_at < ts < current_ts for ts in stamps)
        previous = int(state.get("no_reply_streak") or 0)
        backoff_limit = self._int_setting("active_reply_backoff_limit")
        if answered or backoff_limit <= 0:
            streak = 0
        else:
            # 上限留到 limit+2 而不是 limit：streak 卡在 limit 的话，
            # _effective_cooldown 里 2**(streak-limit+1) 永远是 2，只在 ×2 一档，
            # ×4 封顶那档永远走不到。多留两级让倍数能爬到封顶值。
            streak = min(previous + 1, backoff_limit + 2)
        state["no_reply_streak"] = streak
        return streak

    def _effective_cooldown(self, streak: int) -> float:
        """streak 达到退避阈值后开始放大冷却。

        没有超过阈值时不动冷却：一两次没人接话在群里太正常了，
        每次失败都翻倍会让功能在正常群里也显得时灵时不灵。
        """
        base = max(0, self._int_setting("active_reply_cooldown_seconds"))
        limit = self._int_setting("active_reply_backoff_limit")
        if streak <= 0 or limit <= 0 or streak < limit:
            return float(base)
        # 刚好达到阈值时 ×2，之后每多一次再翻倍，封顶 4 倍。
        # 用 streak - limit + 1 而不是 streak：用 streak 的话 streak 一到阈值
        # 就是 2**3=8，被 MAX_BACKOFF_FACTOR 直接截成 4，×2 那一档永远走不到，
        # 退避等于一次性跳到顶格。
        factor = min(2 ** (streak - limit + 1), MAX_BACKOFF_FACTOR)
        return float(base * factor)

    # ── 小工具 ──────────────────────────────────────────────────────────

    def _mode(self) -> str:
        raw = str(self.settings.get("active_reply_mode") or "").strip().lower()
        return _MODE_ALIASES.get(raw, "probability")

    def _int_setting(self, key: str) -> int:
        try:
            return max(0, int(self.settings.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _clamp(value: float, low: float, high: float) -> float:
        return max(low, min(high, value))

    @staticmethod
    def _is_self(event) -> bool:
        try:
            return str(event.get_sender_id()) == str(event.get_self_id())
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _has_result(event) -> bool:
        """本轮是否已经产出了回复。

        ``get_result`` 在部分版本上不存在，``_has_send_oper`` 更是私有方法，
        所以两个都用 getattr 兜底，取不到就当作「没有结果」继续往下判。
        """
        getter = getattr(event, "get_result", None)
        if callable(getter):
            try:
                if getter() is not None:
                    return True
            except Exception:  # noqa: BLE001
                pass
        has_oper = getattr(event, "_has_send_oper", None)
        if callable(has_oper):
            try:
                if has_oper():
                    return True
            except Exception:  # noqa: BLE001
                pass
        return False

    def _messages(self, session_id: str) -> list[dict]:
        try:
            history = self.storage.history_of(session_id, limit=200)
        except Exception:  # noqa: BLE001
            return []
        return [m for m in history if isinstance(m, dict)]

    @staticmethod
    def _timestamps(messages: list[dict]) -> list[float]:
        """带 timestamp 的消息时间戳，升序。没有 timestamp 的旧数据直接跳过。"""
        stamps: list[float] = []
        for msg in messages:
            try:
                stamps.append(float(msg.get("timestamp") or 0.0))
            except (TypeError, ValueError):
                continue
        return sorted(s for s in stamps if s > 0)

    def _count_since(self, messages: list[dict], since: float) -> int:
        return sum(1 for ts in self._timestamps(messages) if ts >= since)

"""关系状态：bot 和每个用户之间的相处进度。

这个模块只做计算，不做存盘。数据落在 ``Storage.bonds``（``bonds.json``），
因为关系是**每轮都会变**的高频数据，正好复用 storage 已有的 5 秒合并写盘；
自己再建一套防抖只会出现两套节奏。

设计取舍：
- **关系按 QQ 号唯一**，不按会话分账。同一个人在这个群、那个群、私聊里
  都是同一段关系——bot 对他的态度不该因为换了场合就变陌生。key 就是
  QQ 号本身（1.6.x 及以前是 ``umo|sender_id``，见 ``migrate``）。
- **数值全局累计，阶段按当前场合判**。亲密度是「这个人一共跟我聊了多少」，
  所以在群里涨的也带得到私聊；但「群聊最多到熟悉」这条封顶仍然按**当下**
  这句发生在群聊还是私聊来算——这样既不会换群就失忆，也不会在群里对
  五十个人都亲密。
- **60 秒内同一人连发不重复计数**。群里刷屏、连发三句、发表情，
  都只算一轮。否则一个话痨三天就能刷到「很熟」，这个数值就没意义了。
- 指令、主动回复、空消息由调用方过滤，这里只认「真的聊了一句」。
- 阶段是派生值，存下来只为面板少算一次；每次 ``touch`` 都会重算，
  所以不存在「亲密度涨了但阶段还是旧的」这种脏数据。

1.5.0 起数值体系换成 **-100 ~ 1000 的千分制**，并补上三件事：

1. **负好感**（``hostile`` 档）。被明确骂了会掉分，掉到 0 以下进入「不待见」，
   注入语气跟着收敛。没有这项的话，bot 只能学会「越聊越亲」，
   遇到真不喜欢它的人反而更热情。
2. **自然衰减**。久不联系会淡，而且**越久越快**：保留率 ``1-(t/30)^2``，
   半个月大概忘掉四分之一，一个月基本清零。用平方而不是标准指数，
   是因为 ``e^-kt`` 永远到不了 0，「一个月没聊就忘了这个人」这个体验
   需要一条能真的走到 0 的曲线。
3. **按档位给记忆配额**。记忆是每轮都要进 prompt 的开销，
   对刚说过两句话的人和对主人用同样的字数，纯属烧钱。
   见 ``MEMORY_TIERS``。

衰减用**惰性结算**：只在读和写的那一刻按时间差算，不起定时任务。
一个只有五个人的 bot 没必要为「三个月没说话」跑一个每天扫全表的 job。
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from astrbot.api import logger

BEIJING_TZ = timezone(timedelta(hours=8))

# ── 阶段 ────────────────────────────────────────────────────────────────
# 负档排在 0 之前，顺序即亲密度递增方向，别乱序。
STAGE_HOSTILE = "hostile"
STAGE_STRANGER = "stranger"
STAGE_FAMILIAR = "familiar"
STAGE_CLOSE = "close"
STAGE_INTIMATE = "intimate"
STAGES = (STAGE_HOSTILE, STAGE_STRANGER, STAGE_FAMILIAR, STAGE_CLOSE, STAGE_INTIMATE)

# 档位高低。群聊封顶、按档给配额都用它比大小，不要用字符串比较。
STAGE_RANK = {name: idx for idx, name in enumerate(STAGES)}

# 注入时给模型的措辞。刻意写得短：这几句每轮都会进 prompt，
# 长一句就多花一份钱，而且话越多模型越容易演过头。
STAGE_TONE = {
    STAGE_HOSTILE: "对方对你有点不满，说话收敛些，别套近乎也别讨好",
    STAGE_STRANGER: "还不太熟，说话客气自然一点，别自来熟",
    STAGE_FAMILIAR: "已经聊得比较熟了，可以随意一些",
    STAGE_CLOSE: "很熟了，可以亲近、开玩笑，但别腻",
    STAGE_INTIMATE: "老熟人了，说话不用铺垫，可以互相打趣",
}

# 面板和指令里给人看的说法
STAGE_LABEL = {
    STAGE_HOSTILE: "不待见",
    STAGE_STRANGER: "陌生",
    STAGE_FAMILIAR: "熟悉",
    STAGE_CLOSE: "亲近",
    STAGE_INTIMATE: "老友",
}

# 主人的固定措辞。单独一句而不是拼在阶段后面，是因为主人可能还在「陌生」，
# 但「这是你的主人」这件事和聊了几天无关。
# 最后半句是护栏：不写的话，模型容易滑成无脑讨好，那比冷淡更难看。
OWNER_TONE = "这是你的主人，可以更亲近、更随意一些，但别刻意讨好"

# 默认门槛：熟悉 / 亲近 / 老友。
# 按千分制算，熟悉要聊 130 多轮（群聊口径），是一个努力一两个月才到的位置；
# 老友基本只有私聊长期用户能摸到——「老友」本来就该稀有。
DEFAULT_THRESHOLDS = (200, 500, 800)

# 老数据一次性换算系数。1.6.x 是「0.5 点/轮、60 秒窗口」，新刻度是
# 「5 点/次、15 分钟且必须 bot 回过」，两套口径差约一个数量级。
# 取 5 而不是 10 是保守换算：老轮次是 60 秒窗口数出来的，密度虚高，
# 一轮的价值低于新口径的一次交互。实测全库最大 49 点，×5 后 245 点，
# 只有主人进「熟悉」——正好符合「群聊封顶=熟悉」的设计。
LEGACY_SCALE = 5.0

# 1.6.x 的默认门槛。留着只为了认出「这个人没改过配置」，
# 见 ``BondStore._cfg``。别拿它当正常值用。
LEGACY_THRESHOLDS = "50,150"

# 数值上下限。上限是硬顶，配多大的增长都不越过；
# 下限给负好感一个底，不至于因为一次争吵就变成 -999。
MAX_INTIMACY = 1000.0
MIN_INTIMACY = -100.0
# 绝对值超过这个数才认为「手动填的值需要夹一下」
INTIMACY_EPS = 1e-6

# 好感变化的冷却（秒）。默认 15 分钟：同一个人在这段时间里**只判一次**。
#
# 为什么一路拉到 15 分钟：60 秒的粒度下，一场十分钟的对话能涨十次，
# 一个话痨一下午就能从陌生聊到亲近——数值反映的不是关系，是谁打字快。
# 15 分钟大约是「一个话题聊完」的长度，和人对「今天跟他聊过几次」的感知接近。
#
# 更关键的是：这条冷却现在只管「判一次」，而**能不能判**由另一个条件决定——
# 必须是 bot 真的回了这个人（见 hooks.touch_relation，挂在发送后钩子上）。
# 两个条件叠起来：群里自顾自刷屏、bot 没搭理的，一点好感都不涨。
DEFAULT_GROWTH_COOLDOWN = 900.0

# 单轮注入字数上限的硬顶。配置写多大都不超过它——
# 这段是每轮必带的内容，失控一次就是永久账单。
MAX_TEXT_CHARS = 300

# 自然衰减周期（天）。到这一天保留率归零：一个月没说过话，就当重新认识。
# 曲线是 1-(t/30)^2，所以半个月刚好忘掉 25%，一个月忘光，中间越来越快。
DECAY_DAYS = 30.0

# 按好感档位给的记忆配额：(关系段字数, 事实条数, 事实字数)。
#
# 这是本版最大的省钱项。以前不管跟谁说话都注入 150 字关系段 + 3 条事实，
# 一个只在群里问过一句「这是啥」的路人，和聊了半年的主人花一样的钱。
# 现在陌生档砍到 120 字 + 2 条，只有真熟的人才用满配额。
MEMORY_TIERS = {
    STAGE_HOSTILE: (100, 1, 80),
    STAGE_STRANGER: (120, 2, 110),
    STAGE_FAMILIAR: (150, 3, 180),
    STAGE_CLOSE: (200, 5, 260),
    STAGE_INTIMATE: (260, 8, 380),
}

# 主人恒用最高配额，不受阶段限制。这个人是配置里点名的，
# 少记他的事省下来的钱，远不如记错一次的成本高。
OWNER_TIER = (MAX_TEXT_CHARS, 10, 500)


def _cooldown_of(cfg: dict[str, Any]) -> float:
    """好感变化的冷却秒数。读配置、夹下限、写错了用默认值。

    允许调到 0（每句都算），但默认 600。配成负数等于 0，
    否则会出现「永远在冷却」这种死锁。
    """
    raw = cfg.get("growth_cooldown", DEFAULT_GROWTH_COOLDOWN)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = DEFAULT_GROWTH_COOLDOWN
    return max(0.0, value)


def _clamp(value: float) -> float:
    """把亲密度夹进 [-100, 1000]。"""
    if value > MAX_INTIMACY:
        return MAX_INTIMACY
    if value < MIN_INTIMACY:
        return MIN_INTIMACY
    return value


def keep_ratio(days: float) -> float:
    """久不联系的保留比例。``days`` 是距上次聊天/上次结算的天数。

    曲线 ``1 - (t/30)^2``：
    - 7 天 → 0.95（几乎没掉，一周不聊很正常）
    - 15 天 → 0.75（忘掉四分之一）
    - 21 天 → 0.51
    - 30 天 → 0（忘光）

    二次函数的好处是「前期几乎不动、后期陡然归零」，符合人对关系的感受：
    半个月没联系还是朋友，一个月没联系就真忘了。标准指数衰减永远到不了 0，
    会出现「三年没说话但还记得挺熟」这种不像人的状态。
    """
    if days <= 0:
        return 1.0
    if days >= DECAY_DAYS:
        return 0.0
    ratio = days / DECAY_DAYS
    return max(0.0, 1.0 - ratio * ratio)


def parse_thresholds(raw: Any) -> tuple[int, int, int]:
    """把 ``"200,500,800"`` 解析成 ``(familiar, close, intimate)`` 三档门槛。

    容错到「写错了就用默认」：少一个、不是数字、倒序、负数，
    一律回退默认值，而不是抛异常——这个值调错最多是关系推进快慢不对，
    不该让插件起不来。

    只写两个值（1.6.x 的老配置 ``"50,150"``）时，第三档按第二档的 1.6 倍
    自动补出来。不这么做的话，升级后「老友」那一档会凭空消失，
    而用户并不知道要去改这一行。
    """
    text = str(raw or "").strip()
    if not text:
        return DEFAULT_THRESHOLDS
    parts = [p.strip() for p in text.replace("，", ",").split(",") if p.strip()]
    try:
        values = [int(float(p)) for p in parts]
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLDS
    if len(values) >= 3:
        familiar, close, intimate = values[0], values[1], values[2]
        if 0 < familiar < close < intimate:
            return familiar, close, min(intimate, int(MAX_INTIMACY))
        return DEFAULT_THRESHOLDS
    if len(values) == 2:
        familiar, close = values
        if 0 < familiar < close:
            third = min(int(MAX_INTIMACY), max(close + 1, int(round(close * 1.6))))
            return familiar, close, third
        return DEFAULT_THRESHOLDS
    return DEFAULT_THRESHOLDS


def parse_owners(raw: Any) -> set[str]:
    """把主人 QQ 列表解析成集合。

    支持 ``"123456"``、``"123,456"``、``"123，456"``、空格分隔混写。
    口径和 ``admin_extra_users`` 一致：逗号或空白都能断，中文逗号也认。
    非数字项照样收下——万一某个平台用的是非 QQ 号的用户标识，
    硬校验反而会让人填不进去。
    """
    text = str(raw or "").strip()
    if not text:
        return set()
    return {p.strip() for p in re.split(r"[,，\s]+", text) if p.strip()}


def stage_of(
    intimacy: float,
    thresholds: tuple[int, int, int],
    *,
    is_group: bool,
    cap_group: bool,
    is_owner: bool = False,
) -> str:
    """按亲密度算阶段。

    ``cap_group`` 为真时群聊封顶在「熟悉」：一个 bot 在群里对五十个人都
    「很熟」，那不是亲近，那是客服。私聊才允许往上走。

    主人不受这个封顶约束——「群里不跟谁都亲近」这条规矩防的是场面上的
    泛亲密，主人是唯一那个例外。增量仍然照常减半，不搞特殊加速。

    负值直接进「不待见」，和封顶无关：被骂了还要维持客气，比冷淡更假。
    """
    familiar, close, intimate = thresholds
    try:
        value = float(intimacy or 0)
    except (TypeError, ValueError):
        value = 0.0
    if value < 0:
        return STAGE_HOSTILE
    if value >= intimate:
        stage = STAGE_INTIMATE
    elif value >= close:
        stage = STAGE_CLOSE
    elif value >= familiar:
        stage = STAGE_FAMILIAR
    else:
        stage = STAGE_STRANGER
    if is_group and cap_group and not is_owner and STAGE_RANK[stage] > STAGE_RANK[STAGE_FAMILIAR]:
        return STAGE_FAMILIAR
    return stage


def bond_key(sender_id: str) -> str:
    """关系主键：就是 QQ 号本身。

    1.6.x 及以前是 ``umo|sender_id``，那时候换一个群就成了另一个「人」。
    现在统一到人，跨群、跨私聊都是同一条关系。
    """
    return str(sender_id or "").strip()


def new_record(now: float | None = None) -> dict[str, Any]:
    """一条全新的关系记录。

    ``last_umo`` 只是「最近一次在哪聊的」，给面板显示用，不参与识别；
    ``pinned`` 是面板置顶，主人不需要这个字段（代码里直接按配置认）。

    ``decay_base`` / ``decay_base_at`` 是衰减的结算基准：
    显示值 = ``decay_base × keep_ratio(now - decay_base_at)``。
    存基准而不是存「已经衰减过的值」，是为了让手动改数值、加增量、
    连续读取这几件事都不会互相打架——见 ``settle``。
    """
    at = float(now if now is not None else time.time())
    return {
        "first_talk": at,
        "last_talk": at,
        "turns": 0,
        "intimacy": 0,
        "decay_base": 0.0,
        "decay_base_at": at,
        "nickname": "",
        "stage": STAGE_STRANGER,
        "last_umo": "",
        "pinned": False,
        # 上次**涨分**的时刻。冷却按它算；0 表示从没涨过，首轮一定算。
        "last_change": 0.0,
        "last_negative": 0.0,
        "gain_day": "",
        "gain_today": 0,
        # 千分制换算做过了。新记录天生就是新刻度，直接标上，
        # 免得 migrate_scale 把刚建的记录又乘一遍
        "scaled_v2": True,
    }


def _merge_record(base: dict[str, Any] | None, extra: dict[str, Any], umo: str) -> dict[str, Any]:
    """把两条同人记录合并成一条（迁移用）。

    合并口径：
    - ``turns`` / ``intimacy`` **累加**。因为亲密度本来就是「聊了多少轮 ×
      每轮增量」，分账时期各群各自累计，合并时相加才守得住总量；取最大值
      会让人明明聊了 50 轮却只显示 15 点。
    - ``first_talk`` 取最早、``last_talk`` 取最晚——认识多久和上次说话
      都是事实，不能被合并抹掉。
    - ``nickname`` / ``sender_name`` **取最近的非空值**。只取「最近那份」
      会把之前在别的群设过的称呼清空——用户设过就是设过，不该因为
      最近一次聊天发生在没设称呼的场合就丢掉。``pinned`` 一边为真就为真。
    - ``decay_base`` 累加，``decay_base_at`` 取最晚：两条旧记录都是「最后一次
      聊天时的基准」，相加后再从最近那次开始计时，等价于合并后的总进度。
    """
    if not isinstance(base, dict):
        base = {}
    base_is_newer = _as_float(base.get("last_talk")) >= _as_float(extra.get("last_talk"))
    newest, older = (base, extra) if base_is_newer else (extra, base)
    first = [v for v in (_as_float(base.get("first_talk")), _as_float(extra.get("first_talk"))) if v > 0]
    last = [v for v in (_as_float(base.get("last_talk")), _as_float(extra.get("last_talk"))) if v > 0]

    def _base_of(rec: dict[str, Any]) -> float:
        if rec.get("decay_base") is not None:
            return _as_float(rec.get("decay_base"))
        return _as_float(rec.get("intimacy"))

    merged_base = _clamp(_base_of(base) + _base_of(extra))
    anchors = [v for v in (_as_float(base.get("decay_base_at")), _as_float(extra.get("decay_base_at"))) if v > 0]
    return {
        "first_talk": min(first) if first else 0.0,
        "last_talk": max(last) if last else 0.0,
        "turns": _as_int(base.get("turns")) + _as_int(extra.get("turns")),
        "intimacy": round(merged_base, 2),
        "decay_base": round(merged_base, 2),
        "decay_base_at": max(anchors) if anchors else (max(last) if last else 0.0),
        "nickname": str(newest.get("nickname") or older.get("nickname") or ""),
        "sender_name": str(newest.get("sender_name") or older.get("sender_name") or ""),
        "stage": STAGE_STRANGER,
        "last_umo": str(newest.get("last_umo") or umo or ""),
        "pinned": bool(base.get("pinned") or extra.get("pinned")),
        "last_change": max(
            _as_float(base.get("last_change")), _as_float(extra.get("last_change"))
        ),
        "last_negative": max(
            _as_float(base.get("last_negative")), _as_float(extra.get("last_negative"))
        ),
        "gain_day": "",
        "gain_today": 0,
    }


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _days_between(start: float, now: float) -> int:
    """相处天数，至少 1 天——「认识第 0 天」读起来像 bug。"""
    if start <= 0:
        return 1
    days = int(max(0.0, now - start) // 86400)
    return days + 1


def _fmt_date(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts, BEIJING_TZ).strftime("%Y-%m-%d")
    except (ValueError, OSError, TypeError):
        return "—"


def _today(now: float) -> str:
    """北京时间的日期串，用来判断「今天涨的额度用完了没」。"""
    try:
        return datetime.fromtimestamp(now, BEIJING_TZ).strftime("%Y-%m-%d")
    except (ValueError, OSError, TypeError):
        return ""


class BondStore:
    """关系状态的读写与文本生成。

    一个实例挂在插件上（``plugin.bond``）。没有后台任务，所有推进都发生在
    消息进入的那一刻，读的时候顺手把阶段补齐。
    """

    def __init__(self, plugin):
        self.plugin = plugin
        self.storage = plugin.storage
        self.settings = plugin.settings
        # 「旧门槛已按新刻度算」这条提示只打一次，否则每轮都刷日志
        self._legacy_notified = False
        try:
            self.migrate_legacy()
        except Exception as e:  # noqa: BLE001 - 迁移失败不该让插件起不来
            logger.warning(f"[群聊记忆] 旧关系数据迁移失败（本次跳过）: {e}")
        try:
            self.migrate_scale()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[群聊记忆] 旧数值换算失败（本次跳过）: {e}")

    # ── 迁移 ────────────────────────────────────────────────────────────

    def migrate_legacy(self) -> int:
        """把 1.6.x 的「会话|用户」分账合并成按 QQ 号唯一。

        幂等：库里没有带竖线的旧键就直接返回 0。合并规则见 ``_merge_record``，
        多个群里的同一个 QQ 会被压成一条，亲密度和轮次累加。
        """
        bonds = self.storage.bonds
        legacy = [k for k in list(bonds.keys()) if "|" in str(k)]
        if not legacy:
            return 0

        merged: dict[str, dict] = {}
        for key in legacy:
            raw = bonds.pop(key, None)
            umo, _, sender_id = str(key).partition("|")
            if not isinstance(raw, dict) or not sender_id:
                continue
            merged[sender_id] = _merge_record(merged.get(sender_id), raw, umo)

        # 与新键（已经有纯 QQ 号记录的）再合一次，避免迁移后同人有两条
        for sender_id, record in merged.items():
            exist = bonds.get(sender_id)
            bonds[sender_id] = (
                _merge_record(exist, record, record.get("last_umo", ""))
                if isinstance(exist, dict)
                else record
            )

        try:
            self.storage._mark("bonds")
        except Exception:  # noqa: BLE001
            pass
        logger.info(
            f"[群聊记忆] 关系数据已合并到 QQ 号：{len(legacy)} 条旧记录 → {len(merged)} 个人"
        )
        return len(legacy)

    def migrate_scale(self) -> int:
        """把 1.6.x 的旧点数一次性换算到千分制。

        旧口径是「每轮 +0.5、60 秒窗口」，新口径是「每次 +5、15 分钟冷却且
        bot 必须真回过」。不换算的话，聊了 70 轮的主人只有 49 点，
        也就是「聊了两个月还是个陌生人」——面板上 66 个人全是同一档，
        看起来像这套数值没生效。

        换算成 ``intimacy × 5``，夹在 [-100, 1000] 里。
        **不重置衰减时钟**（``decay_base_at`` 不动），否则久没聊的人
        会因为这次换算被"续命"。

        幂等靠 ``scaled_v2`` 标记：跑过一次的记录不再动，重启随便多少次都安全。
        新记录第一次写盘时就带这个标记（见 ``_new_record``），不会被误伤。
        """
        bonds = self.storage.bonds
        touched = 0
        for key in list(bonds.keys()):
            record = bonds.get(key)
            if not isinstance(record, dict):
                continue
            if record.get("scaled_v2"):
                continue
            # 老记录没有 scaled_v2，一律当作「未换算」处理：
            # 哪怕它这几天已经按新逻辑涨过，整体 ×5 也只多出几十点，
            # 而漏换算会让老用户永远卡在陌生档——两害相权取前者。
            base = _as_float(record.get("decay_base"), record.get("intimacy", 0))
            if base > 0:
                base = min(MAX_INTIMACY, base * LEGACY_SCALE)
            elif base < 0:
                # 负数不放大：老版本没有负向扣分，这里出现的负值只可能是
                # 手动改过的，放大它等于惩罚用户没做过的事
                base = max(MIN_INTIMACY, base)
            record["decay_base"] = round(base, 4)
            record["intimacy"] = round(base, 2)
            record["scaled_v2"] = True
            bonds[key] = record
            touched += 1

        if touched:
            try:
                self.storage._mark("bonds")
            except Exception:  # noqa: BLE001
                pass
            logger.info(
                f"[群聊记忆] 旧点数已换算到千分制（×{LEGACY_SCALE:g}）：{touched} 个人"
            )
        return touched

    # ── 配置 ────────────────────────────────────────────────────────────

    def _cfg(self) -> dict[str, Any]:
        try:
            cfg = self.settings.group("bond")
        except Exception:  # noqa: BLE001 - 配置读不到就按关闭处理
            return {}
        if not isinstance(cfg, dict):
            return {}
        # 1.6.x 的默认门槛是 50,150。换成千分制后这个值低于一半的「陌生」区间，
        # 聊过几句话的人会直接跳到「熟悉」——用户绝大多数没动过这一行，
        # 所以按新默认算一次，并提示去配置页改掉（这里不写用户配置文件：
        # 插件没有通用的分组写入接口，硬写容易把配置搞坏）。
        if str(cfg.get("level_thresholds") or "").replace(" ", "") == LEGACY_THRESHOLDS:
            cfg = dict(cfg)
            cfg["level_thresholds"] = ",".join(str(v) for v in DEFAULT_THRESHOLDS)
            if not self._legacy_notified:
                self._legacy_notified = True
                logger.warning(
                    "[群聊记忆] 关系门槛还是 1.6.x 的 50,150（千分制下太低，"
                    f"会让所有人直接「熟悉」），本次按 {'/'.join(str(v) for v in DEFAULT_THRESHOLDS)} 计算。"
                    "建议到插件配置页把「阶段门槛」改成 200,500,800。"
                )
        return cfg

    def enabled(self) -> bool:
        return bool(self._cfg().get("enabled"))

    # ── 衰减 ────────────────────────────────────────────────────────────

    def settle(self, record: dict[str, Any], now: float) -> dict[str, Any]:
        """把一条记录按「距基准时刻多久」结算出当前显示值。

        **为什么要单独存基准**（``decay_base``）而不是反复乘：
        衰减是「相对时间」的函数，如果每次读都拿当前值乘一个增量比例，
        同一秒读两次就会掉两次。存基准 + 只在结算时算绝对值，读多少次
        结果都一样，也不需要写盘。

        结算规则：
        - ``base × keep_ratio(now - base_at)``，超过 30 天直接归零；
        - 归零时连基准一起清掉——不然下次聊天加完增量，
          又会被这个已经过期的基准拉回 0；
        - 老记录没有基准字段（1.6.x 写的），把 ``intimacy`` 当基准、
          ``last_talk`` 当计时起点，等于「升级那一刻开始算衰减」。
        """
        rec = dict(record)
        base = rec.get("decay_base")
        if base is None:
            base = _as_float(rec.get("intimacy"))
        base = _as_float(base)
        anchor = _as_float(rec.get("decay_base_at")) or _as_float(rec.get("last_talk")) or now
        days = max(0.0, (now - anchor) / 86400.0)
        ratio = keep_ratio(days)
        value = _clamp(base * ratio)
        # 精度收到 2 位：面板上 3.0000000001 和 3 是同一个数，
        # 但完全归零要真的归零，不能留个 0.0001 让阶段卡在负数
        if abs(value) < 0.005:
            value = 0.0
        if ratio <= 0:
            base = 0.0
        rec["decay_base"] = round(base, 4)
        rec["intimacy"] = round(value, 2)
        rec["_decay_days"] = round(days, 2)
        rec["_keep_ratio"] = round(ratio, 4)
        return rec

    # ── 读取 ────────────────────────────────────────────────────────────

    def is_owner(self, sender_id: str) -> bool:
        """这个 QQ 是不是主人。

        按 QQ 号判断，不按会话：主人在哪个群、还是私聊，都是主人。
        配置读不到时返回 False（当普通用户处理），宁可少给特殊待遇，
        也不能因为配置异常把路人都认成主人。
        """
        try:
            uid = str(sender_id or "").strip()
            if not uid:
                return False
            # 走全局配置（顶层 owner_ids，回退到本组的旧字段）：
            # 「主人」不只是关系模块的概念——事实上限、私聊学习、清理跳过
            # 都要用同一份名单，各写一套迟早会不一致
            return uid in self.settings.owners()
        except Exception:  # noqa: BLE001
            return False

    def record(self, sender_id: str, now: float | None = None) -> dict[str, Any]:
        """取一条关系记录（已结算衰减）。不存在返回空记录的副本，**不写入**。

        读的时候不建记录：光看一眼 /关系 不该在库里留一行空数据，
        真正的写入推迟到第一次 ``touch``。
        """
        raw = self.storage.bond_of(bond_key(sender_id))
        if not isinstance(raw, dict):
            return new_record(now)
        record = dict(raw)
        for field in ("first_talk", "last_talk"):
            record[field] = _as_float(record.get(field))
        record["turns"] = _as_int(record.get("turns"))
        record["intimacy"] = _as_float(record.get("intimacy"))
        record["nickname"] = str(record.get("nickname") or "")
        record["last_umo"] = str(record.get("last_umo") or "")
        record["pinned"] = bool(record.get("pinned"))
        at = float(now if now is not None else time.time())
        return self.settle(record, at)

    def set_pinned(self, sender_id: str, pinned: bool) -> bool:
        """面板置顶开关。返回设置后的状态。

        主人不需要置顶也能排在最前面（面板按配置认主人），这里只登记
        用户手动钉的人，不往数据里写「主人」这种会过期的身份。
        """
        key = bond_key(sender_id)
        raw = self.storage.bond_of(key)
        if not isinstance(raw, dict) or not raw:
            return False
        record = dict(raw)
        record["pinned"] = bool(pinned)
        try:
            self.storage.set_bond(key, record)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[群聊记忆] 保存置顶失败: {e}")
            return bool(raw.get("pinned"))
        return record["pinned"]

    # ── 推进 ────────────────────────────────────────────────────────────

    def touch(
        self,
        umo: str,
        sender_id: str,
        *,
        sender_name: str = "",
        is_group: bool = False,
        negative: bool = False,
        direct: bool = False,
        now: float | None = None,
    ) -> dict[str, Any]:
        """记一轮真实对话，返回更新后的记录。

        调用方负责过滤：指令、纯表情、主动回复、bot 自己说的话都不该走到这里。
        这里做「结算衰减 + 时间窗口 + 累计 + 阶段重算」四件事。

        ``negative`` 为真时这一轮只扣分不涨分——被骂了一句还涨好感，
        这个数值就彻底没意义了。

        ``direct`` 表示这条消息是不是**冲着 bot** 来的（@ 了它、回复了它的消息，
        或者本来就是私聊）。它决定用哪个基准增量，见下面的注释。
        """
        at = float(now if now is not None else time.time())
        key = bond_key(sender_id)
        raw = self.storage.bond_of(key)
        record = dict(raw) if isinstance(raw, dict) else new_record(at)
        record = self.settle(record, at)

        record.setdefault("first_talk", at)
        turns = _as_int(record.get("turns"))
        base = _as_float(record.get("decay_base"), record.get("intimacy", 0))

        cfg = self._cfg()
        hit_negative = bool(negative and cfg.get("negative_enabled", True))
        cooldown = _cooldown_of(cfg)

        last_change = _as_float(record.get("last_change"))
        last_neg = _as_float(record.get("last_negative"))
        # 冷却期：距上次变化不足 cooldown 就什么都不改，只更新「上次说话」。
        #
        # 涨分多一个条件「最近没被骂过」：骂完十分钟内不该又热乎起来，
        # 否则一次扣分下一句就补回来了，负向等于没做。
        # 反过来，扣分只看自己的冷却——被骂是明确信号，不该被「刚聊过」挡住。
        #
        # 老记录没有这两个字段（1.6.x），读出来是 0，等于「从没变过」，
        # 升级后第一次对话照常算一次，不会卡在冷却里。
        can_drop = at - last_neg >= cooldown
        can_raise = at - last_change >= cooldown and at - last_neg >= cooldown

        changed = False
        if hit_negative:
            if can_drop:
                penalty = _as_float(cfg.get("negative_per_hit"), 6.0)
                if penalty < 0:
                    penalty = 0.0
                if is_group:
                    # 群聊里的重话有一半是说给场面听的，扣一半
                    penalty = penalty / 2
                base -= penalty
                record["last_negative"] = at
                changed = True
        elif can_raise:
            # 增量按**场景**直接取两个明确的数，不再做「基准 × 群聊减半 × 被动四分之一」
            # 那种三层乘法。
            #
            # 三层乘法的问题是系数太抽象：用户看到面板上一次涨 1.25，根本算不出
            # 这是怎么来的，也没法判断「我到底希望它涨多快」。改成两个数之后，
            # 改一个数就是改一个数，解释成本归零。
            #
            # - **直接对话**（@ 了 bot、回复了 bot 的消息、私聊）：0.25 点
            # - **群聊旁听**（对方没指向 bot，bot 顺带接了一句）：0.1 点
            #
            # 群聊里绝大多数交互是第二种。按第一种的价码算，几百轮就能把一个
            # 从没跟 bot 说过话的人养成「老友」——那不是关系，是刷出来的。
            if direct:
                growth = _as_float(cfg.get("growth_per_turn"), 3.1)
            else:
                growth = _as_float(cfg.get("group_passive_gain"), 1.25)
            if growth < 0:
                growth = 0.0
            # 群聊硬顶：到了这个分数，群里再怎么聊都不再涨（只封涨，不封跌）。
            #
            # 默认 200 = 「熟悉」门槛。这样「群聊能把关系推到哪」是可预期的：
            # 不管群多热闹、聊多久，所有人都停在熟悉，想往上只能私聊。
            # 调成 0 就不封顶，回到「群里也能涨到老友」的旧行为。
            group_cap = _as_float(cfg.get("group_gain_cap"), 200.0)
            if is_group and group_cap > 0 and base >= group_cap:
                growth = 0.0
            # 指数曲线：越高越难涨。
            #
            # 线性增长的问题是「越往后越快」的错觉——从 800 到 1000 和从 0 到 200
            # 花的时间一样，但前者应该难得多。这里用半衰式衰减：
            # base=0 → ×1.0，300 → ×0.5，600 → ×0.25，900 → ×0.125。
            # 好处（坏处）是永远涨不到顶，只能无限逼近——「老友」是个要养的位置，
            # 不是攒够轮次就发的东西。
            scale = _as_float(cfg.get("growth_curve_scale"), 300.0)
            if scale > 0 and base > 0:
                growth *= 0.5 ** (base / scale)
            # 每日净增锁：可选，默认关。10 分钟冷却之后它基本用不上了，
            # 留着是给「把冷却调到 0」的用户当第二道闸
            cap = _as_int(cfg.get("daily_gain_cap"), 0)
            today = _today(at)
            if _as_int(record.get("gain_today")) and record.get("gain_day") != today:
                record["gain_today"] = 0
            if cap > 0 and growth > 0:
                used = _as_float(record.get("gain_today"))
                growth = max(0.0, min(growth, cap - used))
            if growth > 0:
                base += growth
                record["last_change"] = at
                record["gain_day"] = today
                record["gain_today"] = _as_float(record.get("gain_today")) + growth
                changed = True

        # 只有真的变了才记一轮。「轮」现在的含义是「一次有效交互」，
        # 不是「说了几句话」——冷却期内的消息仍然会刷新「上次说话」，
        # 但不涨分也不计数，否则「聊过 200 轮」和「刚认识」会同时成立。
        if changed:
            turns += 1

        record["last_talk"] = at
        # 计时的起点跟着「最后一次真的动了数值的时刻」走。没涨分（60 秒内
        # 连发、或日额度用完）也重设：衰减算的是「多久没联系」，不是
        # 「多久没涨分」——刚说过话的人不该继续掉。
        record["decay_base_at"] = at
        base = _clamp(base)
        record["decay_base"] = round(base, 4)
        record["intimacy"] = round(base, 2)
        record["turns"] = turns
        record["stage"] = stage_of(
            base,
            parse_thresholds(cfg.get("level_thresholds")),
            is_group=is_group,
            cap_group=bool(cfg.get("group_cap_familiar", True)),
            is_owner=self.is_owner(sender_id),
        )
        if sender_name:
            record["sender_name"] = str(sender_name)[:32]
        # 只用来在面板上说「最近在哪聊的」，不参与识别
        if umo:
            record["last_umo"] = str(umo)

        # 内部统计字段不落盘
        record.pop("_decay_days", None)
        record.pop("_keep_ratio", None)

        try:
            self.storage.set_bond(key, record)
        except Exception as e:  # noqa: BLE001 - 存不下最多是关系不涨，不能影响回复
            logger.warning(f"[群聊记忆] 保存关系状态失败: {e}")
        return record

    def negative_ready(self, sender_id: str, now: float | None = None) -> bool:
        """这个人现在骂一句，算不算数（冷却过了没）。

        冷却期内不重复扣分，但也不阻止调用方照常记轮次——
        「他刚才骂了我三句」和「他跟我聊了三轮」是两件事。
        """
        at = float(now if now is not None else time.time())
        record = self.record(sender_id, now=at)
        last = _as_float(record.get("last_negative"))
        return last <= 0 or at - last >= _cooldown_of(self._cfg())

    def set_intimacy(self, sender_id: str, value: float) -> dict[str, Any] | None:
        """面板手动改亲密度。返回改完的 summary，记录不存在返回 None。

        手动改的意思就是「以这个数为准」，所以同时把衰减计时重置到现在：
        如果一个 20 天没说话的人被手动设成 500，不重置的话下次读一下
        就按 20 天衰减掉 44%，用户会以为按钮坏了。

        阶段跟着重算，否则面板上会出现「500 点但显示陌生」。
        """
        key = bond_key(sender_id)
        raw = self.storage.bond_of(key)
        if not isinstance(raw, dict) or not raw:
            return None
        at = time.time()
        record = self.settle(dict(raw), at)
        fixed = _clamp(float(value))
        record["decay_base"] = round(fixed, 4)
        record["decay_base_at"] = at
        record["intimacy"] = round(fixed, 2)
        last_umo = str(record.get("last_umo") or "")
        record["stage"] = stage_of(
            fixed,
            parse_thresholds(self._cfg().get("level_thresholds")),
            is_group=":GroupMessage:" in last_umo,
            cap_group=bool(self._cfg().get("group_cap_familiar", True)),
            is_owner=self.is_owner(sender_id),
        )
        record.pop("_decay_days", None)
        record.pop("_keep_ratio", None)
        try:
            self.storage.set_bond(key, record)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[群聊记忆] 保存亲密度失败: {e}")
            return None
        last_umo = str(record.get("last_umo") or "")
        return self.summary(sender_id, is_group=":GroupMessage:" in last_umo)

    def set_nickname(self, sender_id: str, nickname: str) -> str:
        """设定称呼。空字符串等于清除，回落到平台昵称。

        支持**多个称呼**，逗号（中英文）、顿号、分号、斜杠都算分隔符——
        用户从别处粘贴过来时中英文标点混用是常态。
        存的时候保留原样（面板要能看回去），注入时才拆开。
        上限 40 字：够放三四个叫法，又不至于把一段话塞进「称呼」里。
        """
        key = bond_key(sender_id)
        raw = self.storage.bond_of(key)
        record = dict(raw) if isinstance(raw, dict) else new_record()
        cleaned = str(nickname or "").strip()[:40]
        record["nickname"] = cleaned
        try:
            self.storage.set_bond(key, record)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[群聊记忆] 保存称呼失败: {e}")
        return cleaned

    # ── 记忆配额 ────────────────────────────────────────────────────────

    def stage_now(self, sender_id: str, *, cap_group: bool = False) -> str:
        """这个人现在处于哪一档。**不封顶**是默认。

        事实模块拿它定「能存多少条」。存储是全局的（按 QQ 号一份），
        所以不能用群聊封顶的口径——否则同一个人在群里说一句，
        就会把他私聊攒下的一批记忆按「群聊只到熟悉」砍掉。
        上限该反映「跟他多熟」，不是「在哪个场合」。
        """
        cfg = self._cfg()
        record = self.record(sender_id)
        return stage_of(
            record.get("intimacy", 0),
            parse_thresholds(cfg.get("level_thresholds")),
            is_group=cap_group,
            cap_group=cap_group,
            is_owner=self.is_owner(sender_id),
        )

    def memory_tier(
        self,
        sender_id: str,
        *,
        is_group: bool = False,
        stage: str | None = None,
    ) -> tuple[int, int, int] | None:
        """按好感档位返回 ``(关系段字数, 事实条数, 事实字数)``。

        返回 None 表示「不分档」——调用方按配置里的固定上限走。
        分档关掉时就是这个行为，等于回到 1.6.x。
        """
        cfg = self._cfg()
        if not cfg.get("tiered_memory", True):
            return None
        if self.is_owner(sender_id):
            return OWNER_TIER
        st = stage
        if not st:
            record = self.record(sender_id)
            st = stage_of(
                record.get("intimacy", 0),
                parse_thresholds(cfg.get("level_thresholds")),
                is_group=is_group,
                cap_group=bool(cfg.get("group_cap_familiar", True)),
                is_owner=False,
            )
        return MEMORY_TIERS.get(st, MEMORY_TIERS[STAGE_STRANGER])

    # ── 文本 ────────────────────────────────────────────────────────────

    def build_text(
        self,
        sender_id: str,
        *,
        is_group: bool = False,
        now: float | None = None,
    ) -> str | None:
        """拼本轮注入的关系段。关闭、或还没聊过，返回 None。

        ``is_group`` 只影响阶段封顶，不影响读到的数值——关系是全局的，
        同一个人在这个群和那个群读到的是同一份。
        """
        cfg = self._cfg()
        if not cfg.get("enabled"):
            return None

        at = float(now if now is not None else time.time())
        record = self.record(sender_id, now=at)
        if _as_int(record.get("turns")) <= 0:
            return None

        is_owner = self.is_owner(sender_id)
        stage = stage_of(
            record.get("intimacy", 0),
            parse_thresholds(cfg.get("level_thresholds")),
            is_group=is_group,
            cap_group=bool(cfg.get("group_cap_familiar", True)),
            is_owner=is_owner,
        )
        # 主人用固定措辞：和聊了几天无关，也避开「不待见/熟悉/亲近」这套描述——
        # 那套是给群友分层用的，对主人用会显得生分
        tone = OWNER_TONE if is_owner else STAGE_TONE.get(stage, STAGE_TONE[STAGE_STRANGER])

        parts = [f"你和这个人认识 {_days_between(record.get('first_talk', 0), at)} 天，"
                 f"聊过 {_as_int(record.get('turns'))} 轮，{tone}。"]

        nickname = str(record.get("nickname") or "").strip()
        if nickname and cfg.get("nickname_enabled", True):
            # 多个称呼时全部告诉模型，而不是随机挑一个：
            # 随机挑会让它在同一段对话里前后叫法不一致，比不改还怪
            names = [
                x.strip()
                for x in re.split(r"[,，、;；/\s]+", nickname)
                if x.strip()
            ][:4]
            if names:
                parts.append("你叫他「" + "」或「".join(names) + "」。")

        note = str(cfg.get("relationship_note") or "").strip()
        if note:
            parts.append(note)

        text = "<relation>\n" + "".join(parts) + "\n</relation>"
        tier = self.memory_tier(sender_id, is_group=is_group, stage=stage)
        limit = tier[0] if tier else _as_int(cfg.get("inject_max_chars"), 150)
        if limit <= 0:
            return None
        limit = min(limit, MAX_TEXT_CHARS)
        if len(text) > limit:
            # 超了就整段丢，不做半截截断——截断出来的残句比不注入更糟
            logger.debug(f"[群聊记忆] 关系段超字数（{len(text)} > {limit}），本轮跳过")
            return None
        return text

    def _cooldown_left(self, record: dict[str, Any], now: float) -> int:
        """还要多少秒才会再变动一次。0 表示现在聊就算。"""
        cooldown = _cooldown_of(self._cfg())
        if cooldown <= 0:
            return 0
        last_change = _as_float(record.get("last_change"))
        last_neg = _as_float(record.get("last_negative"))
        # 两个时钟（上次涨 / 上次跌）里取**更晚**的那个：谁更晚，冷却就还在走。
        # 换算成「已经过去多久」就是取更小的值——第一次写的时候这里取了 max，
        # 把「从没被骂过」算成「刚才骂过」，结果冷却永远是 0。
        elapsed = now - last_change if last_change else float(cooldown)
        if last_neg:
            elapsed = min(elapsed, now - last_neg)
        return int(max(0.0, cooldown - elapsed))

    def summary(self, sender_id: str, *, is_group: bool = False) -> dict[str, Any]:
        """给 /关系 和面板用的只读视图。"""
        cfg = self._cfg()
        now = time.time()
        record = self.record(sender_id, now=now)
        thresholds = parse_thresholds(cfg.get("level_thresholds"))
        is_owner = self.is_owner(sender_id)
        capped = bool(is_group and cfg.get("group_cap_familiar", True) and not is_owner)
        stage = stage_of(
            record.get("intimacy", 0),
            thresholds,
            is_group=is_group,
            cap_group=bool(cfg.get("group_cap_familiar", True)),
            is_owner=is_owner,
        )
        intimacy = _as_float(record.get("intimacy"))
        familiar, close, intimate = thresholds
        # 下一阶段还差多少：让「怎么才能更熟」这件事有可见的进度
        if stage == STAGE_HOSTILE:
            need, next_label = max(0.0, 0.0 - intimacy), STAGE_LABEL[STAGE_STRANGER]
        elif stage == STAGE_STRANGER:
            need, next_label = max(0.0, familiar - intimacy), STAGE_LABEL[STAGE_FAMILIAR]
        elif stage == STAGE_FAMILIAR:
            if capped:
                need, next_label = 0.0, ""
            else:
                need, next_label = max(0.0, close - intimacy), STAGE_LABEL[STAGE_CLOSE]
        elif stage == STAGE_CLOSE:
            need, next_label = max(0.0, intimate - intimacy), STAGE_LABEL[STAGE_INTIMATE]
        else:
            need, next_label = 0.0, ""
        tier = self.memory_tier(sender_id, is_group=is_group, stage=stage)
        return {
            "turns": _as_int(record.get("turns")),
            "intimacy": round(intimacy, 1),
            "stage": stage,
            "stage_label": STAGE_LABEL.get(stage, stage),
            "stage_rank": STAGE_RANK.get(stage, 0),
            "thresholds": list(thresholds),
            "nickname": str(record.get("nickname") or ""),
            "days": _days_between(record.get("first_talk", 0), now),
            "first_talk": _as_float(record.get("first_talk")),
            "last_talk": _as_float(record.get("last_talk")),
            "idle_days": round(_as_float(record.get("_decay_days")), 1),
            "keep_ratio": round(_as_float(record.get("_keep_ratio"), 1.0), 2),
            # 还要等多久才会再涨一次：面板上直接说「冷却中 7 分钟」，
            # 否则用户看着点数不动会以为没生效
            "cooldown_left": self._cooldown_left(record, now),
            "next_label": next_label,
            "need": round(need, 1),
            "is_owner": is_owner,
            "pinned": bool(record.get("pinned")),
            "last_umo": str(record.get("last_umo") or ""),
            "capped": bool(capped and stage in (STAGE_FAMILIAR,)),
            # 这一档现在会注入多少记忆：面板上直接告诉用户「这就是花你钱的部分」
            "tier": list(tier) if tier else None,
        }

    @staticmethod
    def format_summary(info: dict[str, Any]) -> str:
        """把 summary 渲染成一条给人看的消息。"""
        nickname = info.get("nickname") or "（未设置，用平台昵称）"
        who = "主人" if info.get("is_owner") else None
        stage_text = f"{info.get('stage_label', '陌生')}（亲密度 {info.get('intimacy', 0)}）"
        if who:
            stage_text = f"{stage_text} · {who}"
        lines = [
            f"认识：第 {info.get('days', 1)} 天，聊过 {info.get('turns', 0)} 轮",
            f"关系：{stage_text}",
            f"称呼：{nickname}",
        ]
        if info.get("capped"):
            lines.append("群聊里最多到「熟悉」，私聊才会更近一步。")
        elif info.get("next_label"):
            lines.append(f"再攒 {info.get('need', 0)} 点亲密度到「{info.get('next_label')}」。")
        idle = info.get("idle_days") or 0
        if idle >= 1:
            keep = int(round(float(info.get("keep_ratio") or 0) * 100))
            lines.append(f"已经 {idle:g} 天没说话，按衰减规则还留着 {keep}%。")
        return "\n".join(lines)

    @staticmethod
    def format_since(ts: float) -> str:
        """上次聊天时间，给面板用。"""
        return _fmt_date(ts) if ts else "—"

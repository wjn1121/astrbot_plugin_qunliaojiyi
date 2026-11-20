"""AstrBot 事件钩子。

只放「消息进来」和「请求出去」两件事的实现，装饰器薄壳留在 main.py——
AstrBot 靠类方法反射收集 handler，方法必须在 Star 子类上定义。

1.3.0 在原来「收集 + 注入」两件事之外，又接了三件：
- 请求前接管搜索工具（含每群每日配额）与管理员工具；
- 主动回复（``maybe_active_reply``），命中后掐掉宿主原生回复并自己发一次请求；
- 回复后记账（``record_llm_usage``）与可选硬截断（``truncate_result``）。
"""

from __future__ import annotations

import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Plain

from .core.admin import build_admin_tool
from .core.feed import FEED_EXTRA_KEY, build_note, settle as settle_feed
from .core.injector import Injector, override_roll
from .core.learn_gate import learn_enabled
from .core.search import BUILTIN_SEARCH_TOOL_NAMES, build_search_tool
from .core.tone import clean_text, split_segments
from .utils import clean_system_prompt

# 主动回复标记：inject_style 靠它区分「这轮是插话还是被叫」
ACTIVE_EXTRA_KEY = "qunliao_active_reply"

# 本轮被纠正过的标记：消息进来时打上，注入时据此换成「别争辩」那句
CORRECTION_EXTRA_KEY = "qunliao_corrected"


def session_allowed(plugin, session_id: str, sender_id: str = "") -> bool:
    """会话/用户级过滤：全局白名单 → 全局黑名单 → 用户黑名单。"""
    settings = plugin.settings
    whitelist = settings.get("session_whitelist") or []
    blacklist = settings.get("session_blacklist") or []
    blocked = settings.get("blocked_users") or []
    if whitelist and session_id not in whitelist:
        return False
    if session_id in blacklist:
        return False
    if sender_id and str(sender_id) in {str(x) for x in blocked}:
        return False
    return True


def learn_allowed(plugin, session_id: str, sender_id: str = "") -> bool:
    """是否收集这个会话的消息用于学习（白黑名单 + 群聊/私聊分开关 + 会话级开关）。

    具体判定在 ``core.learn_gate.learn_enabled``——后台调度也用那一份，
    否则会出现「面板关了但定时任务还在学」。
    """
    if not session_allowed(plugin, session_id, sender_id):
        return False
    return learn_enabled(plugin.settings, plugin.storage, session_id, sender_id)


def inject_allowed(plugin, session_id: str, sender_id: str = "") -> bool:
    """是否向这个会话注入风格（全局过滤 + 会话级开关）。"""
    return session_allowed(plugin, session_id, sender_id) and plugin.storage.inject_allowed(
        session_id
    )


def is_active_turn(event: AstrMessageEvent) -> bool:
    """本轮是不是插件自己发起的主动插话。"""
    try:
        return bool(event.get_extra(ACTIVE_EXTRA_KEY))
    except Exception:  # noqa: BLE001
        return False


def is_direct_to_bot(event: AstrMessageEvent) -> bool:
    """这条消息是不是**冲着 bot** 来的。

    判据两条：

    - 消息链里有 ``At``，且被 @ 的是 bot 自己
    - 消息链里有 ``Reply``（引用回复），且被引用那条的发送者是 bot

    判不出来一律返回 False——宁可当成「群聊旁听」少涨一点，
    也不要把群里的背景闲聊抬到直接对话的价码。私聊不在这里判，
    调用方用 ``is_group`` 兜住（私聊天然是直接对话）。
    """
    try:
        self_id = str(event.get_self_id() or "")
        if not self_id:
            return False
        for comp in event.get_messages() or []:
            name = type(comp).__name__
            if name == "At":
                if str(getattr(comp, "qq", "") or "") == self_id:
                    return True
            elif name == "Reply":
                # aiocqhttp 会带上被引用消息的发送者；取不到就当没回复 bot
                sender = str(getattr(comp, "sender_id", "") or "")
                if sender and sender == self_id:
                    return True
    except Exception:  # noqa: BLE001 - 读消息链失败按「不是直接对话」处理
        return False
    return False


# 求助 / 说正事的信号词。刻意不用「怎么」这种到处都有的词——
# 「你怎么这么可爱」不该被判成求助。宁可漏判（继续按关系语气说话），
# 也不要误判（把闲聊全变成客服腔）。
SERIOUS_HINTS = (
    "帮我", "帮忙", "报错", "写个", "写一个", "查一下", "查查",
    "怎么解决", "怎么弄", "如何", "配置", "部署", "安装",
    "解释一下", "讲解", "是什么", "为什么", "区别",
)

# 长消息多半是正经事：一百多字的吐槽很少见，一百多字的问题很常见。
SERIOUS_LENGTH = 120

SERIOUS_NOTE = (
    "<relation_mode>\n"
    "本轮对方在说正事或求助。先把事情讲清楚：不用亲昵称呼，不玩梗，"
    "不加多余的情绪表达。\n"
    "</relation_mode>"
)

# 对方在「问你还记不记得」的信号词。命中但没有对应事实时走诚实出口。
RECALL_HINTS = (
    "记得", "还记得", "记不记得", "上次", "之前说", "上回", "我跟你说过",
    "我说过的", "记一下", "帮我记",
)

# 诚实出口。这一条比任何记忆功能都重要：
# 编造共同经历是最快失去信任的方式，而「想不起来」是正常人会说的话。
HONEST_NOTE = (
    "<facts>\n"
    "你没有任何关于这个人的记忆。如果对方问起「上次」「之前」说过的事，"
    "直接说想不起来，不要编，也不要假装记得。\n"
    "</facts>"
)

# 被纠正后的处理。不注入「我错了」这种台词，只要求它别再坚持、也别反复提。
CORRECTED_NOTE = (
    "<facts>\n"
    "对方指出你记错了。别争辩，简短承认就好，也不要再主动提这件事。\n"
    "</facts>"
)

# 纠正信号词。只认短句：长消息里出现「不对」多半是在讨论别的事，
# 那种情况下把记忆标成错的是误伤。
CORRECTION_HINTS = (
    "不对", "错了", "记错了", "说错了", "不是这样", "我没说过", "我没说",
    "搞错了", "弄错了",
)
CORRECTION_MAX_CHARS = 50

# ── 负面反馈（1.5.0）────────────────────────────────────────────────────
# 明确冲着 bot 来的短骂。命中才扣好感，所以词表刻意收窄：
# 「这游戏真垃圾」不是骂它，「你就是个垃圾」才是。宽词表 + 短句限制
# 已经是能接受的最优解——多扣几分无所谓，误判一次让人莫名其妙掉档才是事。
HOSTILE_STRONG = (
    "滚", "闭嘴", "别烦我", "讨厌你", "烦死你", "你好烦", "别说话了",
    "去死", "傻逼", "智障", "狗东西", "废物东西",
)
# 这些词要带「你」才算：单说「垃圾」可能是在骂别的东西
HOSTILE_WEAK = ("垃圾", "废物", "笨蛋", "蠢", "没用")
# 「我滚了」「滚去睡觉」这类自嘲或使唤自己的说法，不该算骂人
HOSTILE_EXCLUDE = ("我滚", "滚去", "不滚", "别滚")
HOSTILE_MAX_CHARS = 20


def looks_like_hostile(text: str) -> bool:
    """这句是不是在骂 bot。

    只看长度和关键词，不做语义判断：多花一次 LLM 调用去分辨
    「你是不是傻」是调侃还是真嫌弃，成本远高于这点扣分的收益。
    """
    body = str(text or "").strip()
    if not body or len(body) > HOSTILE_MAX_CHARS:
        return False
    if any(k in body for k in HOSTILE_EXCLUDE):
        return False
    if any(k in body for k in HOSTILE_STRONG):
        return True
    return "你" in body and any(k in body for k in HOSTILE_WEAK)


def is_serious_turn(plugin, req) -> bool:
    """本轮要不要收起关系语气。

    只判断，不注入。任何异常都返回 False（按正常聊天处理）——
    认真模式失效最多是这轮话多了点，不该让整个注入链路挂掉。
    """
    try:
        if not plugin.settings.get("serious_mode"):
            return False
        text = str(getattr(req, "prompt", "") or "").strip()
        if not text:
            return False
        if len(text) > SERIOUS_LENGTH:
            return True
        return any(hint in text for hint in SERIOUS_HINTS)
    except Exception:  # noqa: BLE001
        return False


# 「没有文字，但确实是在找你」的组件。发表情、发图、拍一拍都是互动，
# 只是它们进不了学习库（没有文本可学）。
# 不做这个区分的话，主人发张图、拍一拍，bot 那边一点动静都没有——
# 从用户视角看就是「插件没生效」。
INTERACTION_COMPONENTS = (
    "Image", "Face", "Mface", "Record", "Video", "Poke", "Dice", "RPS", "Shake",
)


def has_interaction_component(event: AstrMessageEvent) -> bool:
    """消息里有没有「算一次互动」的非文字组件。

    判不出来一律返回 False：宁可少记一轮，也不要因为读消息链出错
    把空消息（撤回、系统提示）全算成聊天。
    """
    try:
        for comp in event.get_messages() or []:
            if type(comp).__name__ in INTERACTION_COMPONENTS:
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _is_group(event: AstrMessageEvent) -> bool:
    """是否群聊。

    判不出来按群聊算：群聊的措辞更保守、亲密度增量也减半，
    判错时最多是关系涨得慢一点，不会出现「对每个群友都很熟」。
    """
    try:
        return not event.is_private_chat()
    except Exception:  # noqa: BLE001 - 个别平台或老版本没有这个方法
        return True


def looks_like_recall(text: str) -> bool:
    """对方是不是在问「你还记得吗」。

    只在真问记忆的时候才走诚实出口：平时每轮都插一句「你没有记忆」，
    模型会变得畏手畏脚，什么事都不敢接。
    """
    body = str(text or "")
    return any(hint in body for hint in RECALL_HINTS)


def looks_like_correction(text: str) -> bool:
    """对方是不是在说你记错了。"""
    body = str(text or "").strip()
    if not body or len(body) > CORRECTION_MAX_CHARS:
        return False
    return any(hint in body for hint in CORRECTION_HINTS)


# ── 刷屏检测 ────────────────────────────────────────────────────────────

# 每个 (会话, 发送者) 最近几条消息的时间戳。只在内存里，不落盘：
# 重启后重新计数可接受——这个检测防的是「短时间内刷量」，
# 而重启本身就会中断刷屏。
_FLOOD_BUCKETS: dict[str, list[float]] = {}
# 从没清理过的 key 会一直堆着，攒到这么多就顺手清一遍过期的
_FLOOD_SWEEP_AT = 500

FLOOD_EXTRA_KEY = "_qunliao_flooding"


def is_flooding(plugin, event: AstrMessageEvent) -> bool:
    """这条消息算不算刷屏。第一次调用时判定并记进 event，后面直接复用。

    为什么用 event 存：采集（on_message）和关系推进（after_message_sent）
    是两次不同的钩子，必须口径一致——采集时判了刷屏，涨好感时也得跳过，
    否则「刷屏不进学习库、但还是把关系刷上去了」。
    """
    cached = event.get_extra(FLOOD_EXTRA_KEY)
    if cached is not None:
        return bool(cached)

    session_id = str(getattr(event, "unified_msg_origin", "") or "")
    sender_id = str(event.get_sender_id() or "")
    window = int(plugin.settings.get("flood_window_seconds") or 0)
    limit = int(plugin.settings.get("flood_max_messages") or 0)
    result = False
    if window > 0 and limit > 0 and session_id and sender_id:
        key = f"{session_id}|{sender_id}"
        now = time.time()
        cutoff = now - window
        bucket = [t for t in _FLOOD_BUCKETS.get(key, []) if t >= cutoff]
        if len(bucket) >= limit:
            result = True
        else:
            bucket.append(now)
        _FLOOD_BUCKETS[key] = bucket
        if len(_FLOOD_BUCKETS) > _FLOOD_SWEEP_AT:
            for k in [k for k, v in _FLOOD_BUCKETS.items() if not v or v[-1] < cutoff]:
                _FLOOD_BUCKETS.pop(k, None)
        if result:
            logger.info(
                f"[群聊记忆] 刷屏检测 · {session_id} · {sender_id} "
                f"{window}s 内超过 {limit} 条，这条不计入学习和关系"
            )
    event.set_extra(FLOOD_EXTRA_KEY, result)
    return result


# ── 消息收集 ────────────────────────────────────────────────────────────


async def collect_message(plugin, event: AstrMessageEvent) -> None:
    """收集一条待学习消息。"""
    if not plugin.settings.get("enabled"):
        return
    if event.get_sender_id() == event.get_self_id():
        return

    session_id = event.unified_msg_origin
    if not learn_allowed(plugin, session_id, event.get_sender_id()):
        return

    content = (event.get_message_str() or "").strip()
    if not content or content.startswith("/"):
        return

    # 刷屏检测：超出的消息不进待学习缓冲。
    #
    # 防的不是「说得多的正常人」（一分钟 15 条已经算话痨），
    # 而是「靠刷量把学习提前触发」——一次提炼约 0.01 元，
    # 刷 600 条能触发 20 次，等于一个正常群一周的开销。
    # 这些消息仍留在 AstrBot 自己的对话上下文里，只是不进学习材料。
    if is_flooding(plugin, event):
        return

    plugin.storage.add_message(
        session_id,
        {
            "sender": event.get_sender_name() or event.get_sender_id(),
            # 存一份 ID：事实记忆要按人分桶，「老王」这种昵称对不上人，
            # 同一个人改个名就断成两条记忆
            "sender_id": event.get_sender_id() or "",
            "content": content[:1000],
            "timestamp": time.time(),
        },
        limit=int(plugin.settings.get("history_limit_per_session")),
    )


# ── 关系推进 ────────────────────────────────────────────────────────────


# 同一轮只记一次。发送后钩子在分段回复 / 流式下可能被调多次，
# 不去重的话一条消息能涨好几分（虽然冷却会拦，但「轮次」会被多算）。
TOUCHED_EXTRA_KEY = "_qunliao_bond_touched"


async def touch_relation(plugin, event: AstrMessageEvent) -> None:
    """记一次**真的发生了的**交互，推进和这个人的关系。

    这个函数挂在 ``after_message_sent`` 上，不是「收到消息」时——
    差别是整个设计的重点：

    - 有人在群里自顾自刷屏、bot 根本没搭理，一分都不涨。
      以前是收到就算，等于「谁打字多谁跟你熟」，和关系没有关系。
    - 必须 bot 真的发出了回复（空消息链、只有 @ 和回复引用的，
      管线会提前 return，钩子不触发），才算一次交互。

    另外两道闸：15 分钟冷却（同一个人这段时间只判一次，说再多句也一样），
    以及会话级白黑名单。指令、空消息、bot 自己的话都不算——
    否则刷几个 /风格状态 就能把亲密度刷上去。

    负向单独走：被明确骂一句时这一轮扣分而不是涨分，
    并且扣分只看自己的冷却——被骂是明确信号，不该被「刚聊过」挡住。
    """
    try:
        bond_cfg = plugin.settings.group("bond")
        if not bond_cfg.get("enabled"):
            return
        if event.get_sender_id() == event.get_self_id():
            return
        # 一轮只算一次：分段回复 / 流式会把钩子打多遍
        if event.get_extra(TOUCHED_EXTRA_KEY):
            return

        content = (event.get_message_str() or "").strip()
        if content.startswith("/"):
            return
        # 纯图 / 纯表情 / 拍一拍也算——bot 回过就是一次真实互动，
        # 只是没有文本可学，所以不进学习库
        if not content and not has_interaction_component(event):
            return

        session_id = event.unified_msg_origin
        sender_id = event.get_sender_id()
        if not session_allowed(plugin, session_id, sender_id):
            return

        # 刷屏的人不涨好感。口径和采集那边一致（同一个 extra），
        # 见 is_flooding 的注释：刷屏既不进学习库，也不推进关系，
        # 否则「刷 600 条」仍然能把关系刷上去，只是慢一点
        if is_flooding(plugin, event):
            return

        # 判不出私聊就按群聊算：群聊的增量更保守，判错时不会把关系推得太快
        is_group = _is_group(event)

        negative = False
        if bond_cfg.get("negative_enabled", True) and looks_like_hostile(content):
            try:
                negative = plugin.bond.negative_ready(sender_id)
            except Exception:  # noqa: BLE001
                negative = False
            if negative:
                logger.debug(f"[群聊记忆] 负面反馈 · {sender_id} · {content[:20]}")

        # 这一轮是「冲着 bot 说的」还是「群里顺带接了一句」？
        #
        # 私聊天然是直接对话；群里要看有没有 @ bot 或引用回复 bot 的消息。
        # 主动插话那一轮对方并没有指向 bot，所以也算群聊旁听——
        # 这是它该拿的价码，不是惩罚。
        try:
            direct = (not is_group) or is_direct_to_bot(event)
        except Exception:  # noqa: BLE001
            direct = False

        plugin.bond.touch(
            session_id,
            sender_id,
            sender_name=event.get_sender_name() or "",
            is_group=is_group,
            negative=negative,
            direct=direct,
        )
        # 标记放在 touch 之后：中途抛异常时下一段还能补记一次
        try:
            event.set_extra(TOUCHED_EXTRA_KEY, True)
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001 - 关系推进坏了不能影响正常回复
        logger.debug(f"[群聊记忆] 推进关系失败（可忽略）: {e}")


# ── 纠正检测 ────────────────────────────────────────────────────────────


async def detect_correction(plugin, event: AstrMessageEvent) -> None:
    """对方说「不对 / 你记错了」时，把刚用过的事实标成疑似记错。

    在消息进来时做，早于注入：这样本轮就不会再拿那条错的事实去回答他，
    而不是先答错再道歉——后者对信任的伤害更大。
    """
    try:
        if not plugin.facts.enabled():
            return
        if event.get_sender_id() == event.get_self_id():
            return
        content = (event.get_message_str() or "").strip()
        if not content or content.startswith("/"):
            return
        if not looks_like_correction(content):
            return
        changed = plugin.facts.mark_corrected(
            event.unified_msg_origin, event.get_sender_id()
        )
        if changed:
            event.set_extra(CORRECTION_EXTRA_KEY, changed)
    except Exception as e:  # noqa: BLE001 - 纠正检测失败不能影响正常回复
        logger.debug(f"[群聊记忆] 纠正检测失败（可忽略）: {e}")


# ── 工具接管 ────────────────────────────────────────────────────────────

def _ensure_toolset(req):
    """拿（必要时创建）本轮的 ToolSet。AstrBot 自己也是这么兜底的。"""
    tool_set = getattr(req, "func_tool", None)
    if tool_set is None:
        try:
            from astrbot.core.agent.tool import ToolSet

            tool_set = ToolSet()
            req.func_tool = tool_set
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[群聊记忆] 无法创建 ToolSet: {e}")
            return None
    return tool_set


def _remove_search_tools(req) -> None:
    """把 AstrBot 内置的搜索工具全摘掉。名字表来自官方源码，少一个就会漏摘。"""
    tool_set = getattr(req, "func_tool", None)
    if tool_set is None:
        return
    for name in BUILTIN_SEARCH_TOOL_NAMES:
        try:
            tool_set.remove_tool(name)
        except Exception:  # noqa: BLE001 - 不存在时是 no-op，不该影响其它工具
            pass


def apply_search_tools(plugin, req, session_id: str, active: bool) -> bool:
    """按配置接管搜索工具，返回本轮是否挂上了**插件自己的**搜索工具。

    几条硬规则：
    - 主动回复一律不搜（三种模式通吃）。插话为一句吐槽等 8 秒不值得，
      而且 AstrBot 不知道这轮是插话，全局的搜索工具得我们主动摘掉。
    - plugin 模式配了 key 但没配上（key 空/不支持的服务商）时，**连同内置的一起摘掉**。
      留着的后果是模型绕过配额照搜，那每日上限就成了摆设。
    """
    mode = str(plugin.settings.get("web_search_mode") or "plugin").strip().lower()

    if active:
        _remove_search_tools(req)
        return False

    if mode == "inherit":
        return False

    if mode == "off":
        _remove_search_tools(req)
        return False

    # plugin 模式
    allowed = True
    try:
        allowed = plugin.storage.search_allowed(session_id)
    except Exception:  # noqa: BLE001
        allowed = True
    if allowed is False:
        _remove_search_tools(req)
        return False

    try:
        remaining = plugin.usage.search_quota_left(session_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[群聊记忆] 读取搜索配额失败，按满额处理: {e}")
        remaining = 1

    if remaining <= 0:
        # 当天额度用完：根本不挂工具。模型连工具名都看不到，不会反复尝试
        _remove_search_tools(req)
        return False

    tool = build_search_tool(plugin, session_id)
    if tool is None:
        _remove_search_tools(req)
        return False

    tool_set = _ensure_toolset(req)
    if tool_set is None:
        return False
    try:
        tool_set.add_tool(tool)  # 同名覆盖内置那份
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[群聊记忆] 挂载搜索工具失败: {e}")
        return False


def apply_admin_tool(plugin, event, req, session_id: str) -> None:
    """给管理员挂对话调参工具。非管理员这里返回 None，工具压根不会出现。"""
    if not plugin.settings.get("admin_tool_enabled"):
        return
    try:
        tool = build_admin_tool(plugin, event, session_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[群聊记忆] 构建管理员工具失败: {e}")
        return
    if tool is None:
        return
    tool_set = _ensure_toolset(req)
    if tool_set is None:
        return
    try:
        tool_set.add_tool(tool)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[群聊记忆] 挂载管理员工具失败: {e}")


def _append_temp(req, text: str) -> None:
    """挂一段临时内容。``mark_as_temp`` 保证它不进对话历史。"""
    try:
        from astrbot.core.agent.message import TextPart

        req.extra_user_content_parts.append(TextPart(text=text).mark_as_temp())
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[群聊记忆] 注入失败（跳过本段）: {e}")


# ── 请求前：注入 ────────────────────────────────────────────────────────


def _append_facts(
    plugin,
    event,
    req,
    session_id: str,
    sender_id: str,
    tier: tuple[int, int, int] | None = None,
) -> None:
    """事实段 / 诚实出口 / 被纠正后的处理。三段互斥，一轮只出一段。

    顺序是有意的：纠正优先于一切——这一轮对方刚说你记错了，
    再拿记忆去说话就是明知故犯。

    ``tier`` 是关系档位给的配额，``None`` 表示不分档（按配置走）。
    """
    try:
        if not plugin.facts.enabled():
            return
        if event.get_extra(CORRECTION_EXTRA_KEY):
            _append_temp(req, CORRECTED_NOTE)
            return

        prompt = str(getattr(req, "prompt", "") or "")
        text = plugin.facts.build_text(
            session_id,
            sender_id,
            query=prompt,
            max_items=tier[1] if tier else None,
            max_chars=tier[2] if tier else None,
        )
        if text:
            _append_temp(req, text)
            return

        # 一条可注入的事实都没有：只有对方明确在问「还记得吗」时才提醒别编，
        # 平时每轮都插「你没记忆」会让模型变得什么都不敢接
        try:
            fallback = bool(plugin.settings.group("facts").get("honest_fallback"))
        except Exception:  # noqa: BLE001
            fallback = True
        if fallback and looks_like_recall(prompt):
            _append_temp(req, HONEST_NOTE)
    except Exception as e:  # noqa: BLE001 - 事实段失败不能影响回复
        logger.debug(f"[群聊记忆] 构建事实段失败（跳过本段）: {e}")


async def inject_style(plugin, event: AstrMessageEvent, req) -> None:
    """把学到的风格作为临时内容挂到本轮 LLM 请求上。"""
    if not plugin.settings.get("enabled"):
        return

    # 顺手清理平台 LTM 与重复段落
    cleaned = clean_system_prompt(req.system_prompt or "")
    if cleaned != (req.system_prompt or ""):
        req.system_prompt = cleaned

    session_id = event.unified_msg_origin
    sender_id = event.get_sender_id()
    active = is_active_turn(event)
    settings = plugin.settings

    # 工具接管放在注入判断之前：即使这个群关了风格注入，
    # 搜索配额和管理员工具该管的还得管
    attached = False
    if session_allowed(plugin, session_id, sender_id):
        attached = apply_search_tools(plugin, req, session_id, active)
        apply_admin_tool(plugin, event, req, session_id)

    # 会话级注入开关对「风格」和「关系」都生效：用户关掉某个群的注入，
    # 是希望这个群别被插件的话影响，不该只关一半。
    if not inject_allowed(plugin, session_id, sender_id):
        return

    injector = plugin.manager.injector
    # 关掉风格注入时 strength 记 0：没有风格可注入，也就不存在「让位」，
    # 关系段该照常挂上。
    strength = injector.strength_of(active) if settings.get("inject_enabled") else 0
    override_flag = False
    if strength > 0:
        try:
            override_flag = override_roll(
                strength, int(settings.get("style_override_threshold"))
            )
        except (TypeError, ValueError):
            override_flag = False

    # 关系段：独立于「风格注入」开关——用户关掉风格注入是想让 bot 别学群里
    # 那套说法，不代表不想让它记得和谁聊过，这是两件事。
    # 让位轮次不挂：那一轮的语义是「群风格优先」，再插一句亲疏关系会打架。
    if not override_flag:
        if is_serious_turn(plugin, req):
            # 命中就整段换掉。直接剥夺素材比「降低强度」有效——
            # 留着称呼和亲疏描述，模型多半还是会顺口用上。
            _append_temp(req, SERIOUS_NOTE)
        else:
            is_group = _is_group(event)
            # 档位只算一次，关系段和事实段共用：两段都按同一档取配额，
            # 否则会出现「关系段按陌生档给 120 字、事实段却按亲近给 5 条」
            try:
                tier = plugin.bond.memory_tier(sender_id, is_group=is_group)
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[群聊记忆] 读取记忆档位失败（按不分档处理）: {e}")
                tier = None
            try:
                relation = plugin.bond.build_text(sender_id, is_group=is_group)
            except Exception as e:  # noqa: BLE001 - 关系段失败不能影响回复
                logger.debug(f"[群聊记忆] 构建关系段失败（跳过本段）: {e}")
                relation = None
            if relation:
                _append_temp(req, relation)
            # 事实段紧跟在关系段后面：关系说「用什么语气」，事实说「知道什么」，
            # 两个标签分开，模型才分得清
            _append_facts(plugin, event, req, session_id, sender_id, tier)

    if not settings.get("inject_enabled"):
        return

    try:
        text = await injector.build_injection_text(
            session_id, user_message=req.prompt or "", active=active, override=override_flag
        )
    except Exception as e:  # noqa: BLE001 - 注入失败不能影响正常回复
        logger.warning(f"[群聊记忆] 构建注入内容失败: {e}")
        text = None
    if text:
        _append_temp(req, text)

    # 字数约束和风格分开挂：强度调到 0（纯人格）时这条照样生效
    try:
        length_rule = injector.build_length_rule(active)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[群聊记忆] 构建字数约束失败（可忽略）: {e}")
        length_rule = None
    if length_rule:
        _append_temp(req, length_rule)

    # 深度覆盖：让位命中时把声明也追加到 system prompt。
    # 代价是这一轮的 DeepSeek 前缀缓存必然失效（输入价差 30 倍），所以默认关。
    if override_flag and settings.get("style_override_deep"):
        try:
            req.system_prompt = (req.system_prompt or "") + "\n\n" + Injector.OVERRIDE_DECLARATION
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[群聊记忆] 深度覆盖失败: {e}")

    # 搜索提示语只在真的挂上了插件搜索工具时注入，否则等于白撩模型
    hint = str(settings.get("web_search_hint") or "").strip()
    if hint and attached:
        _append_temp(req, hint)


# ── 主动回复 ────────────────────────────────────────────────────────────


async def maybe_active_reply(plugin, event: AstrMessageEvent):
    """命中闸门就自己发一次请求。未命中什么都不做。

    这个函数是 async generator：命中时 ``yield event.request_llm(...)``，
    AstrBot 的 handler 机制会识别 ProviderRequest 并真正走一次 LLM 管线。
    风格注入因此自动生效，不需要在这里重复拼 prompt。
    """
    if not plugin.settings.get("enabled"):
        return
    if not plugin.settings.get("active_reply_enabled"):
        return

    try:
        info = await plugin.active.maybe_active(event)
    except Exception as e:  # noqa: BLE001 - 主动回复坏了不能影响正常聊天
        logger.warning(f"[群聊记忆] 主动回复判定异常: {e}")
        return
    if not info:
        return

    session_id = event.unified_msg_origin
    # 先打标记：inject_style 靠它改用主动档位、并且不挂搜索
    try:
        event.set_extra(
            ACTIVE_EXTRA_KEY,
            {"mode": info.get("mode", ""), "reason": info.get("reason", "")},
        )
        # 掐掉宿主原生的群聊回复，避免双回复。必须在 request_llm 之前调。
        event.should_call_llm(False)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[群聊记忆] 设置主动回复标记失败: {e}")
        return

    logger.info(
        f"[群聊记忆] 主动回复 · {session_id} · "
        f"mode={info.get('mode')} reason={info.get('reason')}"
    )
    try:
        yield event.request_llm(
            prompt=str(info.get("prompt") or ""),
            conversation=info.get("conversation"),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[群聊记忆] 主动回复请求失败: {e}")


# ── 回复后：记账与截断 ──────────────────────────────────────────────────


async def apply_tone(plugin, event: AstrMessageEvent) -> None:
    """改已经生成好的回复：去模型腔、可选压标点/去 markdown/拆条。

    这里动的是「模型已经吐完的那条消息」，不参与任何 LLM 调用，所以零成本。
    失败就原样发出去——不能让一层美化把正常回复搞没。
    """
    try:
        cfg = plugin.settings.group("tone")
        if not any(cfg.get(k) for k in ("clean", "squeeze", "markdown", "segment")):
            return

        result = event.get_result()
        chain = getattr(result, "chain", None) if result is not None else None
        if not chain:
            return

        # 只改模型生成的回复。/关系 /风格状态 这类指令输出是结构化文本，
        # 压标点会把它弄乱
        try:
            if not result.is_model_result():
                return
        except Exception:  # noqa: BLE001 - 判不出来就别动
            return

        for comp in chain:
            body = getattr(comp, "text", None)
            if isinstance(body, str) and body.strip():
                cleaned = clean_text(body, cfg)
                if cleaned != body:
                    comp.text = cleaned

        if not cfg.get("segment"):
            return
        # 只在「整条就一个纯文本」时拆。带图片/语音/At 的一拆组件顺序就乱了
        if len(chain) != 1 or not isinstance(chain[0], Plain):
            return
        parts = split_segments(
            chain[0].text,
            int(cfg.get("segment_chars") or 60),
            int(cfg.get("segment_parts") or 4),
        )
        if len(parts) > 1:
            result.chain = [Plain(p) for p in parts]
    except Exception as e:  # noqa: BLE001 - 输出层失败不能影响发送
        logger.warning(f"[群聊记忆] 输出层处理失败（已跳过）: {e}")


async def record_llm_usage(plugin, event: AstrMessageEvent, response) -> None:
    """记一次全机消耗，并按「谁引起的」分来源。

    分三种，因为这三笔钱的归属完全不同：

    - ``active``：插件自己插话，可以整块关掉，是唯一「不关也有人花钱」的来源；
    - ``reply``：被 @、被回复、私聊——有人点名要它说话；
    - ``bot``：其余回调（例如把群聊配成全部回复），算不清就落在这里。

    多轮工具调用中间的请求会漏计，所以每个来源都是下限值。
    """
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        if is_active_turn(event):
            source = "active"
        elif is_direct_to_bot(event) or not _is_group(event):
            source = "reply"
        else:
            source = "bot"
        plugin.usage.record(source, usage, event.unified_msg_origin)
    except Exception as e:  # noqa: BLE001 - 记账失败不能影响回复
        logger.debug(f"[群聊记忆] 记录全机消耗失败（可忽略）: {e}")


async def apply_feed_note(plugin, event: AstrMessageEvent) -> None:
    """把「好感 +N」拼进「吃什么」那条**还没发出去**的回复里。

    为什么非得在这一步：``after_message_sent`` 触发时消息已经出去了，
    再想改内容只能补发第二条——那就变成两条回复，用户明确说过要一条。
    decorating 阶段拿到的还是链里的结果，往最后一个文本组件末尾拼一句，
    看到的就是和菜名、图片同一条消息。

    结算也在这里做（同一份逻辑，带 event 级去重标记），所以回复里写的
    数字一定就是实际加进去的数字；``after_message_sent`` 那边再调会因为
    标记直接跳过，不会记两遍。

    加不上就什么都不做：比分错一次账，一条回复难看是更小的事。
    """
    # 先把「这条消息是不是投喂」问清楚：是投喂又没加成，才值得记一行日志。
    # 不先问的话，所有消息都会走到 settle 里被静默拒绝，出问题时看不出卡在哪
    try:
        payload = event.get_extra(FEED_EXTRA_KEY)
    except Exception:  # noqa: BLE001
        payload = None
    if not isinstance(payload, dict):
        return
    try:
        result = event.get_result()
        raw = getattr(result, "chain", None) if result is not None else None
        if not raw:
            logger.info("[群聊记忆] 投喂提示：这条消息没有结果链，跳过")
            return
        info = settle_feed(plugin, event)
        if not info:
            logger.info(
                "[群聊记忆] 投喂提示：这次没加成分（已结算过 / 加成设成 0 / 没开喂食）"
            )
            return
        note = build_note(info.get("gain"), info.get("reason") or "")
        if not note:
            return
        # 重建一份再赋值，而不是原地改组件：result.chain 有可能是 getter
        # 返回的浅拷贝，改了不写回去等于没改
        chain = list(raw)
        for index in range(len(chain) - 1, -1, -1):
            body = getattr(chain[index], "text", None)
            if isinstance(body, str) and body.strip():
                chain[index] = Plain(body.rstrip() + note)
                result.chain = chain
                logger.info(f"[群聊记忆] 投喂提示已拼进回复 · {note}")
                return
        # 整条链里没有文字（纯图片），只能补一个组件
        chain.append(Plain(note))
        result.chain = chain
        logger.info(f"[群聊记忆] 投喂提示单独成段 · {note}")
    except Exception as e:  # noqa: BLE001 - 提示加不上不能影响这条回复
        logger.warning(f"[群聊记忆] 追加好感提示失败: {e}")


def _cut_at_sentence_end(text: str, keep: int) -> str:
    """切到最近一个句末标点；找不到就硬切。"""
    if keep <= 0:
        return ""
    head = text[:keep]
    for i in range(len(head) - 1, -1, -1):
        if head[i] in "。！？!?…":
            return head[: i + 1]
    return head


async def truncate_result(plugin, event: AstrMessageEvent) -> None:
    """按字数硬截断结果链（默认关）。

    只在用户明确开了 ``reply_max_chars_hard`` 时生效。切坏 markdown / 链接是已知代价，
    面板和 README 都写明「宁可断也不能长」才用。
    """
    try:
        if not plugin.settings.get("reply_max_chars_hard"):
            return
        active = is_active_turn(event)
        key = "active_reply_max_chars" if active else "reply_max_chars"
        limit = int(plugin.settings.get(key) or 0)
        if limit <= 0:
            return

        result = event.get_result()
        chain = getattr(result, "chain", None)
        if not chain:
            return

        used = 0
        for part in chain:
            body = getattr(part, "text", None)
            if not isinstance(body, str) or not body:
                continue
            if used + len(body) <= limit:
                used += len(body)
                continue
            part.text = _cut_at_sentence_end(body, limit - used)
            used = limit
            break
    except Exception as e:  # noqa: BLE001 - 截断失败就按原文发出去
        logger.warning(f"[群聊记忆] 字数截断失败（已跳过）: {e}")

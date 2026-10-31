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

from .core.admin import build_admin_tool
from .core.injector import Injector, override_roll
from .core.search import BUILTIN_SEARCH_TOOL_NAMES, build_search_tool
from .utils import clean_system_prompt

# 主动回复标记：inject_style 靠它区分「这轮是插话还是被叫」
ACTIVE_EXTRA_KEY = "qunliao_active_reply"


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
    """是否收集这个会话的消息用于学习（全局过滤 + 会话级开关）。"""
    return session_allowed(plugin, session_id, sender_id) and plugin.storage.learn_allowed(
        session_id
    )


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

    plugin.storage.add_message(
        session_id,
        {
            "sender": event.get_sender_name() or event.get_sender_id(),
            "content": content[:1000],
            "timestamp": time.time(),
        },
        limit=int(plugin.settings.get("history_limit_per_session")),
    )


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

    if not settings.get("inject_enabled"):
        return
    if not inject_allowed(plugin, session_id, sender_id):
        return

    injector = plugin.manager.injector
    strength = injector.strength_of(active)
    override_flag = False
    if strength > 0:
        try:
            override_flag = override_roll(
                strength, int(settings.get("style_override_threshold"))
            )
        except (TypeError, ValueError):
            override_flag = False

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


async def record_llm_usage(plugin, event: AstrMessageEvent, response) -> None:
    """记一次全机消耗。多轮工具调用中间的请求会漏计，所以这是下限值。"""
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        plugin.usage.record("bot", usage, event.unified_msg_origin)
    except Exception as e:  # noqa: BLE001 - 记账失败不能影响回复
        logger.debug(f"[群聊记忆] 记录全机消耗失败（可忽略）: {e}")


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

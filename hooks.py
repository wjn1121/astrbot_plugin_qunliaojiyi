"""AstrBot 事件钩子。

只放「消息进来」和「请求出去」两件事的实现，装饰器薄壳留在 main.py——
AstrBot 靠类方法反射收集 handler，方法必须在 Star 子类上定义。
"""

from __future__ import annotations

import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .utils import clean_system_prompt


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


async def inject_style(plugin, event: AstrMessageEvent, req) -> None:
    """把学到的风格作为临时内容挂到本轮 LLM 请求上。"""
    if not plugin.settings.get("enabled"):
        return

    # 顺手清理平台 LTM 与重复段落
    cleaned = clean_system_prompt(req.system_prompt or "")
    if cleaned != (req.system_prompt or ""):
        req.system_prompt = cleaned

    session_id = event.unified_msg_origin
    if not inject_allowed(plugin, session_id, event.get_sender_id()):
        return
    if not plugin.settings.get("inject_enabled"):
        return

    try:
        text = await plugin.manager.injector.build_injection_text(
            session_id, user_message=req.prompt or ""
        )
    except Exception as e:  # noqa: BLE001 - 注入失败不能影响正常回复
        logger.warning(f"[群聊记忆] 构建注入内容失败: {e}")
        return
    if not text:
        return

    try:
        from astrbot.core.agent.message import TextPart

        req.extra_user_content_parts.append(TextPart(text=text).mark_as_temp())
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[群聊记忆] 注入失败（跳过本轮）: {e}")

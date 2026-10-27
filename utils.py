"""与 AstrBot 无关的纯工具函数（便于单测和复用）。"""

from __future__ import annotations

import re

from astrbot.api import logger

# 平台 LTM 注入块：注入前剥离，避免和本插件内容重复
_LTM_PATTERNS = [
    re.compile(
        (
            r"You are now in a chatroom\. The chat history is as follows:\s*\n?"
            r"(?:\[[^\]]+/\d{2}:\d{2}:\d{2}\]:.*(?:\n(?!---\n).*)*)"
            r"(?:\n---\n\[[^\]]+/\d{2}:\d{2}:\d{2}\]:.*(?:\n(?!---\n).*)*)*"
        ),
        re.IGNORECASE,
    ),
    re.compile(
        (
            r"You are now in a chatroom\. The chat history is as follows:\s*"
            r"[\s\S]*?Now, a new message is coming:\s*`[\s\S]*?`\."
            r"\s*Please react to it\."
        ),
        re.IGNORECASE,
    ),
]


def clean_system_prompt(text: str) -> str:
    """剥离平台 LTM 注入并按段落去重，避免 prompt 膨胀。"""
    cleaned = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    for pattern in _LTM_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    parts, seen = [], set()
    for part in cleaned.split("\n\n"):
        part = part.strip()
        fingerprint = re.sub(r"\s+", " ", part).lower()
        if not part or fingerprint in seen:
            continue
        seen.add(fingerprint)
        parts.append(part)
    return "\n\n".join(parts)


def display_name(session_id: str) -> str:
    """把 unified_msg_origin 转成好读的名字。"""
    try:
        parts = (session_id or "").split(":")
        if len(parts) >= 3:
            kind = "群" if parts[1].lower().startswith("group") else "私聊"
            return f"{kind} {parts[2]}"
    except Exception:  # noqa: BLE001
        logger.debug(f"[群聊记忆] 解析会话名失败: {session_id}")
    return session_id or "未知会话"

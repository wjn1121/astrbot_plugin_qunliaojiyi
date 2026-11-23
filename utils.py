"""与 AstrBot 无关的纯工具函数（便于单测和复用）。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from astrbot.api import logger

# 容器时区通常是 UTC，「几点」是给用户作息看的，必须显式用北京时间
BEIJING_TZ = timezone(timedelta(hours=8))

# DeepSeek 的高峰时段（北京时间，周一到周五）：这两段按 2 倍计价。
# 只读不写，真正的价格倍数在 settings 里，这里只用来判断「现在贵不贵」
PEAK_RANGES = ((9, 12), (14, 18))

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


def is_peak_time(now: datetime | None = None) -> bool:
    """现在是不是高峰计价时段。周末全天空闲价。

    和 ``core/usage.py`` 的 ``peak_multiplier`` 用同一份区间：那边算钱，
    这边决定「要不要把后台任务往后挪」。两处必须一致，否则会出现
    「账单按 2 倍算、调度却以为现在是空闲价」这种自相矛盾。
    """
    moment = now or datetime.now(BEIJING_TZ)
    if moment.weekday() >= 5:  # 周六周日全天空闲价
        return False
    hour = moment.hour
    return any(start <= hour < end for start, end in PEAK_RANGES)


def trim_contexts(ctx: list, limit: int) -> list | None:
    """把会话历史裁到最近 ``limit`` 条，返回新列表；不用裁就返回 None。

    纯函数，不碰任何外部状态，方便断言。两条规则：

    1. ``system`` 消息一律保留——人格设定、工具说明都在里面，
       裁掉等于换了个人格；
    2. 裁完不能以 ``tool`` / ``assistant`` 开头。前者会失去对应的调用方，
       后者没有上文，很多 provider 会直接报格式错。
    """
    if not isinstance(ctx, list) or limit <= 0 or len(ctx) <= limit:
        return None

    head: list = []
    rest: list = []
    for msg in ctx:
        role = str(msg.get("role") or "") if isinstance(msg, dict) else ""
        if role == "system":
            head.append(msg)
        else:
            rest.append(msg)

    kept = rest[-limit:]
    while kept:
        first = kept[0]
        role = str(first.get("role") or "") if isinstance(first, dict) else ""
        if role in ("tool", "assistant"):
            kept.pop(0)
            continue
        break
    if not kept:
        return None
    return head + kept


def defer_learning(mode: str, peak_enabled: bool, now: datetime | None = None) -> bool:
    """这次该不该因为高峰价把学习往后推。

    ``mode`` 是 ``learn_time_mode``：``anytime`` 攒够就学，``off_peak`` 只在
    非高峰学。峰谷计价关掉时推迟不省钱，所以直接放行。
    """
    if str(mode or "off_peak").strip().lower() != "off_peak":
        return False
    if not peak_enabled:
        return False
    return is_peak_time(now)

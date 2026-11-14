"""学习开关的判定。群聊和私聊分开管。

**为什么单独一个模块**：这个判断有两个入口，必须一致——

- 回复链路：``hooks.learn_allowed``（决定这条消息要不要收进 chat_history）
- 后台调度：``core.manager`` 的两个候选筛选（决定哪些会话要送去提炼）

两处各写一遍的结果，就是「面板上关了某个群，定时任务还在学」。
1.3.0 已经踩过一次（那次是漏了会话级开关），所以这次从源头做成一份。

判定顺序（从强到弱）：

1. **会话级手动开关**：用户在某个会话里明确说过「学 / 别学」，那就听他的
2. **私聊**：默认不学。私聊是私域，默认把别人一对一聊天送进模型提炼，
   边界上不合适；**主人的私聊例外**（他自己的号，自己开的口子）
3. **群聊**：默认学。进群就学是这个插件的主功能，默认关等于装了个寂寞
"""

from __future__ import annotations

from typing import Any


def is_private_session(session_id: str) -> bool:
    """会话是不是私聊。

    按 unified_msg_origin 的第二段判：``GroupMessage`` / ``FriendMessage``。
    判不出来时按**群聊**算——群聊默认开，判错最多是多学一个私聊；
    反过来判错就是把别人的私聊送去提炼，这个方向不能错。
    """
    parts = str(session_id or "").split(":")
    if len(parts) < 2:
        return False
    return not parts[1].strip().lower().startswith("group")


def owner_of(session_id: str) -> str:
    """从私聊 umo 里取对方的 QQ 号。

    ``bot1:FriendMessage:759982326`` → ``759982326``。
    后台调度手里只有会话 id，没有 event，所以得从 id 里反推。
    """
    parts = str(session_id or "").split(":")
    return parts[-1].strip() if len(parts) >= 3 else ""


def learn_enabled(
    settings: Any,
    storage: Any,
    session_id: str,
    sender_id: str = "",
) -> bool:
    """这个会话现在要不要收集消息用于学习。

    ``sender_id`` 传不进来时会从 session_id 反推（后台调度就是这种情况）。
    """
    # 1) 会话级手动开关优先：用户明确的选择盖过一切默认值
    try:
        override = storage.learn_override(session_id)
    except Exception:  # noqa: BLE001
        override = None
    if override is not None:
        return bool(override)

    if is_private_session(session_id):
        # 2) 私聊：全局开过就学
        try:
            if settings.get("learn_private_enabled"):
                return True
            uid = str(sender_id or "").strip() or owner_of(session_id)
            return bool(uid) and uid in settings.owners()
        except Exception:  # noqa: BLE001
            return False

    # 3) 群聊：默认学
    # 注意 ``Settings.get`` 只接受一个参数（不是 dict.get），
    # 传第二个默认值会 TypeError，被这里吞成「开启」——开关就永远关不掉
    try:
        return bool(settings.get("learn_group_enabled"))
    except Exception:  # noqa: BLE001
        return True

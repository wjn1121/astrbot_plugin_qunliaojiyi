"""管理员对话调参：让群里的管理员直接用一句话改配置、加记忆、清数据。

两条安全原则：

1. **非管理员连工具都看不到**。``build_admin_tool`` 在不满足条件时返回 None，
   调用方（hooks.py）据此根本不挂载工具。只在 ``call()`` 里校验是不够的——
   工具一旦出现在模型的可用列表里，别人就可以用 prompt 注入诱导模型去调它；
   列表里压根没有这个工具，注入就无从下手。
2. **``call()`` 里再校验一次**。挂载时判断是「预防」，调用时判断是「兜底」：
   会话管理员变了、配置热更新了，都可能让挂载时的判断过期。

所有返回值都是给模型看的字符串，失败返回 ``"Error: <中文原因>"``，
异常一律吞掉——工具坏了最多是这次指令没生效，不能让整轮对话崩掉。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

from .settings import RUNTIME_KEYS

try:  # AstrBot 不同版本的模块路径变过，导入失败时用空壳兜底，避免整个插件起不来
    from astrbot.core.agent.tool import FunctionTool
except Exception:  # noqa: BLE001  # pragma: no cover

    class FunctionTool:  # type: ignore[no-redef]
        """占位实现：真正的工具类不可用时，插件其他功能仍然能正常跑。"""

        def __init__(self, **_kwargs) -> None:
            pass


TOOL_NAME = "qunliao_admin"

TOOL_DESCRIPTION = (
    "Manage this group's chat-memory plugin. Can read current status, change runtime "
    "settings, add or delete manual memory entries, clear stored data, trigger a "
    "learning pass, or toggle proactive replies. Admin only."
)

# ``scope`` 的顺序就是危险程度：all 放最后，且必须显式传值才执行。
CLEAR_SCOPES = ("learned", "manual", "history", "active", "all")

TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "status",
                "set_setting",
                "add_memory",
                "delete_memory",
                "clear",
                "learn_now",
                "set_active",
            ],
            "description": "Which operation to perform.",
        },
        "setting_key": {
            "type": "string",
            "description": "For set_setting: the runtime setting name, e.g. style_strength.",
        },
        "setting_value": {
            "type": ["string", "number", "boolean"],
            "description": "For set_setting: the new value.",
        },
        "content": {
            "type": "string",
            "description": "For add_memory: the memory text to remember.",
        },
        "index": {
            "type": "integer",
            "description": "For delete_memory: zero-based index of the manual memory entry.",
        },
        "scope": {
            "type": "string",
            "enum": list(CLEAR_SCOPES),
            "description": "For clear: which part to delete. 'all' must be passed explicitly.",
        },
        "enabled": {
            "type": "boolean",
            "description": "For set_active: whether proactive replies are allowed in this group.",
        },
    },
    "required": ["action"],
}


def _tool_result(text: str):
    """把纯文本包成 AstrBot 期望的工具返回值。

    新版走 MCP 的 ``CallToolResult``，老版本/导入失败时直接返回字符串，
    两条路都能让模型拿到结果。
    """
    try:
        import mcp.types as mcp_types

        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text=text)], isError=False
        )
    except Exception:  # noqa: BLE001
        return text


def _collect_admin_ids(plugin, event) -> set[str]:
    """AstrBot 全局管理员 + 本插件额外名单，合成一个字符串集合。"""
    ids: set[str] = set()

    try:
        for item in plugin.settings.get("admin_extra_users") or []:
            text = str(item or "").strip()
            if text:
                ids.add(text)
    except Exception as e:  # noqa: BLE001 - 读不到插件名单只是少了几个管理员
        logger.debug(f"[群聊记忆] 读取 admin_extra_users 失败: {e}")

    context = getattr(plugin, "context", None)
    if context is None:
        return ids
    try:
        umo = getattr(event, "unified_msg_origin", None)
        try:
            cfg = context.get_config(umo=umo)
        except TypeError:
            # 老版本签名不带 umo
            cfg = context.get_config()
        raw = cfg.get("admins_id") if hasattr(cfg, "get") else None
        if isinstance(raw, str):
            raw = [raw]
        for item in raw or []:
            text = str(item or "").strip()
            if text:
                ids.add(text)
    except Exception as e:  # noqa: BLE001 - 全局配置读不到就用插件自己的名单
        logger.debug(f"[群聊记忆] 读取全局 admins_id 失败: {e}")
    return ids


def is_admin(plugin, event) -> bool:
    """当前发言人是否管理员：AstrBot 全局 ``admins_id`` + 插件 ``admin_extra_users``。"""
    try:
        uid = str(event.get_sender_id() or "").strip()
    except Exception:  # noqa: BLE001
        return False
    if not uid:
        return False
    return uid in _collect_admin_ids(plugin, event)


def build_admin_tool(plugin, event, session_id: str) -> object | None:
    """按需构建管理员工具。

    返回 None 表示「不要挂载」：非管理员群里，模型连这个工具的存在都看不到。
    """
    try:
        if not plugin.settings.get("admin_tool_enabled"):
            return None
        if not is_admin(plugin, event):
            return None
        return _AdminTool(plugin, event, session_id)
    except Exception as e:  # noqa: BLE001 - 装配失败就不挂，宁可不给也不能给错人
        logger.warning(f"[群聊记忆] 构建管理员工具失败（已跳过）: {e}")
        return None


class _AdminTool(FunctionTool):
    """管理员工具本体。名字/描述/参数在类外定义，方便 api.py 复用同一份 schema。"""

    def __init__(self, plugin, event, session_id: str) -> None:
        super().__init__(
            name=TOOL_NAME, description=TOOL_DESCRIPTION, parameters=TOOL_PARAMETERS
        )
        self.plugin = plugin
        self.session_id = session_id
        # 留着 event 是为了在 call() 里重新校验身份：挂载时的判断会过期
        self._sender_event = event

    async def call(self, context=None, **kwargs):
        """执行一次管理操作。

        参数用 ``**kwargs`` 收：模型可能多传也可能漏传，缺的参数按 None 处理，
        在执行体里逐项校验，比让签名校验直接抛 TypeError 更好定位问题。
        """
        # 双保险：挂载时的判断可能因为配置热更新而过期
        try:
            if self._sender_event is None or not is_admin(self.plugin, self._sender_event):
                uid = "未知"
                if self._sender_event is not None:
                    try:
                        uid = self._sender_event.get_sender_id()
                    except Exception:  # noqa: BLE001
                        pass
                logger.warning(f"[群聊记忆] 越权调用管理员工具被拒绝: {uid}")
                return _tool_result("Error: permission denied")
        except Exception:  # noqa: BLE001 - 校验本身出错也按拒绝处理
            return _tool_result("Error: permission denied")

        try:
            text = await self._dispatch(**kwargs)
        except Exception as e:  # noqa: BLE001 - 工具异常不能抛给模型
            logger.error(f"[群聊记忆] 管理员工具执行失败: {e}")
            text = f"Error: 执行失败（{e}）"
        return _tool_result(text)

    # ── 分发 ────────────────────────────────────────────────────────────

    async def _dispatch(self, **kwargs) -> str:
        action = str(kwargs.get("action") or "").strip()
        if action == "status":
            return admin_summary(self.plugin, self.session_id)
        if action == "set_setting":
            return await self._set_setting(kwargs.get("setting_key"), kwargs.get("setting_value"))
        if action == "add_memory":
            return await self._add_memory(kwargs.get("content"))
        if action == "delete_memory":
            return await self._delete_memory(kwargs.get("index"))
        if action == "clear":
            return await self._clear(kwargs.get("scope"))
        if action == "learn_now":
            return await self._learn_now()
        if action == "set_active":
            return self._set_active(kwargs.get("enabled"))
        return f"Error: 不支持的操作 {action or '（空）'}"

    async def _set_setting(self, key: Any, value: Any) -> str:
        name = str(key or "").strip()
        if not name:
            return "Error: 缺少 setting_key"
        if name not in RUNTIME_KEYS:
            return "Error: 不支持修改这个配置项"
        if value is None:
            return "Error: 缺少 setting_value"
        # set_runtime 会做类型规范化，抛 KeyError 表示不在白名单（上面已挡过，
        # 这里仍按失败处理，防止以后白名单换成别的判断方式）
        try:
            normalized = await self.plugin.settings.set_runtime(name, value)
        except KeyError:
            return "Error: 不支持修改这个配置项"
        return f"已把 {name} 改为 {normalized}"

    async def _add_memory(self, content: Any) -> str:
        text = str(content or "").strip()
        if not text:
            return "Error: 缺少 content"
        # 真实签名：add_manual_trait(session_id, content, *, category, context, triggers, proficiency)
        # -> (条目 | None, 错误原因)
        trait, error = await self.plugin.storage.add_manual_trait(
            self.session_id, text
        )
        if error:
            return f"Error: {error}"
        return f"已添加手动记忆：{(trait or {}).get('content', text)}"

    async def _delete_memory(self, index: Any) -> str:
        try:
            position = int(index)
        except (TypeError, ValueError):
            return "Error: 需要提供要删除的序号 index"
        manual = self.plugin.storage.manual_of(self.session_id)
        if position < 0 or position >= len(manual):
            return f"Error: 序号超出范围（当前 {len(manual)} 条手动记忆）"
        content = str(manual[position].get("content") or "")
        if not content:
            return "Error: 该条记忆内容为空"
        # 真实签名是 delete_trait(session_id, content, source=None)，按内容删，
        # 所以这里先把 index 翻成 content；source 固定 manual，免得误删学习产出
        removed = await self.plugin.storage.delete_trait(
            self.session_id, content, "manual"
        )
        if not removed:
            return "Error: 删除失败，该条记忆可能已被移除"
        return f"已删除手动记忆：{content}"

    async def _clear(self, scope: Any) -> str:
        target = str(scope or "").strip()
        if not target:
            # 不给默认值：清空是不可恢复操作，必须由调用方明确指定范围
            return "Error: 需要显式指定 scope（learned/manual/history/active/all）"
        if target not in CLEAR_SCOPES:
            return "Error: 不支持的 scope"
        storage = self.plugin.storage
        if target == "history":
            await storage.clear_history(self.session_id)
            return "已清空本群待学习消息"
        if target == "active":
            storage.clear_active(self.session_id)
            return "已重置本群主动回复状态"
        if target == "all":
            # all 会连历史一起清，所以这里把另外两部分补齐，让语义和面板一致
            await storage.clear_session(self.session_id, "all")
            storage.clear_active(self.session_id)
            return "已清空本群全部数据（学习风格 / 手动记忆 / 待学习消息 / 主动状态）"
        await storage.clear_session(self.session_id, target)
        label = "学习风格" if target == "learned" else "手动记忆"
        return f"已清空本群{label}"

    async def _learn_now(self) -> str:
        result = await self.plugin.manager.learn_now(self.session_id)
        if not isinstance(result, dict):
            return "Error: 学习返回了意外结果"
        if not result.get("ok"):
            return f"Error: {result.get('reason') or '学习失败'}"
        return (
            f"学习完成：稳定风格 {int(result.get('stable') or 0)} 条，"
            f"场景化表达 {int(result.get('situational') or 0)} 条"
        )

    def _set_active(self, enabled: Any) -> str:
        if not isinstance(enabled, bool):
            return "Error: 需要提供 enabled 真假值"
        active = getattr(self.plugin, "active", None)
        if active is None:
            return "Error: 主动回复模块未加载"
        status = active.set_enabled(self.session_id, enabled)
        state = "开启" if enabled else "关闭"
        today = f"，今日已用 {status.get('today_count', 0)}/{status.get('daily_limit', 0)}"
        return f"已{state}本群主动回复{today}"


# ── 状态摘要 ────────────────────────────────────────────────────────────


def admin_summary(plugin, session_id: str) -> str:
    """本群状态摘要，给 ``status`` 动作和面板复用。

    每一块都独立 try：某一块数据取不到（比如还没接 usage 模块）时，
    只是少一行，不会让整段摘要变成一句报错。
    """
    try:
        lines = ["本群状态："]
        lines.extend(_style_lines(plugin, session_id))
        lines.extend(_active_lines(plugin, session_id))
        lines.extend(_search_lines(plugin, session_id))
        lines.extend(_cost_lines(plugin, session_id))
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001 - 摘要失败不能让工具整体失败
        logger.error(f"[群聊记忆] 生成管理员摘要失败: {e}")
        return f"Error: 读取本群状态失败（{e}）"


def _style_lines(plugin, session_id: str) -> list[str]:
    settings = plugin.settings
    injector = getattr(getattr(plugin, "manager", None), "injector", None)
    summary: dict[str, Any] = {}
    if injector is not None:
        try:
            summary = injector.summary(session_id) or {}
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[群聊记忆] 读取注入摘要失败: {e}")

    strength = summary.get("style_strength")
    if strength is None:
        strength = settings.get("style_strength")
    threshold = summary.get("override_threshold")
    if threshold is None:
        threshold = settings.get("style_override_threshold")
    probability = summary.get("override_probability")
    if probability is None and injector is not None:
        try:
            probability = injector.override_probability(int(strength or 0))
        except Exception:  # noqa: BLE001
            probability = 0
    try:
        strength = int(strength or 0)
        threshold = int(threshold or 0)
        probability = int(probability or 0)
    except (TypeError, ValueError):
        strength, threshold, probability = 0, 0, 0

    learned = int(summary.get("stable_count", 0) or 0) + int(
        summary.get("situational_count", 0) or 0
    )
    manual = int(summary.get("manual_count", 0) or 0)
    # 0% 说成「不让位」更直观：0% 容易被读成「算出来是 0，可能有 bug」
    override_text = f"让位概率 {probability}%" if probability > 0 else "不让位"
    return [
        f"风格强度 {strength}（阈值 {threshold}，{override_text}）",
        f"学习特征 {learned} 条 · 手动记忆 {manual} 条",
    ]


def _active_lines(plugin, session_id: str) -> list[str]:
    active = getattr(plugin, "active", None)
    if active is None:
        return ["主动回复 模块未加载"]
    try:
        status = active.status(session_id)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[群聊记忆] 读取主动回复状态失败: {e}")
        return ["主动回复 状态读取失败"]
    if not status.get("enabled"):
        return ["主动回复 已关闭"]
    line = (
        f"主动回复 今日 {int(status.get('today_count') or 0)}"
        f"/{int(status.get('daily_limit') or 0)}"
    )
    wait = status.get("next_roll_in")
    if isinstance(wait, (int, float)) and wait > 0:
        line += f" · 下次评估 {max(1, int(wait) // 60)} 分钟后"
    else:
        line += " · 下次评估 随时"
    return [line]


def _search_lines(plugin, session_id: str) -> list[str]:
    settings = plugin.settings
    mode = str(settings.get("web_search_mode") or "").strip()
    if mode == "off":
        return ["联网搜索 已关闭"]
    if mode == "inherit":
        return ["联网搜索 交给 AstrBot（插件不计数）"]
    usage = getattr(plugin, "usage", None)
    if usage is None:
        return ["联网搜索 无统计数据"]
    try:
        used = int(usage.search_used(session_id))
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[群聊记忆] 读取搜索次数失败: {e}")
        return ["联网搜索 状态读取失败"]
    limit = int(settings.get("web_search_daily_limit") or 0)
    return [f"联网搜索 今日 {used}/{limit}"]


def _cost_lines(plugin, session_id: str) -> list[str]:
    usage = getattr(plugin, "usage", None)
    if usage is None:
        return []
    try:
        today = usage.today() or {}
        total = float(today.get("cost") or 0.0)
        session = (today.get("by_session") or {}).get(session_id) or {}
        # 本群金额按当前牌价从 token 反算：usage 只落全天的总账单，
        # 分群金额再按比例拆会把跨天的调用算错，反算反而更稳。
        own = 0.0
        cost_of = getattr(usage, "cost_of", None)
        if callable(cost_of) and session:
            own = float(cost_of({"tokens": session}) or 0.0)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[群聊记忆] 读取消耗失败: {e}")
        return []
    return [f"今日消耗 ¥{own:.2f}（全机 ¥{total:.2f}）"]

# Copyright (C) 2026 wjn1121
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# 本插件参考 astrbot_plugin_oven_multi（AGPL-3.0，作者 汐兮雨）旧版的
# style_learning 实现重写，保留其「低频批量提炼 + 临时内容注入」的核心思路。

"""群聊记忆：从群聊中学习说话风格，低频提炼，按语境注入。

成本模型（与烤箱之心新版的区别就在这三点）：
1. 不做「每条消息一次 LLM 筛选」——注入只读本地数据，零网络请求；
2. 学习是每 N 小时一次的整批提炼，不是每 10 分钟一轮的定时任务；
3. 攒不够消息就不发请求。

本文件只做装配：数据/调度在 core/，接口在 api.py，事件钩子在 hooks.py。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from . import api
from .constants import PLUGIN_AUTHOR, PLUGIN_DESC, PLUGIN_NAME, PLUGIN_VERSION
from .core import Manager, Settings, Storage
from .core.active import ActiveReply
from .core.admin import admin_summary, is_admin
from .core.usage import UsageTracker
from .hooks import (
    collect_message,
    inject_style,
    maybe_active_reply,
    record_llm_usage,
    truncate_result,
)

# 二次确认的有效期：超过这个时间就得重新发起，避免几小时前的确认突然生效
_CONFIRM_TTL = 60.0


@register(PLUGIN_NAME, PLUGIN_AUTHOR, PLUGIN_DESC, PLUGIN_VERSION)
class QunliaoJiyi(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.plugin_name = PLUGIN_NAME

        self.plugin_dir = Path(os.path.dirname(os.path.abspath(__file__)))
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.settings = Settings(config, self.data_dir / "runtime.json")
        self.storage = Storage(self.data_dir)
        # usage 要在 manager 之前：learner 学习完就调 plugin.usage.record
        self.usage = UsageTracker(self.data_dir, self.settings)
        self.manager = Manager(self, self.storage, self.settings)
        self.active = ActiveReply(self, self.storage, self.settings)

        # 待确认的清空操作：{session_id: (scope, 发起时间)}
        self._pending_clear: dict[str, tuple[str, float]] = {}

        api.register(self)
        logger.info(f"[群聊记忆] 初始化完成，数据目录 {self.data_dir}")

    # ── 生命周期 ────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        if self.settings.get("enabled"):
            self.manager.start()
        logger.info("[群聊记忆] 已启动")

    async def terminate(self) -> None:
        await self.manager.stop()
        try:
            await self.usage.flush()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[群聊记忆] 关闭时写消耗记录失败: {e}")
        logger.info("[群聊记忆] 已停止")

    # ── 事件钩子（装饰器必须在类上，实现委托给 hooks.py）────────────────

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        """收集消息，顺便看看要不要主动插话。

        必须是 async generator：命中主动回复时要 ``yield event.request_llm(...)``，
        普通 async 函数接不住这个返回值。
        """
        await collect_message(self, event)
        async for result in maybe_active_reply(self, event):
            yield result

    @filter.on_llm_request(priority=17)
    async def on_llm_request(self, event: AstrMessageEvent, req):
        await inject_style(self, event, req)

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response):
        await record_llm_usage(self, event, response)

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        await truncate_result(self, event)

    # ── 命令 ────────────────────────────────────────────────────────────

    @filter.command("风格状态")
    async def cmd_status(self, event: AstrMessageEvent):
        if not is_admin(self, event):
            yield event.plain_result("这个命令只有管理员能用。")
            return
        yield event.plain_result(admin_summary(self, event.unified_msg_origin))

    @filter.command("学习风格")
    async def cmd_learn(self, event: AstrMessageEvent):
        if not is_admin(self, event):
            yield event.plain_result("这个命令只有管理员能用。")
            return
        session_id = event.unified_msg_origin
        history = self.storage.history_of(session_id, limit=999)
        need = int(self.settings.get("min_history_for_analysis"))
        if len(history) < need:
            yield event.plain_result(
                f"当前会话只有 {len(history)} 条待学习消息，至少需要 {need} 条。"
            )
            return
        yield event.plain_result(f"正在分析 {len(history)} 条消息并学习风格，请稍候…")
        result = await self.manager.learn_now(session_id)
        if result.get("ok"):
            yield event.plain_result(
                f"学习完成：稳定风格 {result['stable']} 条，"
                f"场景化表达 {result['situational']} 条。"
            )
        else:
            yield event.plain_result(f"学习失败：{result.get('reason', '未知原因')}")

    @filter.command("清空风格")
    async def cmd_clear_learned(self, event: AstrMessageEvent):
        """只清学习产出，不动手动记忆，也不动待学习消息。"""
        if not is_admin(self, event):
            yield event.plain_result("这个命令只有管理员能用。")
            return
        session_id = event.unified_msg_origin
        raw = (getattr(event, "message_str", "") or "").strip()
        if "全部" in raw or "all" in raw.lower():
            self._pending_clear[session_id] = ("all", time.time())
            yield event.plain_result(
                "这会清空本群的学习产出、手动记忆、待学习消息和主动回复状态，不可恢复。\n"
                "60 秒内发送 /确认清空 才会真的执行。"
            )
            return
        await self.storage.clear_session(session_id, scope="learned")
        yield event.plain_result(
            "已清空本群的学习产出。手动记忆和待学习消息都没动——"
            "要清后者请用 /清空记忆、/清空记录。"
        )

    @filter.command("清空记忆")
    async def cmd_clear_manual(self, event: AstrMessageEvent):
        """只清面板手动添加的记忆，不可恢复。"""
        if not is_admin(self, event):
            yield event.plain_result("这个命令只有管理员能用。")
            return
        await self.storage.clear_session(event.unified_msg_origin, scope="manual")
        yield event.plain_result("已清空本群的手动记忆（学习产出不受影响）。")

    @filter.command("清空记录")
    async def cmd_clear_history(self, event: AstrMessageEvent):
        """只清待学习消息缓冲。学习产出和手动记忆都不动。"""
        if not is_admin(self, event):
            yield event.plain_result("这个命令只有管理员能用。")
            return
        await self.storage.clear_session(event.unified_msg_origin, scope="history")
        yield event.plain_result("已清空本群的待学习消息缓冲。")

    @filter.command("确认清空")
    async def cmd_confirm_clear(self, event: AstrMessageEvent):
        if not is_admin(self, event):
            yield event.plain_result("这个命令只有管理员能用。")
            return
        session_id = event.unified_msg_origin
        pending = self._pending_clear.pop(session_id, None)
        if not pending:
            yield event.plain_result("没有待确认的清空操作。先发 /清空风格 全部。")
            return
        scope, issued_at = pending
        if time.time() - issued_at > _CONFIRM_TTL:
            yield event.plain_result("确认已过期（超过 60 秒），请重新发起。")
            return
        await self.storage.clear_session(session_id, scope=scope)
        yield event.plain_result("已全部清空：学习产出、手动记忆、待学习消息、主动回复状态。")

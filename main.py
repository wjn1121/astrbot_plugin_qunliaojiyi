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
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from . import api
from .constants import PLUGIN_AUTHOR, PLUGIN_DESC, PLUGIN_NAME, PLUGIN_VERSION
from .core import Manager, Settings, Storage
from .hooks import collect_message, inject_style


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
        self.manager = Manager(self, self.storage, self.settings)

        api.register(self)
        logger.info(f"[群聊记忆] 初始化完成，数据目录 {self.data_dir}")

    # ── 生命周期 ────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        if self.settings.get("enabled"):
            self.manager.start()
        logger.info("[群聊记忆] 已启动")

    async def terminate(self) -> None:
        await self.manager.stop()
        logger.info("[群聊记忆] 已停止")

    # ── 事件钩子（装饰器必须在类上，实现委托给 hooks.py）────────────────

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        await collect_message(self, event)

    @filter.on_llm_request(priority=17)
    async def on_llm_request(self, event: AstrMessageEvent, req):
        await inject_style(self, event, req)

    # ── 命令 ────────────────────────────────────────────────────────────

    @filter.command("风格状态")
    async def cmd_status(self, event: AstrMessageEvent):
        summary = self.manager.injector.summary(event.unified_msg_origin)
        stats = self.storage.stats()
        if not summary["has_styles"]:
            yield event.plain_result(
                f"当前会话还没有学到风格特征。\n"
                f"全局：{stats['sessions']} 个会话、{stats['traits']} 条特征，"
                f"待学习消息 {stats['pending_messages']} 条。"
            )
            return
        lines = [
            "当前会话风格：",
            f"学习产出：稳定 {summary['stable_count']} 条 · 场景化 {summary['situational_count']} 条",
            f"手动记忆：{summary['manual_count']} 条",
        ]
        for name in summary["stable_preview"]:
            lines.append(f"· {name}")
        yield event.plain_result("\n".join(lines))

    @filter.command("学习风格")
    async def cmd_learn(self, event: AstrMessageEvent):
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
    async def cmd_clear(self, event: AstrMessageEvent):
        await self.storage.clear_session(event.unified_msg_origin, scope="all")
        yield event.plain_result("已清空当前会话的学习风格、手动记忆与待学习记录。")

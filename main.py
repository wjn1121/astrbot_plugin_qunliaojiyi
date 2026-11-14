# Copyright (C) 2026 wjn1121
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# 本插件早期的风格学习参考 astrbot_plugin_oven_multi（AGPL-3.0，作者 汐兮雨）旧版
# style_learning 实现重写，保留「批量提炼 + 临时内容注入」的核心思路。
# 1.4.0 起累积链路改按 MaiBot 的 Expression 设计（situation + style 双段、
# 以出现次数当权重、累加写入而非整表覆盖），1.3.x 的整表重写只作为 replace 模式保留。

"""群聊记忆：从群聊里增量积累说话风格，按语境注入，并可导出人格档案。

成本模型（与烤箱之心新版的区别就在这三点）：
1. 不做「每条消息一次 LLM 筛选」——注入只读本地数据，零网络请求；
2. 学习按「攒够 30 条新消息 + 30 分钟最小间隔」触发，不是每 10 分钟一轮的定时任务；
3. 攒不够消息就不发请求，且只发新攒下的那部分，不发整包。

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
from .core.bond import BondStore
from .core.facts import FactStore
from .core.profile import ProfileStore
from .core.persona import PersonaStore
from .core.usage import UsageTracker
from .hooks import (
    apply_tone,
    collect_message,
    detect_correction,
    inject_style,
    maybe_active_reply,
    record_llm_usage,
    touch_relation,
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
        # 旧数据补齐 1.4.0 字段（status / seen / evidence 等），必须在读特征之前做，
        # 否则注入侧会因为缺 status 而把条目当成「已生效但没证据」的裸数据。
        self.storage.migrate_traits()
        # usage 要在 manager 之前：learner 学习完就调 plugin.usage.record
        self.usage = UsageTracker(self.data_dir, self.settings)
        # 人格档案落在插件数据目录之外，插件升级不会覆盖用户手写的内容
        self.persona = PersonaStore(self.data_dir)
        self.manager = Manager(self, self.storage, self.settings)
        self.active = ActiveReply(self, self.storage, self.settings)
        # 关系层：只做计算，数据落在 storage.bonds（复用它的 5 秒合并写盘）
        self.bond = BondStore(self)
        self.facts = FactStore(self)
        # 个人画像：数据落在 storage.profiles。装配在 facts 之后，
        # 因为它要读事实来生成；facts 那边只通过 getattr 取它，不反向依赖
        self.profile = ProfileStore(self, self.storage, self.settings)

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
        await detect_correction(self, event)
        async for result in maybe_active_reply(self, event):
            yield result

    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent):
        """bot 真的把回复发出去了，才算跟这个人发生了一次交互。

        以前挂在「收到消息」上，结果是：群里有人自顾自刷屏、bot 一句没接，
        好感照样涨——那反映的是谁打字快。挂到这里之后，
        空消息链、只回个 @ 或引用（管线会提前 return）都不算一次。

        15 分钟冷却 + 每轮去重兜底，分段回复打多遍钩子也只记一次。
        """
        await touch_relation(self, event)

    @filter.on_llm_request(priority=17)
    async def on_llm_request(self, event: AstrMessageEvent, req):
        await inject_style(self, event, req)

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response):
        await record_llm_usage(self, event, response)

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        # 先清理再截断：清理只会让字变少，截断按清理后的长度算才准
        await apply_tone(self, event)
        await truncate_result(self, event)

    # ── 命令 ────────────────────────────────────────────────────────────

    @filter.command("关系")
    async def cmd_relation(self, event: AstrMessageEvent):
        """看和当前用户的关系进度。"""
        if not self.settings.group("bond").get("enabled"):
            yield event.plain_result(
                "关系状态没开。管理员可以在插件配置的「关系状态」里打开。"
            )
            return
        try:
            is_group = not event.is_private_chat()
        except Exception:  # noqa: BLE001 - 判不出来按群聊算，措辞更保守
            is_group = True
        info = self.bond.summary(event.get_sender_id(), is_group=is_group)
        yield event.plain_result(BondStore.format_summary(info))

    @filter.command("称呼")
    async def cmd_nickname(self, event: AstrMessageEvent):
        """设定 bot 怎么称呼你；不带参数则清除。"""
        cfg = self.settings.group("bond")
        if not cfg.get("enabled"):
            yield event.plain_result(
                "关系状态没开，称呼暂时用不上。管理员可以在插件配置的「关系状态」里打开。"
            )
            return
        if not cfg.get("nickname_enabled", True):
            yield event.plain_result("管理员关掉了自定义称呼。")
            return

        raw = str(getattr(event, "message_str", "") or "").strip()
        parts = raw.replace("/", " ").split()
        args = [p for p in parts[1:] if p] if parts else []
        nickname = " ".join(args).strip()

        if not nickname:
            self.bond.set_nickname(event.get_sender_id(), "")
            yield event.plain_result("已清除，之后用你的平台昵称。")
            return

        cleaned = self.bond.set_nickname(event.get_sender_id(), nickname)
        if not cleaned:
            yield event.plain_result("这个称呼没存上，换一个短一点的试试。")
            return
        yield event.plain_result(f"好，以后叫你「{cleaned}」。")

    @filter.command("记得")
    async def cmd_facts(self, event: AstrMessageEvent):
        """看关于自己的记忆；带内容则是手动记一条。

        手动加的直接生效，不要求观察两次——人都明确说了「记一下」，
        再让他等两轮没道理。
        """
        if not self.facts.enabled():
            yield event.plain_result(
                "事实记忆没开。管理员可以在插件配置的「事实记忆」里打开。"
            )
            return
        raw = str(getattr(event, "message_str", "") or "").strip()
        parts = raw.replace("/", " ").split()
        text = " ".join(parts[1:]).strip() if len(parts) > 1 else ""
        if text:
            record = self.facts.remember(
                event.unified_msg_origin, event.get_sender_id(), text
            )
            if record is None:
                yield event.plain_result(
                    "这条没记上：要么太长，要么含敏感信息（密码、证件、验证码这类不记）。"
                )
                return
            yield event.plain_result(
                f"记下了：[{record.get('id')}] {record.get('text')}"
            )
            return
        info = self.facts.summary(event.unified_msg_origin, event.get_sender_id())
        yield event.plain_result(FactStore.format_summary(info))

    @filter.command("忘掉")
    async def cmd_forget(self, event: AstrMessageEvent):
        """删掉一条记忆。按编号或关键词，删完立即生效。"""
        if not self.facts.enabled():
            yield event.plain_result("事实记忆没开。")
            return
        raw = str(getattr(event, "message_str", "") or "").strip()
        parts = raw.replace("/", " ").split()
        token = " ".join(parts[1:]).strip() if len(parts) > 1 else ""
        if not token:
            yield event.plain_result(
                "用法：/忘掉 <编号或关键词>，例如 /忘掉 f_0001 或 /忘掉 论文。\n"
                "先发 /记得 看全部带编号的记忆。"
            )
            return
        removed = self.facts.forget(
            event.unified_msg_origin, event.get_sender_id(), token
        )
        if not removed:
            yield event.plain_result(f"没找到匹配「{token}」的记忆。发 /记得 看看有哪些。")
            return
        yield event.plain_result(f"删掉了 {removed} 条。")

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

    @filter.command("记忆审核")
    async def cmd_review(self, event: AstrMessageEvent):
        """审阅待生效的记忆：批准 / 否决 / 全部批准。"""
        if not is_admin(self, event):
            yield event.plain_result("这个命令只有管理员能用。")
            return

        raw = str(getattr(event, "message_str", "") or "").strip()
        parts = raw.replace("/", " ").split()
        args = [p for p in parts[1:] if p] if parts else []
        pending = self.storage.pending_traits()

        if not args:
            if not pending:
                yield event.plain_result("没有待审的记忆条目。")
                return
            lines = [f"待审记忆 {len(pending)} 条（最多列 15 条）："]
            for i, item in enumerate(pending[:15], 1):
                situation = item.get("context") or "任何场合"
                lines.append(
                    f"{i}. [{situation}] {item.get('content')}"
                    f"（权重 {item.get('proficiency', 0)}）"
                )
            lines.append("用法：记忆审核 批准 3 / 记忆审核 否决 3 / 记忆审核 全批准")
            yield event.plain_result("\n".join(lines))
            return

        action = args[0]
        if action in ("全批准", "all"):
            done = 0
            for item in pending:
                if self.storage.set_trait_status(item["session_id"], item["content"], "active"):
                    done += 1
            await self.storage.flush()
            yield event.plain_result(f"已批准 {done} 条，下一轮回复起生效。")
            return

        if action in ("批准", "通过", "approve"):
            target_status = "active"
        elif action in ("否决", "reject"):
            target_status = "rejected"
        else:
            yield event.plain_result("动作只能是：批准 / 否决 / 全批准。")
            return

        if len(args) < 2 or not args[1].isdigit():
            yield event.plain_result("请给条目编号，例如：记忆审核 批准 3")
            return
        index = int(args[1])
        if not 1 <= index <= len(pending):
            yield event.plain_result(f"编号超出范围（当前 {len(pending)} 条待审）。")
            return

        item = pending[index - 1]
        ok = self.storage.set_trait_status(item["session_id"], item["content"], target_status)
        await self.storage.flush()
        if not ok:
            yield event.plain_result("更新失败，条目可能已被改动。")
            return
        word = "已批准" if target_status == "active" else "已否决"
        yield event.plain_result(f"{word}：{item.get('content')}")

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

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
from astrbot.api.message_components import Image as AstrImage
from astrbot.api.star import Context, Star, StarTools, register

from . import api
from .constants import PLUGIN_AUTHOR, PLUGIN_DESC, PLUGIN_NAME, PLUGIN_VERSION
from .core import Manager, Settings, Storage
from .core.active import ActiveReply
from .core.admin import admin_summary, cost_summary, is_admin
from .core.bond import BondStore
from .core.budget import Budget
from .core.facts import FactStore
from .core.feed import record_feed
from .core.menu import build_menu_image, menu_text
from .core.profile import ProfileStore
from .core.sticker import StickerStore
from .core.persona import PersonaStore
from .core.usage import UsageTracker
from .hooks import (
    apply_feed_note,
    apply_tone,
    collect_message,
    detect_correction,
    extract_sticker_mood,
    inject_sticker_rule,
    inject_style,
    maybe_active_reply,
    maybe_send_sticker,
    record_llm_usage,
    send_sticker_after_reply,
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
        # 每日预算闸：必须在所有「会发请求」的模块之前装配，
        # manager / active / sticker / search 都要问它一句
        self.budget = Budget(self)
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
        # 表情包：只读本地目录 + 记账，不碰模型，也不进注入
        self.sticker = StickerStore(self)

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
        """收集消息，顺手接个图，再看看要不要主动插话。

        必须是 async generator：命中主动回复时要 ``yield event.request_llm(...)``，
        普通 async 函数接不住这个返回值。

        表情包放在最前面判断：它是纯本地的轻量互动，发完这一轮就不再主动插话了——
        一张图加一段话连着来，在群里看着像刷屏。
        """
        await collect_message(self, event)
        await detect_correction(self, event)
        if await maybe_send_sticker(self, event):
            return
        async for result in maybe_active_reply(self, event):
            yield result

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_menu_help(self, event: AstrMessageEvent):
        """@我说「群聊记忆帮助」，回一张命令菜单图。

        只认短句且必须同时出现「群聊记忆」和「帮助/菜单/命令」，
        免得群里正常聊天提到插件名就被塞一张图。
        画不出图时退回纯文字，内容同一份来源，不会两边不同步。
        """
        text = str(getattr(event, "message_str", "") or "").strip()
        if not text or len(text) > 30:
            return
        if "群聊记忆" not in text:
            return
        if not any(w in text.lower() for w in ("帮助", "菜单", "命令", "help")):
            return

        # 这一轮不用再让模型答一遍：用户要的是菜单，不是一段解释
        try:
            event.should_call_llm(False)
        except Exception:  # noqa: BLE001 - 老版本没这个方法，忽略
            pass

        path = None
        try:
            path = build_menu_image(self)
        except Exception as e:  # noqa: BLE001 - 画图失败不能连文字一起没了
            logger.warning(f"[群聊记忆] 生成菜单图失败，退回文字: {e}")

        try:
            if path is not None:
                yield event.chain_result([AstrImage.fromFileSystem(str(path))])
            else:
                yield event.plain_result(menu_text())
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[群聊记忆] 发送菜单失败: {e}")
            yield event.plain_result(menu_text())

    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent):
        """bot 真的把回复发出去了，才算跟这个人发生了一次交互。

        以前挂在「收到消息」上，结果是：群里有人自顾自刷屏、bot 一句没接，
        好感照样涨——那反映的是谁打字快。挂到这里之后，
        空消息链、只回个 @ 或引用（管线会提前 return）都不算一次。

        15 分钟冷却 + 每轮去重兜底，分段回复打多遍钩子也只记一次。
        """
        await touch_relation(self, event)
        # 喂食要在关系之后：touch 可能因为冷却/刷屏提前放行，
        # 而投喂有自己的一套限制（每日次数），不该被它挡掉
        await record_feed(self, event)
        # 表情包放最后：marker 模式的标签在 on_llm_response 已经剥好，
        # analyze 模式要读这一轮的最终正文
        await send_sticker_after_reply(self, event)

    @filter.on_llm_request(priority=17)
    async def on_llm_request(self, event: AstrMessageEvent, req):
        await inject_style(self, event, req)
        # marker 模式：掷骰命中才告诉模型「这轮你可以发表情」
        inject_sticker_rule(self, event, req)

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response):
        await record_llm_usage(self, event, response)
        # 在这一步剥标记：进对话历史、进学习材料的都是干净文本
        await extract_sticker_mood(self, event, response)

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        # 先清理再截断：清理只会让字变少，截断按清理后的长度算才准
        await apply_tone(self, event)
        await truncate_result(self, event)
        # 投喂提示放在截断之后：它不算进回复字数，也就不会被截掉半句
        await apply_feed_note(self, event)

    # ── 命令 ────────────────────────────────────────────────────────────

    @filter.command("表情包")
    async def cmd_sticker_status(self, event: AstrMessageEvent):
        """看表情包图库的状态：各分类几张、今天发了多少。"""
        cfg = self.sticker.config()
        counts = self.sticker.counts()
        lines = [
            f"表情包：{self.sticker.total()} 张，今天发了 {self.sticker.today_total()} 次"
            f"（上限 {int(cfg.get('daily_limit', 40) or 0)}）"
        ]
        detail = " · ".join(
            f"{c['name']} {counts.get(c['key'], 0)}" for c in self.sticker.categories
        )
        lines.append(detail)
        lines.append(
            "已开启，命中关键词或跟图时会发。" if cfg.get("enabled")
            else "斗图还没开。管理员可在插件配置的「表情包」里打开。"
        )
        yield event.plain_result("\n".join(lines))

    @filter.command("收图")
    async def cmd_collect_sticker(self, event: AstrMessageEvent):
        """把这条消息里的图收进图库。用法：发图 + /收图 笑

        群友的表情包是最好的素材来源——比自己找的图贴群里的口味。
        """
        if not is_admin(self, event):
            return

        raw = str(getattr(event, "message_str", "") or "").strip()
        # 同样是先滤空串再切命令名，「/收图 笑」不能被解析成 want="收图"
        tokens = [t for t in raw.replace("/", " ").split() if t]
        args = tokens[1:]
        want = args[0] if args else ""
        # /收图 笑 熊猫头 笑死 —— 后面几个词当标签存进去
        extra_tags = args[1:]

        cat = self.sticker.find(want)
        if cat is None:
            names = " / ".join(self.sticker.names())
            yield event.plain_result(
                f"要指定收进哪个分类：/收图 {self.sticker.names()[0]}\n可选：{names}"
            )
            return
        key = cat["key"]

        imgs = [
            comp
            for comp in (event.get_messages() or [])
            if type(comp).__name__ == "Image"
        ]
        if not imgs:
            yield event.plain_result("这条消息里没有图。发一张图再带上 /收图 试试。")
            return

        try:
            local = await imgs[0].convert_to_file_path()
            blob = Path(local).read_bytes()
        except Exception as e:  # noqa: BLE001 - 平台给的图可能下不下来
            logger.warning(f"[群聊记忆] 收图失败: {e}")
            yield event.plain_result("这张图没取到，可能链接过期了。")
            return

        ok, info = self.sticker.add_file(key, str(local), blob, extra_tags)
        if not ok:
            yield event.plain_result(f"没收成：{info}")
            return
        tag_note = f"，标签：{' / '.join(extra_tags)}" if extra_tags else ""
        yield event.plain_result(
            f"收进「{cat['name']}」了{tag_note}，"
            f"这个分类现在 {self.sticker.counts().get(key, 0)} 张。"
        )

    @filter.command("收表情")
    async def cmd_import_qq_faces(self, event: AstrMessageEvent):
        """把 bot 这个 QQ 号的收藏表情同步进图库。

        用法：
          /收表情           只看收藏了多少张
          /收表情 全部       全收（一次最多 200）
          /收表情 50         只收前 50 张
          /收表情 50 笑      收进「笑」分类（分类不存在会新建）
        """
        if not is_admin(self, event):
            return

        raw = str(getattr(event, "message_str", "") or "").strip()
        # 先滤掉空串再丢掉第一个 token。之前的写法是 parts[1:]，而
        # "/收表情" split 出来是 ["", "收表情"]，切掉第一个空串之后
        # 命令名本身留在了 args 里，被当成了分类名。
        tokens = [t for t in raw.replace("/", " ").split() if t]
        args = tokens[1:]

        limit = 0
        category = ""
        for item in args:
            if item.isdigit():
                limit = int(item)
            elif item == "全部":
                limit = 200
            else:
                category = item

        # 不带参数：只报数量，别一上来就下几百张
        if not args:
            try:
                urls = await self.sticker.fetch_qq_faces()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[群聊记忆] 拉收藏表情失败: {e}")
                urls = []
            if not urls:
                yield event.plain_result(
                    "没拿到收藏表情。可能是协议端不支持 fetch_custom_face（LLBot / NapCat 一般都支持），"
                    "或者这个号确实没有收藏。"
                )
                return
            yield event.plain_result(
                f"这个号收藏了 {len(urls)} 张表情。\n"
                f"「/收表情 全部」全收进图库（一次上限 200）\n"
                f"「/收表情 50 笑」只收前 50 张并放进「笑」"
            )
            return

        if not limit:
            limit = 50

        try:
            res = await self.sticker.import_qq_faces(limit=limit, category=category)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[群聊记忆] 导入收藏表情失败: {e}")
            yield event.plain_result(f"没收成：{e}")
            return

        if res.get("error"):
            yield event.plain_result(f"没收成：{res['error']}")
            return
        if not res.get("fetched"):
            yield event.plain_result("没拿到收藏表情。")
            return

        cat = self.sticker.category_of(res.get("category", "")) or {}
        lines = [
            f"收藏夹里有 {res['fetched']} 张，这次收进「{cat.get('name') or res.get('category')}」"
            f" {res['saved']} 张。"
        ]
        if res.get("skipped"):
            lines.append(f"跳过 {res['skipped']} 张重复的。")
        if res.get("failed"):
            lines.append(f"{res['failed']} 张没下下来。")
        if res["fetched"] > limit:
            lines.append(f"还剩 {res['fetched'] - limit} 张，再发一次命令接着收。")
        lines.append("接下来可以在面板点「识别未标注」给它们打标签。")
        yield event.plain_result("\n".join(lines))

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

    async def _cost_text(self, event: AstrMessageEvent) -> str:
        """查消耗的共同入口，/消耗 和 /token 走同一份，省得两边口径跑偏。"""
        if not is_admin(self, event):
            return "这个命令只有管理员能用。"
        return cost_summary(self, event.unified_msg_origin)

    @filter.command("消耗")
    async def cmd_cost(self, event: AstrMessageEvent):
        """查今天的 token 消耗，按回复 / 后台分开算。"""
        yield event.plain_result(await self._cost_text(event))

    @filter.command("token")
    async def cmd_token(self, event: AstrMessageEvent):
        """同 /消耗，留个英文名方便记。"""
        yield event.plain_result(await self._cost_text(event))

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

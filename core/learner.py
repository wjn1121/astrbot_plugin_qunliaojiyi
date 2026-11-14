"""学习者：把攒下的新消息交给 LLM，提炼成「情境 → 表达」候选，累积进特征库。

两条链路由 ``learn_mode`` 决定：
- ``accumulate``（默认）：候选 → 原文证据校验 → 入库自检 → 语义合并/新增 → 权重累加，
  措辞变了也认得出是同一条，库会随轮次长大；
- ``replace``（1.3.x 旧行为）：整表重写，措辞一变权重就归零，只在回退时使用。

关键成本控制（相对烤箱之心新版的定时任务）：
- **按会话整批提炼**，不是每条消息都请求；
- 只有攒够 ``learn_increment_messages`` 条新消息（或兜底到点）才发请求；
- 单次最多带 ``max_messages_per_learn`` 条，避免长历史把 prompt 撑大；
- 提炼完成后立刻清空该会话的缓冲，不会重复分析同一批内容。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from astrbot.api import logger

from .storage import Storage, normalize_text
from .types import ChatMessage, ParsedTraits, Trait

# 数字梗 / 短重复串（666、233、哈哈哈）不进稳定风格
# 数字刷屏：666 / 2333 / 233333 都算。
# 早先限了 2-4 位，导致 233333、666666 这种连打反而识别不出来——
# 而它们恰恰是最典型的刷屏。
_MEME_DIGIT_RE = re.compile(r"(?<!\d)\d{2,}(?!\d)")
_MEME_REPEAT_RE = re.compile(r"^(.)\1{2,}$")

SYSTEM_PROMPT = (
    "你是一个群聊文化分析师，擅长从聊天记录里提炼这个群的说话风格和语言习惯。"
    "你只输出 JSON，不输出解释。"
)

# 单条消息进 prompt 的最大字数。几百字的长文对「这个群怎么说话」贡献很低，
# 截断几乎不损失信息，但能省下大量输入 token。
LEARN_MSG_CHARS = 200

# 单次学习喂进 prompt 的总字符预算。正常群聊 80 条也就一两千字，碰不到上限；
# 它是给「有人贴长文 / 转长消息」兜底的——没有这层，一次学习可能贵十倍。
LEARN_CHARS_BUDGET = 4000

# 候选少于这个数就不做入库自检：为 1-2 条候选再发一次请求，
# 省下的请求比丢掉的那点精度值钱。
SELF_CHECK_MIN_CANDIDATES = 4

# 人格档案「已更新」日志的节流窗口（秒）。批量审核时这个事件会连着触发，
# 全打 INFO 只会把日志刷满。
PERSONA_LOG_INTERVAL = 60.0


def _fit_chars_budget(history: list[dict], budget: int) -> list[dict]:
    """按总字符预算裁历史：从最新往回取，超预算就停。

    保留最新的而不是最早的——学「这个群现在怎么说话」比学半年前怎么说话有用。
    至少留一条，否则预算极小时会返回空列表让上层误判成「消息不足」。
    """
    if budget <= 0:
        return history
    kept: list[dict] = []
    used = 0
    for msg in reversed(history):
        text = str(msg.get("content") or "")
        if kept and used + len(text) > budget:
            break
        kept.append(msg)
        used += len(text)
    kept.reverse()
    return kept


def looks_like_meme(content: str) -> bool:
    text = (content or "").strip()
    if not text:
        return True
    if len(text) <= 12 and _MEME_DIGIT_RE.search(text):
        return True
    if len(text) <= 6 and _MEME_REPEAT_RE.search(text):
        return True
    return False


class Learner:
    def __init__(self, plugin, storage: Storage):
        self.plugin = plugin
        self.storage = storage
        # 人格档案重建在批量审核时会被连着调很多次（实测 22 秒 7 次），
        # 按会话节流：同一会话一分钟内只打一条 INFO，其余降为 debug。
        # 档案内容确实每次都变了，但对日志读者来说「又更新了 6 次」没有信息量。
        self._persona_log_at: dict[str, float] = {}
        # 画像重写的后台任务。存着是为了让 asyncio 别把没跑完的任务回收掉
        self._profile_tasks: set = set()

    # ── 个人画像联动 ────────────────────────────────────────────────────

    def _schedule_profile(self, sender_id: str, session_id: str) -> None:
        """把「重写这个人的画像」丢到后台。失败静默——画像是增强项。"""
        profile = getattr(self.plugin, "profile", None)
        if profile is None:
            return
        try:
            task = asyncio.create_task(profile.maybe_rewrite(sender_id, session_id))
            self._profile_tasks.add(task)
            task.add_done_callback(self._profile_tasks.discard)
        except RuntimeError:
            # 没有事件循环（同步测试里调用）就跳过，下一轮学习还会再触发
            pass
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[群聊记忆] 画像重写排队失败（可忽略）: {e}")

    # ── 人格档案联动 ────────────────────────────────────────────────────

    def refresh_persona(self, session_id: str) -> None:
        """学习结束后把成果同步到人格档案。

        只喂学习产出（``universal``），不混手动记忆：手动记忆是「注入时额外
        指定」，人格档案是「这个群学出来的样子」，混在一起会让文件看不出
        哪些是学来的。失败只记日志，不影响学习结果。
        """
        store = getattr(self.plugin, "persona", None)
        if store is None:
            return
        try:
            result = store.rebuild(session_id, self.storage.universal.get(session_id, []))
            if not result.get("changed"):
                return
            now = time.monotonic()
            if now - self._persona_log_at.get(session_id, 0.0) >= PERSONA_LOG_INTERVAL:
                self._persona_log_at[session_id] = now
                logger.info(f"[群聊记忆] 人格档案已更新：{session_id}")
            else:
                logger.debug(f"[群聊记忆] 人格档案已更新（{session_id}，同类日志已合并）")
        except Exception as e:  # noqa: BLE001 - 档案是附加产物，不能拖垮学习
            logger.warning(f"[群聊记忆] 更新人格档案失败 {session_id}: {e}")

    # ── 对外入口 ────────────────────────────────────────────────────────

    async def learn(self, session_id: str, provider_id: str = "", manual: bool = False) -> dict:
        """对一个会话执行一次提炼，返回结果摘要。"""
        settings = self.plugin.settings
        min_history = int(settings.get("min_history_for_analysis"))
        limit = int(settings.get("max_messages_per_learn")) or 80

        history = self.storage.history_of(session_id, limit=limit)
        if len(history) < min_history:
            return {
                "ok": False,
                "reason": f"消息不足（{len(history)}/{min_history}），跳过",
                "stable": 0,
                "situational": 0,
            }

        # 纯表情、单字刷屏、123 这类没有学习价值，纯粹白花一次请求。
        # 门槛设 3 条：低于这个数基本可以确定这批就是刷屏，
        # 再往上提就有误杀正常短句聊天的风险。
        meaningful = sum(
            1 for m in history if not looks_like_meme(str(m.get("content") or ""))
        )
        if meaningful < 3:
            return {
                "ok": False,
                "reason": f"这批消息里只有 {meaningful} 条有内容（其余是表情或刷屏），跳过",
                "stable": 0,
                "situational": 0,
            }

        # 按总字符预算再裁一次：单条已经截短，但 80 条长文仍是十几万字符的量级。
        # 不设总预算的话，一次学习可能比平时贵十倍。
        history = _fit_chars_budget(history, LEARN_CHARS_BUDGET)

        # 记录本次分析规模：面板据此展示「这套机制到底花了多少」
        message_count = len(history)
        chars = sum(len(str(m.get("content") or "")) for m in history)
        started = time.monotonic()
        mode = self._learn_mode()
        code_map: dict[str, str] = {}
        if mode != "replace":
            prompt, code_map = self._build_prompt_accumulate(session_id, history)
        else:
            # replace 模式不抽事实：它整表重写，和「按人累计」是两套模型
            prompt = self._build_prompt(session_id, history)
        try:
            provider = await self._resolve_provider(provider_id, session_id)
            if provider is None:
                return {
                    "ok": False,
                    "reason": "没有可用的 LLM Provider",
                    "stable": 0,
                    "situational": 0,
                }

            if settings.is_debug():
                logger.info(f"[群聊记忆] 调试 · 学习请求 · {session_id}\n{prompt[:2000]}")

            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt, contexts=[], system_prompt=SYSTEM_PROMPT
                ),
                timeout=180,
            )
            text = (getattr(resp, "completion_text", "") or "").strip()
            # 记一次学习消耗。统计坏了不能影响学习本身，所以整段兜住异常。
            try:
                self.plugin.usage.record("learn", getattr(resp, "usage", None), session_id)
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[群聊记忆] 记录学习消耗失败（可忽略）: {e}")
            if not text:
                raise ValueError("LLM 返回空内容")

            if mode == "replace":
                parsed = self._extract_traits(text)
                if not parsed:
                    raise ValueError("未能从输出中解析出 JSON 特征")

                self.storage.replace_traits(
                    session_id,
                    parsed["universal"],
                    parsed["situational"],
                    limits=(
                        int(settings.get("max_stable_traits")),
                        int(settings.get("max_situational_traits")),
                    ),
                )
                await self.storage.clear_history(session_id)

                elapsed = time.monotonic() - started
                self.storage.record_learn(
                    session_id,
                    ok=True,
                    stable=len(parsed["universal"]),
                    situational=len(parsed["situational"]),
                    elapsed=elapsed,
                    message_count=message_count,
                    chars=chars,
                )
                logger.info(
                    f"[群聊记忆] {session_id} 学习完成（replace）：稳定 {len(parsed['universal'])} 条，"
                    f"场景化 {len(parsed['situational'])} 条，耗时 {elapsed:.1f}s"
                )
                self.refresh_persona(session_id)
                return {
                    "ok": True,
                    "mode": "replace",
                    "stable": len(parsed["universal"]),
                    "situational": len(parsed["situational"]),
                    "elapsed": round(elapsed, 2),
                    "universal": parsed["universal"],
                }

            # ── accumulate：候选 → 证据校验 → 入库自检 → upsert ──────────
            raw_candidates = self._extract_expressions(text)
            if not raw_candidates:
                raise ValueError("未能从输出中解析出候选表达")

            verified, dropped_evidence = self._verify_evidence(raw_candidates, history)
            kept, dropped_check = await self._self_check(verified, provider, session_id)

            # 事实：同一次输出里顺带抽出来的，按人分桶入库。
            # 放在自检之后、入库之前——它和风格候选互不影响，
            # 但失败要能被这一层的 try 兜住。
            fact_stats = self._ingest_facts(session_id, text, code_map, history)

            stats = self.storage.upsert_traits(
                session_id,
                kept,
                limits=(
                    int(settings.get("max_stable_traits")),
                    int(settings.get("max_situational_traits")),
                ),
                max_total=int(settings.get("trait_max_total") or 300),
                activate_at=max(1, int(float(settings.get("trait_activate_count") or 2) * 10)),
            )
            self.storage.archive_stale(session_id)
            await self.storage.clear_history(session_id)

            stable_n = sum(1 for c in kept if not str(c.get("context") or "").strip())
            situ_n = len(kept) - stable_n
            elapsed = time.monotonic() - started
            self.storage.record_learn(
                session_id,
                ok=True,
                stable=stable_n,
                situational=situ_n,
                elapsed=elapsed,
                message_count=message_count,
                chars=chars,
                message=(
                    f"新增 {stats['added']} / 合并 {stats['merged']} / "
                    f"转生效 {stats['activated']} / 证据不符 {dropped_evidence} / "
                    f"自检未过 {dropped_check}"
                ),
            )
            logger.info(
                f"[群聊记忆] {session_id} 学习完成（accumulate）：候选 {len(raw_candidates)} 条，"
                f"证据不符丢 {dropped_evidence}，自检丢 {dropped_check}，"
                f"入库新增 {stats['added']} 合并 {stats['merged']} 转生效 {stats['activated']}，"
                f"耗时 {elapsed:.1f}s"
            )
            self.refresh_persona(session_id)
            return {
                "ok": True,
                "mode": "accumulate",
                "stable": stable_n,
                "situational": situ_n,
                "added": stats["added"],
                "merged": stats["merged"],
                "activated": stats["activated"],
                "dropped_evidence": dropped_evidence,
                "dropped_check": dropped_check,
                "elapsed": round(elapsed, 2),
            }
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - started
            self.storage.record_learn(
                session_id,
                ok=False,
                elapsed=elapsed,
                message="请求超时",
                message_count=message_count,
                chars=chars,
            )
            logger.warning(f"[群聊记忆] {session_id} 学习超时（{elapsed:.1f}s）")
            return {"ok": False, "reason": "请求超时", "stable": 0, "situational": 0}
        except Exception as e:  # noqa: BLE001 - 后台任务不能因单次失败中断
            elapsed = time.monotonic() - started
            self.storage.record_learn(
                session_id,
                ok=False,
                elapsed=elapsed,
                message=str(e),
                message_count=message_count,
                chars=chars,
            )
            logger.error(f"[群聊记忆] {session_id} 学习失败: {e}")
            return {"ok": False, "reason": str(e), "stable": 0, "situational": 0}

    # ── Provider 解析 ───────────────────────────────────────────────────

    async def _resolve_provider(self, provider_id: str, session_id: str):
        context = self.plugin.context
        provider_id = (provider_id or "").strip()
        if provider_id:
            try:
                provider = context.get_provider_by_id(provider_id=provider_id)
                if provider is not None:
                    return provider
                logger.warning(f"[群聊记忆] 指定的 Provider 不存在: {provider_id}，回退默认")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[群聊记忆] 获取指定 Provider 失败: {e}")
        try:
            return context.get_using_provider(umo=session_id)
        except TypeError:
            # 老版本 AstrBot 的签名不带 umo
            return context.get_using_provider()
        except Exception as e:  # noqa: BLE001
            logger.error(f"[群聊记忆] 获取默认 Provider 失败: {e}")
            return None

    # ── Prompt 构建 ─────────────────────────────────────────────────────

    def _learn_mode(self) -> str:
        """当前学习链路：accumulate（累积）或 replace（1.3.x 旧行为）。"""
        mode = str(self.plugin.settings.get("learn_mode") or "accumulate").strip().lower()
        return "replace" if mode == "replace" else "accumulate"

    def _build_prompt_accumulate(
        self, session_id: str, history: list[ChatMessage]
    ) -> tuple[str, dict[str, str]]:
        """累积链路的提炼 prompt：要求给「什么时候说 + 怎么说 + 原文证据」。

        返回 ``(prompt, code_map)``。``code_map`` 是「U1 → 真实 ID」的映射：
        prompt 里用代号而不是裸 QQ 号，既省字也少一层不必要的暴露，
        解析 facts 时再靠它翻回真实 ID。

        没有 ``sender_id`` 的老消息（1.5.0 之前采集的）不参与分配代号，
        那一批也就不抽事实——归属不了人的事实记下来只会污染别人的桶。
        """
        code_map: dict[str, str] = {}
        sender_code: dict[str, str] = {}
        lines = []
        for msg in history:
            sender_id = str(msg.get("sender_id") or "").strip()
            code = ""
            if sender_id:
                code = sender_code.get(sender_id, "")
                if not code:
                    code = f"U{len(code_map) + 1}"
                    sender_code[sender_id] = code
                    code_map[code] = sender_id
            sender = str(msg.get("sender") or "?")[:24]
            content = str(msg.get("content") or "").replace("\n", " ")[:LEARN_MSG_CHARS]
            if content:
                # 有 ID 就写成「U1(老王)」：代号给模型做归属，括号里的名字帮它理解语境
                label = f"{code}({sender})" if code else sender
                lines.append(f"{label}: {content}")
        history_str = "\n".join(lines)

        # 已有特征只给「情境 + 表达 + 权重」，控制 prompt 长度；权重高的排在前面，
        # 让模型知道哪些已经是这个群的稳定习惯，不要重复输出。
        existing = sorted(
            (t for t in self.storage.universal.get(session_id, [])
             if t.get("status") != "rejected"),
            key=lambda t: t.get("proficiency", 0),
            reverse=True,
        )[:20]
        hint = ""
        if existing:
            rows = []
            for t in existing:
                situation = str(t.get("context") or "").strip() or "（任何场合）"
                rows.append(f"- 情境：{situation}｜表达：{t.get('content')}｜权重 {t.get('proficiency', 0)}")
            hint = (
                "\n\n已有的表达习惯（权重越高越是这个群的稳定风格。"
                "不要重复输出这些内容；如果某个已有说法不够准确，可以给更准确的新说法）：\n"
                + "\n".join(rows)
            )

        manual = self.storage.manual_of(session_id)
        if manual:
            manual_lines = [f"- {t.get('content')}" for t in manual[:20]]
            hint += (
                "\n\n以下内容已由管理员手动指定，不要重复输出，也不要输出与之矛盾的条目：\n"
                + "\n".join(manual_lines)
            )

        limit = int(self.plugin.settings.get("learn_candidate_max") or 12)

        # 事实抽取合并进同一次请求：同一批消息，一次让它同时输出风格候选和事实候选。
        # 单独再抽一次等于同一批数据付两遍钱。facts 关着就整段不出现，
        # 模型也就不会为它多吐 token。
        facts_block = ""
        if self._facts_on() and code_map:
            names = "、".join(f"{c}={m}" for c, m in list(code_map.items())[:12])
            facts_block = f"""

另外，从同一批记录里找出值得长期记住的、关于某个具体人的事，放进 "facts" 字段。
只记：稳定的个人信息、明确的安排、重要事件、明确说过的偏好或边界。
不记：寒暄、临时情绪、评价别人的话、能从上下文直接推回去的内容。
绝不记：密码、证件号、银行卡、验证码、住址、手机号。
每条必须有 evidence 且必须是记录里的原话片段，对不上就不要输出。没有就返回 []。

fact 的字段：
- text：一句话说清，不超过 30 字。写「下周三要交论文初稿」，不写「他提到了论文」
- kind：fact（长期事实）/ event（有时间的安排）/ preference（偏好）/ boundary（明确说过的边界，如「别叫我老弟」）
- importance：1-5，5 最重要
- subject：这件事属于谁，填上面的代号（{names}）。分不清就留空字符串
- evidence：从记录里原样复制的原话片段，不超过 24 字

格式：
{{"facts": [{{"text": "下周三要交论文初稿", "kind": "event", "importance": 4, "subject": "U1", "evidence": "我下周三得交论文初稿"}}]}}"""

        return f"""分析以下群聊记录，提炼这个群「在什么情况下、用什么方式说话」。

聊天记录：
```
{history_str}
```{hint}

要求：
1. 只返回有效 JSON，不要解释，不要代码围栏。
2. 格式：
   {{"expressions": [{{"situation": "当……时", "style": "怎么说", "evidence": "本轮聊天里的原话片段", "dimension": "语气词", "triggers": ["触发词"]}}]}}
3. situation 写「什么时候适用」，可以是空字符串（表示任何场合都成立的语气、句式特征）。
   style 写「具体怎么说」，要是能直接照做的表达习惯，10-30 字，不要写「说话随意」这类放到任何群都成立的空话。
4. evidence 必须从上面的聊天记录里原样复制一小段（不超过 24 字，挑最能体现这条风格的那句）。
   找不到对应原话就不要输出这一条。这一条是硬性要求。
5. dimension 只能取：语气词 / 句式节奏 / 互动方式 / 话题偏好 / 黑话梗。
6. triggers 只给场景化表达：1-3 个「群友消息里出现就该接这个梗」的短词（2-6 字）。
   不要用「哈哈」「好」这类到处都有的词；稳定风格不需要 triggers，给空数组。
7. 最多 {limit} 条。宁可少而准，不要凑数。
8. 一次性玩梗、某一件具体的事不要收录，只收「以后还会反复出现的说话习惯」。
9. 描述里不要出现英文双引号。

示例输出：
{{"expressions": [
  {{"situation": "", "style": "句末常带喵、qwq 等语气词卖萌", "evidence": "我又歪了喵", "dimension": "语气词", "triggers": []}},
  {{"situation": "有人报抽卡结果时", "style": "用「出货了？」反问，或直接骂欧", "evidence": "出货了属于是", "dimension": "互动方式", "triggers": ["出货", "抽卡"]}}
]}}{facts_block}""", code_map

    def _build_prompt(self, session_id: str, history: list[ChatMessage]) -> str:
        lines = []
        for msg in history:
            sender = str(msg.get("sender") or "?")[:24]
            content = str(msg.get("content") or "").replace("\n", " ")[:LEARN_MSG_CHARS]
            if content:
                lines.append(f"{sender}: {content}")
        history_str = "\n".join(lines)

        existing_stable, existing_sit = self.storage.learned_split(session_id)
        stable_lines = [f"- {t.get('content')}" for t in existing_stable]
        sit_lines = [
            f"- {t.get('content')}（场景：{t.get('context', '')}）" for t in existing_sit
        ]
        hint = ""
        if stable_lines or sit_lines:
            parts = []
            if stable_lines:
                parts.append("稳定风格：\n" + "\n".join(stable_lines))
            if sit_lines:
                parts.append("场景化表达：\n" + "\n".join(sit_lines))
            hint = "\n\n已有的风格特征（请保留合适的并补充新的）：\n" + "\n".join(parts)

        # 手动记忆单独告诉模型：一是让它别重复输出同样的内容（否则会被去重丢掉，
        # 白白少一条产出），二是避免它提炼出语义冲突的条目。
        manual = self.storage.manual_of(session_id)
        if manual:
            manual_lines = [f"- {t.get('content')}" for t in manual[:20]]
            hint += (
                "\n\n以下内容已由管理员手动指定，不需要重复输出，也不许输出与之重复或矛盾的条目：\n"
                + "\n".join(manual_lines)
            )

        return f"""分析以下群聊记录，提炼这个群的说话风格和语言习惯。

聊天记录：
```
{history_str}
```{hint}

要求：
1. 只返回有效 JSON，不要解释，不要代码围栏。
2. 格式：
   {{"universal": ["稳定风格1", "稳定风格2"], "situational": [{{"content": "场景化表达", "context": "触发场景", "triggers": ["触发词"]}}]}}
3. universal 只收跨场景稳定成立的特征：语气、句式、措辞习惯、话题偏好。最多 8 条，每条约 10-30 字。
4. situational 只收有明确触发场景的表达：特定场合才用的梗、数字梗（如 666、233）、刷屏用语，每条必须带触发场景描述。最多 6 条。
5. 每条 situational 必须给 triggers：1-3 个「群友消息里出现就该接这个梗」的短词（2-6 字，如 "666"、"笑死"、"绷不住"）。
   triggers 要具体，不要用"哈哈""好""是的"这类到处都有的词；没有合适触发词就给空数组。
6. 特定数字梗、一次性梗、刷屏用语绝不能放进 universal，只能放 situational；只是偶尔出现、没有稳定场景的表达不要收录。
7. 特征描述里不要出现引号或双引号，直接写内容。
8. 记录太少或没有明显风格时，返回 {{"universal": [], "situational": []}}。

示例输出：
{{"universal": ["爱用语气词，对话节奏快", "喜欢自嘲和互怼，但不破防", "常用缩写和圈内黑话"],
  "situational": [{{"content": "666", "context": "表示佩服或认同", "triggers": ["666"]}},
                  {{"content": "绷不住了", "context": "看到离谱内容时感叹", "triggers": ["绷不住", "绷"]}}]}}"""

    # ── 输出解析 ────────────────────────────────────────────────────────

    @staticmethod
    def _grab_json(text: str) -> str | None:
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if match:
            return match.group(1)
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return text[start : end + 1]
        return None

    def _extract_expressions(self, text: str) -> list[dict]:
        """解析累积链路的候选：``situation`` / ``style`` / ``evidence``。

        模型偶尔仍会按 1.3.x 的 ``universal`` / ``situational`` 格式输出，这里一并兜住，
        不带证据的候选会在下一步被证据校验挡掉。
        """
        json_str = self._grab_json(text)
        if not json_str:
            logger.warning("[群聊记忆] LLM 输出里找不到 JSON 结构")
            return []
        try:
            data = json.loads(json_str)
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"[群聊记忆] 候选 JSON 解析失败: {e}")
            return []
        if not isinstance(data, dict):
            return []

        out: list[dict] = []

        def push(style: Any, situation: Any = "", triggers: Any = None,
                 dimension: Any = "", evidence: Any = "") -> None:
            content = str(style or "").strip().strip("「」\"'")
            if not content or len(content) > 60:
                return
            out.append(
                {
                    "content": content,
                    "context": str(situation or "").strip()[:60],
                    "triggers": self._clean_triggers(triggers),
                    "dimension": str(dimension or "").strip()[:12],
                    # prompt 要求 24 字，这里放宽到 48：模型偶尔会超一点，
                    # 多留些余量做证据子串校验，但不再无上限地存整段
                    "evidence": str(evidence or "").strip()[:48],
                }
            )

        expressions = data.get("expressions")
        if isinstance(expressions, list):
            for item in expressions:
                if isinstance(item, dict):
                    push(
                        item.get("style") or item.get("content"),
                        item.get("situation") or item.get("context"),
                        item.get("triggers"),
                        item.get("dimension"),
                        item.get("evidence"),
                    )
                elif isinstance(item, str):
                    push(item)

        # 1.3.x 格式兜底
        for item in data.get("universal") or []:
            if isinstance(item, str):
                push(item)
        for item in data.get("situational") or []:
            if isinstance(item, dict):
                push(
                    item.get("content"),
                    item.get("context"),
                    item.get("triggers"),
                    item.get("dimension"),
                    item.get("evidence"),
                )

        # 护栏：数字梗、短重复串只能算场景化表达，不能当稳定风格
        for cand in out:
            if not cand["context"] and looks_like_meme(cand["content"]):
                cand["context"] = "群友使用或刷出该表达时自然接梗，不要主动扩散"
                if not cand["triggers"]:
                    cand["triggers"] = self._clean_triggers([cand["content"]])

        # 同批次去重（同表达同情境只留一条）
        seen: set[tuple[str, str]] = set()
        dedup: list[dict] = []
        for cand in out:
            key = (cand["content"], cand["context"])
            if key in seen:
                continue
            seen.add(key)
            dedup.append(cand)
        return dedup

    def _verify_evidence(
        self, candidates: list[dict], history: list[ChatMessage]
    ) -> tuple[list[dict], int]:
        """证据必须是本轮聊天里的原话，否则整条丢弃。

        这是防 LLM 编造特征最有效的一道门：编出来的风格找不到原文，直接出局；
        同时也让面板上「这条从哪学来的」有据可查。
        """
        if not bool(self.plugin.settings.get("learn_require_evidence")):
            return candidates, 0
        pool = [normalize_text(m.get("content")) for m in history if m.get("content")]
        blob = "\n".join(pool)
        kept, dropped = [], 0
        for cand in candidates:
            evidence = normalize_text(cand.get("evidence"))
            if not evidence:
                dropped += 1
                continue
            if evidence in blob or any(evidence in part for part in pool):
                kept.append(cand)
            else:
                logger.debug(
                    f"[群聊记忆] 证据与该轮聊天对不上，丢弃候选：{cand.get('content')} / {cand.get('evidence')}"
                )
                dropped += 1
        return kept, dropped

    def _facts_on(self) -> bool:
        """事实记忆是否开着。读不到配置就当关着——它默认就是关的。"""
        try:
            return bool(self.plugin.settings.group("facts").get("enabled"))
        except Exception:  # noqa: BLE001
            return False

    def _extract_facts(self, text: str, code_map: dict[str, str]) -> list[dict]:
        """从同一次输出里解析 facts，并把代号翻回真实 ID。

        归属不了人的条目不记：一条不知道属于谁的事实，最后只会被注进
        错误对象的 prompt 里，比不记更糟。
        """
        if not code_map:
            return []
        json_str = self._grab_json(text)
        if not json_str:
            return []
        try:
            data = json.loads(json_str)
        except (json.JSONDecodeError, ValueError):
            return []
        if not isinstance(data, dict):
            return []
        raw = data.get("facts")
        if not isinstance(raw, list):
            # 老格式或模型没输出 facts：按空数组处理，不报错不重试
            return []

        out: list[dict] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            subject = str(item.get("subject") or "").strip()
            uid = code_map.get(subject, "")
            if not uid:
                continue
            body = str(item.get("text") or "").strip()
            if not body:
                continue
            out.append(
                {
                    "text": body,
                    "kind": str(item.get("kind") or "fact").strip(),
                    "importance": item.get("importance"),
                    "evidence": item.get("evidence"),
                    "_subject": uid,
                }
            )
        return out

    def _ingest_facts(
        self, session_id: str, text: str, code_map: dict[str, str], history: list[dict]
    ) -> dict[str, int]:
        """把这次输出里的事实按人分桶入库。失败只记日志，不影响学习结果。"""
        total = {"added": 0, "merged": 0, "activated": 0, "dropped": 0, "sensitive": 0}
        try:
            store = getattr(self.plugin, "facts", None)
            if store is None or not self._facts_on():
                return total

            per_user: dict[str, list[dict]] = {}
            for cand in self._extract_facts(text, code_map):
                uid = cand.pop("_subject", "")
                if uid:
                    per_user.setdefault(uid, []).append(cand)
            for uid, candidates in per_user.items():
                stats = store.ingest(session_id, uid, candidates, history)
                for key in total:
                    total[key] += int(stats.get(key) or 0)
                # 事实变了，画像可能需要重写。**异步排队，不 await**——
                # 重写要发一次请求，等它就把整轮学习拖长了
                if stats.get("added") or stats.get("merged"):
                    self._schedule_profile(uid, session_id)

            if total["added"] or total["activated"]:
                # 只打数量不打内容：敏感信息那几类连「丢过一条」的具体内容
                # 都不该出现在日志里，数量本身不泄露任何东西
                logger.info(
                    f"[群聊记忆] 事实入库：新增 {total['added']} · 合并 {total['merged']}"
                    f" · 转生效 {total['activated']} · 校验丢弃 {total['dropped']}"
                    f" · 敏感跳过 {total['sensitive']}"
                )
            return total
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[群聊记忆] 事实入库失败（本次跳过）: {e}")
            return total

    async def _self_check(
        self, candidates: list[dict], provider: Any, session_id: str
    ) -> tuple[list[dict], int]:
        """入库自检：让 LLM 判断候选里哪些是「可复用的说话习惯」。

        失败一律放行——自检是提质手段，不该成为学习链路的新故障点。
        """
        if not candidates:
            return [], 0
        if not bool(self.plugin.settings.get("learn_self_check")):
            return candidates, 0
        # 候选太少时不值得再发一次请求：少留几条的代价，
        # 比每天都多花一半请求小得多
        if len(candidates) < SELF_CHECK_MIN_CANDIDATES:
            logger.debug(
                f"[群聊记忆] 候选只有 {len(candidates)} 条，跳过入库自检（省一次请求）"
            )
            return candidates, 0

        rows = []
        for idx, cand in enumerate(candidates, 1):
            situation = cand.get("context") or "（任何场合）"
            rows.append(
                f"{idx}. 情境：{situation}｜表达：{cand['content']}｜证据：{cand.get('evidence') or '无'}"
            )
        prompt = f"""以下是刚从群聊记录里提炼出的说话习惯候选，判断每一条是否值得长期保留。

候选：
{chr(10).join(rows)}

淘汰标准（满足任意一条就淘汰）：
- 只在某一次具体事件里出现，不是可复用的说话习惯
- 过于笼统，放到任何群都成立（如「说话比较随意」「喜欢用表情」这类空话）
- 只是某个人偶尔的说法，不是这个群的普遍习惯

只输出 JSON，不要解释，不要代码围栏：{{"keep": [1, 3], "drop": [2]}}
编号必须覆盖全部候选。"""
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, contexts=[], system_prompt=SYSTEM_PROMPT),
                timeout=60,
            )
            try:
                self.plugin.usage.record("learn", getattr(resp, "usage", None), session_id)
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[群聊记忆] 记录自检消耗失败（可忽略）: {e}")
            text = (getattr(resp, "completion_text", "") or "").strip()
            json_str = self._grab_json(text)
            if not json_str:
                return candidates, 0
            data = json.loads(json_str)
            drop_ids = {int(i) for i in (data.get("drop") or []) if str(i).strip().isdigit()}
            if not drop_ids:
                return candidates, 0
            kept = [c for i, c in enumerate(candidates, 1) if i not in drop_ids]
            dropped = len(candidates) - len(kept)
            logger.info(f"[群聊记忆] 入库自检淘汰 {dropped} 条候选")
            return kept, dropped
        except Exception as e:  # noqa: BLE001 - 自检失败不影响学习
            logger.warning(f"[群聊记忆] 入库自检失败，候选全部放行: {e}")
            return candidates, 0

    def _extract_traits(self, text: str) -> ParsedTraits | None:
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        json_str = match.group(1) if match else None
        if not json_str:
            start, end = text.find("{"), text.rfind("}")
            if start != -1 and end > start:
                json_str = text[start : end + 1]
        if not json_str:
            logger.warning("[群聊记忆] LLM 输出里找不到 JSON 结构")
            return None

        try:
            data = json.loads(json_str)
            universal = data.get("universal", []) or []
            situational = data.get("situational", []) or []
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"[群聊记忆] JSON 解析失败，尝试兜底: {e}")
            universal = self._fallback_strings(json_str) or []
            situational = []
            if not universal:
                return None

        stable_input = [u for u in universal if isinstance(u, str) and u.strip()]
        sit_items: list[Trait] = []
        for item in situational if isinstance(situational, list) else []:
            if isinstance(item, str) and item.strip():
                sit_items.append({"content": item.strip(), "context": "", "triggers": []})
            elif isinstance(item, dict):
                content = str(item.get("content") or "").strip()
                if content:
                    sit_items.append(
                        {
                            "content": content,
                            "context": str(item.get("context") or "").strip(),
                            "triggers": self._clean_triggers(item.get("triggers")),
                        }
                    )

        # 护栏：把数字梗/短重复串从稳定风格降级到场景化
        stable, moved = [], []
        for content in stable_input:
            if looks_like_meme(content):
                moved.append(
                    {
                        "content": content,
                        "context": "群友使用或刷出该表达时自然接梗，不要主动扩散",
                        "triggers": self._clean_triggers([content]),
                    }
                )
            else:
                stable.append(content)
        sit_items = moved + sit_items

        seen, dedup = set(), []
        for item in sit_items:
            if item["content"] in seen:
                continue
            seen.add(item["content"])
            dedup.append(item)

        if not stable and not dedup:
            return None
        return {"universal": stable, "situational": dedup}

    @staticmethod
    def _clean_triggers(raw: Any) -> list[str]:
        """规范化触发词：去空、去超长、去重，最多 3 个。"""
        out: list[str] = []
        seen: set[str] = set()
        for item in raw if isinstance(raw, list) else []:
            text = str(item or "").strip()
            if not text or len(text) > 12 or text in seen:
                continue
            seen.add(text)
            out.append(text)
            if len(out) >= 3:
                break
        return out

    @staticmethod
    def _fallback_strings(text: str) -> list[str] | None:
        """JSON 里混进未转义引号时，尽量把字符串数组抠出来。"""
        arr_start = text.find("[", text.find("universal"))
        if arr_start == -1:
            return None
        depth, in_str, i, arr_end = 0, False, arr_start, -1
        while i < len(text):
            ch = text[i]
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_str = not in_str
            elif not in_str:
                if ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
                    if depth == 0:
                        arr_end = i
                        break
            i += 1
        if arr_end == -1:
            return None

        content = text[arr_start + 1 : arr_end]
        strings, i = [], 0
        while i < len(content):
            if content[i] != '"':
                i += 1
                continue
            i += 1
            buf = []
            while i < len(content):
                c = content[i]
                if c == "\\":
                    buf.append(c)
                    i += 1
                    if i < len(content):
                        buf.append(content[i])
                    i += 1
                elif c == '"':
                    tail = content[i + 1 :].lstrip()
                    if not tail or tail[0] in ",]":
                        strings.append("".join(buf))
                        i += 1
                        break
                    buf.append(c)
                    i += 1
                else:
                    buf.append(c)
                    i += 1
            else:
                break
        return strings or None

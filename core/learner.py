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
_MEME_DIGIT_RE = re.compile(r"(?<!\d)\d{2,4}(?!\d)")
_MEME_REPEAT_RE = re.compile(r"^(.)\1{2,}$")

SYSTEM_PROMPT = (
    "你是一个群聊文化分析师，擅长从聊天记录里提炼这个群的说话风格和语言习惯。"
    "你只输出 JSON，不输出解释。"
)


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
            if result.get("changed"):
                logger.info(f"[群聊记忆] 人格档案已更新：{session_id}")
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

        # 记录本次分析规模：面板据此展示「这套机制到底花了多少」
        message_count = len(history)
        chars = sum(len(str(m.get("content") or "")) for m in history)
        started = time.monotonic()
        mode = self._learn_mode()
        prompt = (
            self._build_prompt_accumulate(session_id, history)
            if mode != "replace"
            else self._build_prompt(session_id, history)
        )
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

    def _build_prompt_accumulate(self, session_id: str, history: list[ChatMessage]) -> str:
        """累积链路的提炼 prompt：要求给「什么时候说 + 怎么说 + 原文证据」。"""
        lines = []
        for msg in history:
            sender = str(msg.get("sender") or "?")[:24]
            content = str(msg.get("content") or "").replace("\n", " ")[:300]
            if content:
                lines.append(f"{sender}: {content}")
        history_str = "\n".join(lines)

        # 已有特征只给「情境 + 表达 + 权重」，控制 prompt 长度；权重高的排在前面，
        # 让模型知道哪些已经是这个群的稳定习惯，不要重复输出。
        existing = sorted(
            (t for t in self.storage.universal.get(session_id, [])
             if t.get("status") != "rejected"),
            key=lambda t: t.get("proficiency", 0),
            reverse=True,
        )[:30]
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
4. evidence 必须从上面的聊天记录里**原样复制**一小段（不超过 40 字，挑最能体现这条风格的那句）。
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
]}}"""

    def _build_prompt(self, session_id: str, history: list[ChatMessage]) -> str:
        lines = []
        for msg in history:
            sender = str(msg.get("sender") or "?")[:24]
            content = str(msg.get("content") or "").replace("\n", " ")[:300]
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
                    "evidence": str(evidence or "").strip()[:120],
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

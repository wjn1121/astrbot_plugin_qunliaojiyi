"""注入器：把学到的特征拼成一段临时内容，挂到 LLM 请求上。

设计取舍（沿用旧版）：
- 注入走 ``extra_user_content_parts`` 且 ``mark_as_temp()``，**不改 system prompt**，
  这样 LLM 的前缀缓存仍然命中，长会话也不会因为风格文本变化而整体重算；
- 稳定风格带「参考而非复读」约束，避免模型每条回复都硬塞 666/233；
- 场景化表达只在当前消息语境匹配时才注入，且最多 2 条。
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

from astrbot.api import logger

from .storage import CATEGORY_SITUATIONAL, CATEGORY_STABLE, SOURCE_MANUAL, Storage
from .types import Trait


class Injector:
    def __init__(self, plugin, storage: Storage):
        self.plugin = plugin
        self.storage = storage
        # 稳定风格文本缓存：数据版本不变时不重复拼装
        self._stable_cache: dict[str, tuple[int, tuple[int, int, int], str | None]] = {}
        # 场景化特征的嵌入向量缓存：内容不变时不重复请求 embedding
        self._emb_cache: dict[str, dict[str, list[float]]] = {}
        self._emb_version: dict[str, int] = {}

    # ── 对外入口 ────────────────────────────────────────────────────────

    def should_inject(self, session_id: str) -> bool:
        settings = self.plugin.settings
        if not settings.get("enabled") or not settings.get("inject_enabled"):
            return False
        if self.storage.traits_of(session_id):
            return True
        if settings.get("enable_cross_group"):
            return any(
                self.storage.traits_of(sid) for sid in self.storage.universal if sid != session_id
            )
        return False

    async def build_injection_text(self, session_id: str, user_message: str = "") -> str | None:
        if not self.should_inject(session_id):
            return None

        settings = self.plugin.settings
        stable, situational = self.storage.split(session_id)

        global_stable: list[dict[str, Any]] = []
        if settings.get("enable_cross_group"):
            global_stable = self.storage.other_stable_except(session_id)

        parts: list[str] = []
        stable_text = self._build_stable_text(session_id, stable, global_stable)
        if stable_text:
            parts.append(stable_text)

        if settings.get("enable_situational_inject"):
            sit_text = await self._build_situational_text(session_id, situational, user_message)
            if sit_text:
                parts.append(sit_text)

        if not parts:
            return None
        text = "\n\n".join(parts)
        if settings.is_debug():
            logger.info(f"[群聊记忆] 调试 · 注入 · {session_id}\n{text}")
        return text

    # ── 稳定风格 ────────────────────────────────────────────────────────

    def _build_stable_text(
        self, session_id: str, local: list[Trait], global_stable: list[Trait]
    ) -> str | None:
        settings = self.plugin.settings
        # 名额也算进缓存键：否则面板把 max_manual_inject 从 3 改成 0 后，
        # 要等到下一次数据变更才会生效，看起来像设置没起作用。
        quota = (
            int(settings.get("max_manual_inject")),
            int(settings.get("max_stable_inject")),
            int(settings.get("max_global_styles")),
        )
        version = self.storage.get_version()
        cached = self._stable_cache.get(session_id)
        if cached is not None and cached[0] == version and cached[1] == quota:
            return cached[2]

        # 手动记忆和学习风格各占自己的名额：手工加多了不会把学到的一条都挤掉，
        # 学习再频繁也顶不掉你明确指定的那几条。
        manual = [t for t in local if t.get("source") == SOURCE_MANUAL]
        learned = [t for t in local if t.get("source") != SOURCE_MANUAL]
        manual_names = self.select_top(manual, quota[0])
        learned_names = self.select_top(learned, quota[1])
        global_names = self.select_top(global_stable, quota[2])

        text = None
        if manual_names or learned_names or global_names:
            lines = ["<style_guidelines>", "以下是从群聊记录里提炼的说话风格参考："]
            if manual_names:
                lines.append(f"本群固定风格（管理员指定，优先遵守）：{'、'.join(manual_names)}")
            if learned_names:
                lines.append(f"本群学习风格：{'、'.join(learned_names)}")
            if global_names:
                lines.append(f"其他群风格（仅作参考）：{'、'.join(global_names)}")
            lines.extend(
                [
                    "使用要求：",
                    "1. 风格只是参考，不是复读指令；不要为了「像群聊」而每句话都强行加 666、233 或语气词。",
                    "2. 只在语境自然合适时使用，优先回应当前用户消息，风格不得覆盖或干扰当前话题。",
                    "3. 不要机械堆砌，也不要每条回复都刻意体现全部风格。",
                    "</style_guidelines>",
                ]
            )
            text = "\n".join(lines)

        self._stable_cache[session_id] = (version, quota, text)
        return text

    # ── 场景化表达 ──────────────────────────────────────────────────────

    async def _build_situational_text(
        self, session_id: str, situational: list[Trait], user_message: str
    ) -> str | None:
        settings = self.plugin.settings
        if not situational:
            return None
        message = (user_message or "").strip()
        trigger_matching = bool(settings.get("enable_trigger_matching"))

        matched: list[tuple[dict, float]] = []
        unresolved: list[dict] = []

        for trait in situational:
            content = str(trait.get("content") or "").strip()
            if not content:
                continue
            context = str(trait.get("context") or "").strip()
            triggers = [str(x) for x in (trait.get("triggers") or []) if str(x).strip()]

            # 1) 触发词命中：纯本地字符串比较，零网络开销，优先级最高
            if message and trigger_matching and any(t in message for t in triggers):
                matched.append((trait, 1.1))
                continue
            # 2) 当前消息里直接出现了这条表达或它的场景描述
            if message and (content in message or (context and context in message)):
                matched.append((trait, 1.0))
                continue
            unresolved.append(trait)

        # 3) 只有在仍有特征没本地命中时，才值得花一次向量请求；
        #    有触发词覆盖的情况下这里完全不会发请求。
        if unresolved and message and settings.get("enable_emb_selection"):
            embedding_provider = self._resolve_embedding_provider()
            query_emb = (
                await self._embed(message, embedding_provider) if embedding_provider else None
            )
            if query_emb:
                threshold = float(settings.get("situational_similarity_threshold"))
                version = self.storage.get_version()
                if self._emb_version.get(session_id) != version:
                    self._emb_cache[session_id] = {}
                    self._emb_version[session_id] = version
                emb_cache = self._emb_cache.setdefault(session_id, {})
                for trait in unresolved:
                    content = str(trait.get("content") or "").strip()
                    context = str(trait.get("context") or "").strip()
                    if not content or not context:
                        continue
                    trait_emb = emb_cache.get(content)
                    if trait_emb is None:
                        trait_emb = await self._embed(
                            f"{content}：{context}", embedding_provider
                        )
                        if trait_emb:
                            emb_cache[content] = trait_emb
                    if trait_emb:
                        score = self._cosine(query_emb, trait_emb)
                        if score >= threshold:
                            matched.append((trait, score))

        if not matched:
            return None
        matched.sort(key=lambda x: x[1], reverse=True)
        top = max(0, int(settings.get("max_situational_inject")))
        # 手动记忆优先占位，剩余名额给学习到的。场景化本身条数就少，
        # 再让学习结果把它挤掉，手工加的基本等于白加。
        manual_hits = [t for t, _ in matched if t.get("source") == SOURCE_MANUAL][:top]
        learned_hits = [t for t, _ in matched if t.get("source") != SOURCE_MANUAL]
        selected = manual_hits + learned_hits[: max(0, top - len(manual_hits))]
        lines = ["当前语境相关的场景化表达（仅供理解与自然接梗，不要主动扩散）："]
        for trait in selected:
            content = trait.get("content", "")
            context = trait.get("context", "")
            lines.append(f"- 「{content}」：{context}" if context else f"- 「{content}」")
        return "\n".join(lines)

    # ── Provider / 向量工具 ─────────────────────────────────────────────

    def _resolve_embedding_provider(self):
        context = self.plugin.context
        configured = (self.plugin.settings.get("embedding_provider_id") or "").strip()
        if configured:
            try:
                provider = context.get_provider_by_id(provider_id=configured)
                if provider and callable(getattr(provider, "get_embedding", None)):
                    return provider
                logger.warning(f"[群聊记忆] 配置的 embedding Provider 不可用: {configured}")
            except Exception:  # noqa: BLE001
                pass
        try:
            providers = context.get_all_embedding_providers()
            return providers[0] if providers else None
        except Exception:  # noqa: BLE001
            return None

    async def _embed(self, text: str, provider) -> list[float] | None:
        """带硬超时的向量请求。

        这个方法跑在 on_llm_request 链路上，embedding 服务一旦变慢就会拖住回复，
        所以超时后直接返回 None，由调用方降级为纯关键词匹配。
        """
        if provider is None:
            return None
        raw_timeout = float(self.plugin.settings.get("embedding_timeout_seconds"))
        timeout = max(0.2, raw_timeout)
        try:
            return await asyncio.wait_for(self._embed_raw(text, provider), timeout=timeout)
        except Exception as e:  # noqa: BLE001 - 超时/失败一律降级，不影响回复
            logger.debug(f"[群聊记忆] embedding 不可用（{type(e).__name__}），本轮降级关键词匹配")
            return None

    @staticmethod
    async def _embed_raw(text: str, provider) -> list[float] | None:
        for method in ("get_embedding", "get_embeddings", "text_embedding"):
            fn = getattr(provider, method, None)
            if not callable(fn):
                continue
            try:
                result = await fn(text)
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[群聊记忆] embedding 调用失败（可忽略）: {e}")
                continue
            if isinstance(result, list) and result:
                return result[0] if isinstance(result[0], list) else result
        return None

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        return dot / (na * nb) if na > 1e-10 and nb > 1e-10 else 0.0

    # ── 选择算法 ────────────────────────────────────────────────────────

    @staticmethod
    def select_top(traits: list[dict], top_n: int) -> list[str]:
        """按熟练度取前 top_n 条内容。名额为 0 时返回空，表示这类不注入。"""
        if top_n <= 0:
            return []
        ordered = sorted(traits, key=lambda t: t.get("proficiency", 0), reverse=True)
        return [str(t["content"]) for t in ordered[:top_n] if t.get("content")]

    def selected_stable_contents(self, session_id: str) -> set[str]:
        """当前会真正进入注入的稳定风格内容，面板标「注入中」用。

        跨群参考那部分不算：它取决于别的群的数据，在本群卡片上标出来只会误导。
        """
        settings = self.plugin.settings
        local, _ = self.storage.split(session_id)
        manual = [t for t in local if t.get("source") == SOURCE_MANUAL]
        learned = [t for t in local if t.get("source") != SOURCE_MANUAL]
        names = self.select_top(manual, int(settings.get("max_manual_inject")))
        names += self.select_top(learned, int(settings.get("max_stable_inject")))
        return set(names)

    def limits(self) -> dict[str, int]:
        """当前注入名额，面板用来解释「为什么这条没入选」。"""
        settings = self.plugin.settings
        return {
            "manual": int(settings.get("max_manual_inject")),
            "learned": int(settings.get("max_stable_inject")),
            "situational": int(settings.get("max_situational_inject")),
        }

    def summary(self, session_id: str) -> dict[str, Any]:
        learned_stable, learned_sit = self.storage.learned_split(session_id)
        manual = self.storage.manual_of(session_id)
        manual_stable = [t for t in manual if t.get("category", CATEGORY_STABLE) == CATEGORY_STABLE]
        manual_sit = [t for t in manual if t.get("category") == CATEGORY_SITUATIONAL]
        return {
            "has_styles": bool(learned_stable or learned_sit or manual),
            "stable_count": len(learned_stable),
            "situational_count": len(learned_sit),
            "manual_count": len(manual),
            "manual_stable_count": len(manual_stable),
            "manual_situational_count": len(manual_sit),
            "stable_preview": [t.get("content") for t in learned_stable[:3]],
            "situational_preview": [
                {"content": t.get("content"), "context": t.get("context", "")}
                for t in learned_sit[:3]
            ],
        }

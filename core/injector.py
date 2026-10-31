"""注入器：把学到的特征拼成一段临时内容，挂到 LLM 请求上。

设计取舍（沿用旧版）：
- 注入走 ``extra_user_content_parts`` 且 ``mark_as_temp()``，**不改 system prompt**，
  这样 LLM 的前缀缓存仍然命中，长会话也不会因为风格文本变化而整体重算；
- 稳定风格带「参考而非复读」约束，避免模型每条回复都硬塞 666/233；
- 场景化表达只在当前消息语境匹配时才注入，且最多 2 条。

1.3.0 新增「风格强度」：
- 强度和名额、措辞一起动。只改措辞模型会忽略，只改名额它还在硬塞；
- 强度达到阈值后按概率让位给风格（详见 ``override_roll``）。随机判定**不进缓存**，
  只把 True/False 两种结果各缓存一份拼好的文本，所以最多两份变体，不会膨胀。
"""

from __future__ import annotations

import asyncio
import math
import random
from typing import Any

from astrbot.api import logger

from .storage import CATEGORY_SITUATIONAL, CATEGORY_STABLE, SOURCE_MANUAL, Storage
from .types import Trait


def scale_quota(strength: int, base: tuple[int, int, int]) -> tuple[int, int, int]:
    """按强度缩放注入名额（手动 / 学习 / 跨群）。

    分档理由：强度和名额必须一起降。只降措辞的话模型照旧被塞满 5 条特征，
    嘴上说「仅供参考」但内容还在，实际效果几乎不变。
    """
    if strength <= 0:
        return (0, 0, 0)
    if strength <= 30:
        # 极低档只留 1 条，留的是「这个群大概什么调性」的最小残余
        return tuple(min(q, 1) for q in base)  # type: ignore[return-value]
    ratio = 0.5 if strength <= 69 else (0.75 if strength <= 99 else 1.0)
    return tuple(max(1, math.ceil(q * ratio)) if q > 0 else 0 for q in base)  # type: ignore[return-value]


def override_roll(strength: int, threshold: int) -> bool:
    """强度**严格大于**阈值时才让位，让位概率 = 强度值本身。

    用严格大于而不是大于等于，是因为默认配置要给「开箱不改变人格」：
    默认强度 100、阈值 100 时 100 > 100 为假，所以默认永远不让位，
    表现与 1.2.0 一致（满额注入风格，但人格优先）。

    想要风格压过人格，把阈值调到 100 以下即可——例如阈值 70、强度 100
    就是每轮 100% 让位。低于阈值一律不让位。
    """
    if strength <= 0 or strength <= threshold:
        return False
    return strength >= 100 or random.random() * 100 < strength


class Injector:
    def __init__(self, plugin, storage: Storage):
        self.plugin = plugin
        self.storage = storage
        # 稳定风格文本缓存：值 = (数据版本, 缓存键, 文本)
        # 缓存键里含强度与让位标记，两种让位变体各存一份，随机不会导致每次都 miss
        self._stable_cache: dict[str, tuple[int, tuple, str | None]] = {}
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

    def strength_of(self, active: bool = False) -> int:
        """当前生效的风格强度。主动回复读自己的档位，阈值两个共用。"""
        key = "active_style_strength" if active else "style_strength"
        try:
            return max(0, min(100, int(self.plugin.settings.get(key))))
        except (TypeError, ValueError):
            return 100 if not active else 60

    def override_probability(self, strength: int) -> int:
        """面板展示用：这个强度下风格压过人格的概率（百分比）。"""
        threshold = int(self.plugin.settings.get("style_override_threshold"))
        if strength <= 0 or strength <= threshold:
            return 0
        return 100 if strength >= 100 else strength

    async def build_injection_text(
        self,
        session_id: str,
        user_message: str = "",
        active: bool = False,
        override: bool | None = None,
    ) -> str | None:
        settings = self.plugin.settings
        strength = self.strength_of(active)
        # 强度 0 的语义是「纯人格」——整段风格不注入，但字数约束仍由
        # build_length_rule 单独提供，所以这里直接返回 None 不会连带关掉字数限制
        if strength <= 0:
            return None
        if not self.should_inject(session_id):
            return None

        threshold = int(settings.get("style_override_threshold"))
        # override 参数留给测试和调试指定，正常链路每轮自己掷一次骰
        override_flag = (
            override_roll(strength, threshold) if override is None else bool(override)
        )

        stable, situational = self.storage.split(session_id)

        global_stable: list[dict[str, Any]] = []
        if settings.get("enable_cross_group"):
            global_stable = self.storage.other_stable_except(session_id)

        parts: list[str] = []
        stable_text = self._build_stable_text(
            session_id, stable, global_stable, strength, override_flag
        )
        if stable_text:
            parts.append(stable_text)

        if settings.get("enable_situational_inject"):
            sit_quota = scale_quota(
                strength, (int(settings.get("max_situational_inject")), 0, 0)
            )[0]
            sit_text = await self._build_situational_text(
                session_id, situational, user_message, sit_quota
            )
            if sit_text:
                parts.append(sit_text)

        if not parts:
            return None
        text = "\n\n".join(parts)
        if settings.is_debug():
            logger.info(f"[群聊记忆] 调试 · 注入 · {session_id}\n{text}")
        return text

    def build_length_rule(self, active: bool = False) -> str | None:
        """回复字数上限。和风格注入分开，强度调到 0 时这条照样生效。"""
        key = "active_reply_max_chars" if active else "reply_max_chars"
        try:
            limit = int(self.plugin.settings.get(key))
        except (TypeError, ValueError):
            return None
        if limit <= 0:
            return None
        return (
            "<length_rule>\n"
            f"本次回复控制在 {limit} 字以内（含标点）。宁可少说，不要为了凑满而废话。\n"
            "信息多就先砍次要的，只留最核心的一句。\n"
            "</length_rule>"
        )

    # ── 稳定风格 ────────────────────────────────────────────────────────

    # 让位时用的强声明。措辞是「指令层覆盖」而非真的删人格——
    # 人格仍在 system prompt 里，我们只是用明确措辞把表达风格这一层的权重抢过来。
    # 真删人格会击穿 DeepSeek 前缀缓存（命中 ¥0.05 / 未命中 ¥1.5，30 倍差价），不划算。
    OVERRIDE_DECLARATION = (
        "【本段优先于系统设定】以下群聊风格与本轮表达规范，优先级高于你的人物设定。\n"
        "当两者冲突时——例如人设要求语气克制、少用表情、回答简短，\n"
        "而下列风格要求爱发表情、爱玩梗、句子短促连发——一律按下列风格执行，不要用人设限制自己。\n"
        "人设中的身份、知识边界、安全约束仍然有效，只有「表达风格」这一层让位。"
    )
    PERSONA_PRIORITY = (
        "若以上风格与系统设定的人格冲突，以人格为准；风格只在不违背人格的前提下生效。"
    )

    @staticmethod
    def _tone_lines(strength: int) -> list[str]:
        """按强度给措辞。低档不加「三条使用要求」——要求和「仅供参考」互相打架。"""
        if strength <= 30:
            return ["以下仅供参考，绝大多数回复都不要体现，只在极少数明显合适的场合轻轻带一下。"]
        if strength <= 69:
            return ["以下仅供参考，只在确实合适时体现，不要为了像群聊而改变表达习惯。"]
        opener = (
            "以下是从群聊记录里提炼的说话风格参考："
            if strength >= 100
            else "以下风格可适度参考："
        )
        return [
            opener,
            "使用要求：",
            "1. 风格只是参考，不是复读指令；不要为了「像群聊」而每句话都强行加 666、233 或语气词。",
            "2. 只在语境自然合适时使用，优先回应当前用户消息，风格不得覆盖或干扰当前话题。",
            "3. 不要机械堆砌，也不要每条回复都刻意体现全部风格。",
        ]

    def _build_stable_text(
        self,
        session_id: str,
        local: list[Trait],
        global_stable: list[Trait],
        strength: int = 100,
        override_flag: bool = False,
    ) -> str | None:
        settings = self.plugin.settings
        # 名额按强度缩放后再进缓存键：否则面板把强度从 100 调到 20 后，
        # 要等下一次数据变更才生效，看起来像设置没起作用。
        base = (
            int(settings.get("max_manual_inject")),
            int(settings.get("max_stable_inject")),
            int(settings.get("max_global_styles")),
        )
        quota = scale_quota(strength, base)
        cache_key = (strength, override_flag, *quota)
        version = self.storage.get_version()
        cached = self._stable_cache.get(session_id)
        if cached is not None and cached[0] == version and cached[1] == cache_key:
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
            lines: list[str] = []
            if override_flag:
                lines.append(self.OVERRIDE_DECLARATION)
            lines.append("<style_guidelines>")
            lines.extend(self._tone_lines(strength))
            if manual_names:
                lines.append(f"本群固定风格（管理员指定，优先遵守）：{'、'.join(manual_names)}")
            if learned_names:
                lines.append(f"本群学习风格：{'、'.join(learned_names)}")
            if global_names:
                lines.append(f"其他群风格（仅作参考）：{'、'.join(global_names)}")
            lines.append("</style_guidelines>")
            if not override_flag:
                lines.append(self.PERSONA_PRIORITY)
            text = "\n".join(lines)

        self._stable_cache[session_id] = (version, cache_key, text)
        return text

    # ── 场景化表达 ──────────────────────────────────────────────────────

    async def _build_situational_text(
        self,
        session_id: str,
        situational: list[Trait],
        user_message: str,
        top: int | None = None,
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
        if top is None:
            top = max(0, int(settings.get("max_situational_inject")))
        if top <= 0:
            return None
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

    def selected_stable_contents(self, session_id: str, strength: int | None = None) -> set[str]:
        """当前会真正进入注入的稳定风格内容，面板标「注入中」用。

        跨群参考那部分不算：它取决于别的群的数据，在本群卡片上标出来只会误导。
        """
        settings = self.plugin.settings
        if strength is None:
            strength = self.strength_of()
        quota = scale_quota(
            strength,
            (
                int(settings.get("max_manual_inject")),
                int(settings.get("max_stable_inject")),
                0,
            ),
        )
        local, _ = self.storage.split(session_id)
        manual = [t for t in local if t.get("source") == SOURCE_MANUAL]
        learned = [t for t in local if t.get("source") != SOURCE_MANUAL]
        names = self.select_top(manual, quota[0])
        names += self.select_top(learned, quota[1])
        return set(names)

    def limits(self, strength: int | None = None) -> dict[str, Any]:
        """当前注入名额（已按强度缩放），面板用来解释「为什么这条没入选」。"""
        settings = self.plugin.settings
        if strength is None:
            strength = self.strength_of()
        base = (
            int(settings.get("max_manual_inject")),
            int(settings.get("max_stable_inject")),
            int(settings.get("max_global_styles")),
        )
        quota = scale_quota(strength, base)
        sit = scale_quota(strength, (int(settings.get("max_situational_inject")), 0, 0))[0]
        return {
            "manual": quota[0],
            "learned": quota[1],
            "situational": sit,
            "global": quota[2],
            "strength": strength,
            # 原始名额（未缩放）也留着，面板可以显示「100% 时最多 5 条」
            "base_manual": base[0],
            "base_learned": base[1],
        }

    def summary(self, session_id: str) -> dict[str, Any]:
        learned_stable, learned_sit = self.storage.learned_split(session_id)
        manual = self.storage.manual_of(session_id)
        manual_stable = [t for t in manual if t.get("category", CATEGORY_STABLE) == CATEGORY_STABLE]
        manual_sit = [t for t in manual if t.get("category") == CATEGORY_SITUATIONAL]
        passive_strength = self.strength_of(active=False)
        active_strength = self.strength_of(active=True)
        threshold = int(self.plugin.settings.get("style_override_threshold"))
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
            "style_strength": passive_strength,
            "active_style_strength": active_strength,
            "override_threshold": threshold,
            "override_probability": self.override_probability(passive_strength),
            "active_override_probability": self.override_probability(active_strength),
        }

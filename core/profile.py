"""按 QQ 号的个人画像：从事实里提炼「这个人是谁」，单独一段注入。

和事实记忆的分工，是这个模块存在的全部理由：

- **事实**：具体的、会过期的（"周三交论文""这周末想改完插件"）——
  按当前话题挑相关的几条，30 天没人提就归档
- **画像**：稳定的、抽象的（"考研党，爱玩碧蓝航线，说话爱带梗"）——
  固定注入，不按话题挑，让人设对着这个人稳下来

为什么不让事实直接充当画像：事实是一串互不相干的句子，模型只能从里面
现推"这人是什么人"；而画像是一段连续的描述，读起来就是"认识这个人"。
两者信息重叠但不重复——画像回答"他是谁"，事实回答"他最近在干嘛"。

**重写频率极低**（每人每天最多一次）：
画像不是每轮都算的，那样等于每轮多一次请求。它只在事实积累到阈值时
异步重写，注入时读缓存。重写失败/超时一律回退成本地拼装，绝不影响回复。

字数上限按好感档位给（见 ``PROFILE_CHARS``）：越熟画像越厚，
陌生人只有一句话，不待见干脆不写——陌生人几十个，给他们写画像是纯烧钱。
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from astrbot.api import logger

from .facts import KIND_LABEL, TIER_CORE, TIER_DEEP, condense, tier_of

BEIJING_TZ = timezone(timedelta(hours=8))

# 各档位的画像字数上限。0 = 不生成、不注入。
#
# 为什么按好感而不是一刀切：画像的用途是「让 bot 对这个人有稳定的印象」，
# 而「印象该有多具体」本来就该跟亲疏走——刚认识的人知道个大概就行，
# 老友才值得写两百字。这同时也是最自然的省钱方式：陌生人占绝大多数，
# 他们那 60 字是唯一成本，不待见直接 0。
PROFILE_CHARS = {
    "hostile": 0,
    "stranger": 80,
    "familiar": 130,
    "close": 190,
    "intimate": 260,
}
# 主人另给：他是「养得最久」的那条线，画像该最厚。
#
# 520 是「大幅加」的结果：画像质量受字数直接约束——200 字只能写
# 「他是玩手游的、在改插件」，500 字才写得出「说话实在、带点自嘲、
# 是群里既干活又闲聊的那类人」这种能影响语气的描述。
# 代价约 325 token/轮，主人这条线值这个钱。
OWNER_PROFILE_CHARS = 520
# 关系功能没开时按「熟悉」档：不然整个画像功能对没开关系的人等于不存在
FALLBACK_STAGE = "familiar"

# 单人手调的字数上限（面板上填的数字）。给个硬顶是防手滑填 99999——
# 那等于把整份事实原文塞进每一轮 prompt，比不做画像还贵
MAX_PROFILE_CHARS = 800

# 重写时最多喂多少条事实进去。多了 prompt 变贵，而且后面的信息
# 对「这个人是谁」没什么增量
REWRITE_MAX_FACTS = 30
# 本地拼装（LLM 不可用时的兜底）每条最多留多少字
LOCAL_FACT_CHARS = 28

REWRITE_SYSTEM_PROMPT = (
    "你在整理群聊机器人的「人物画像」。只输出画像正文，不要标题、不要解释、"
    "不要 markdown、不要分点符号。用第三人称陈述句，像在跟同事介绍一个群友。"
)


class ProfileStore:
    """画像的存取、重写与注入文本。"""

    def __init__(self, plugin, storage, settings) -> None:
        self.plugin = plugin
        self.storage = storage
        self.settings = settings
        # 同一时刻只允许一个人重写，防止一次学习触发十几个并发请求
        self._busy: set[str] = set()
        # 每天重写次数上限。放在内存里就够：重启清零最多多写几个，
        # 而落盘一个计数器反而要处理跨天重置和文件损坏
        self._day = ""
        self._day_count = 0

    # ── 配置 ────────────────────────────────────────────────────────────

    def enabled(self) -> bool:
        try:
            return bool(self.settings.get("profile_enabled"))
        except Exception:  # noqa: BLE001
            return False

    def _int_setting(self, key: str, fallback: int) -> int:
        try:
            return int(self.settings.get(key) or fallback)
        except (TypeError, ValueError):
            return fallback

    def quota_of(self, sender_id: str) -> int:
        """这个人的画像字数上限。0 表示不生成也不注入。

        优先级：**单人手动设的 > 档位默认**。手动值是用户对这个 QQ 的
        单独授权（"这个人值得写详细点"），不该被档位覆盖；把它存在
        profiles.json 里，跟着画像一起走。
        """
        if not self.enabled():
            return 0
        try:
            override = int((self.record(sender_id) or {}).get("chars") or 0)
        except (TypeError, ValueError):
            override = 0
        if override > 0:
            return min(override, MAX_PROFILE_CHARS)
        return self.stage_quota_of(sender_id)

    def stage_quota_of(self, sender_id: str) -> int:
        """按好感档位算的默认上限（不含单人手调）。"""
        try:
            bond = getattr(self.plugin, "bond", None)
            if bond is not None and bond.is_owner(sender_id):
                return OWNER_PROFILE_CHARS
            stage = ""
            if bond is not None:
                stage = str(bond.stage_now(sender_id) or "")
        except Exception:  # noqa: BLE001
            stage = ""
        if not stage:
            stage = FALLBACK_STAGE
        return int(PROFILE_CHARS.get(stage, PROFILE_CHARS[FALLBACK_STAGE]))

    # ── 读写 ────────────────────────────────────────────────────────────

    def record(self, sender_id: str) -> dict:
        raw = self.storage.profiles.get(str(sender_id))
        return dict(raw) if isinstance(raw, dict) else {}

    def body_of(self, sender_id: str, limit: int | None = None) -> str:
        """拼出要注入的画像正文（人工段在前，自动段在后）。

        人工段永远优先：用户亲手写的「他是我室友，别调侃学历」比模型
        总结的一百字都管用，而且不该被下一次自动重写覆盖。
        """
        rec = self.record(sender_id)
        if not rec:
            return ""
        cap = limit if limit is not None else self.quota_of(sender_id)
        if cap <= 0:
            return ""
        manual = str(rec.get("manual") or "").strip()
        auto = str(rec.get("text") or "").strip()
        parts = [x for x in (manual, auto) if x]
        if not parts:
            return ""
        body = "；".join(parts)
        if len(body) <= cap:
            return body
        # 截断优先保住人工段：人工段自己就超限时只留人工段
        if manual and len(manual) >= cap:
            return manual[: cap - 1] + "…"
        room = cap - len(manual) - (1 if manual else 0)
        if room <= 0:
            return manual
        return (manual + "；" if manual else "") + auto[: max(0, room - 1)] + "…"

    def set_chars(self, sender_id: str, chars: int) -> int:
        """给某个 QQ 单独设画像字数上限。0 = 恢复档位默认。

        单独设的值存在画像记录里（``chars``），比重写覆盖更稳：
        改档位不会把它冲掉，删画像也不会连带清掉。
        """
        key = str(sender_id)
        rec = dict(self.storage.profiles.get(key) or {})
        try:
            value = max(0, min(MAX_PROFILE_CHARS, int(chars)))
        except (TypeError, ValueError):
            value = 0
        if value:
            rec["chars"] = value
        else:
            rec.pop("chars", None)
        self.storage.profiles[key] = rec
        self.storage._mark("profiles")
        logger.info(f"[群聊记忆] 画像字数上限 · {key} → {value or '档位默认'}")
        return value

    def save_manual(self, sender_id: str, text: str) -> None:
        """保存人工补充段。空字符串等于清掉。"""
        key = str(sender_id)
        rec = dict(self.storage.profiles.get(key) or {})
        text = str(text or "").strip()
        if text:
            rec["manual"] = text[:400]
        else:
            rec.pop("manual", None)
        rec["manual_at"] = round(time.time(), 1)
        self.storage.profiles[key] = rec
        self.storage._mark("profiles")
        logger.info(f"[群聊记忆] 已保存个人画像人工段 · {key}")

    def _save(self, sender_id: str, text: str, basis: int) -> None:
        key = str(sender_id)
        rec = dict(self.storage.profiles.get(key) or {})
        rec["text"] = text
        rec["updated_at"] = round(time.time(), 1)
        # basis = 这次重写时基于多少条事实。下一次靠它判断「值不值得再写一遍」
        rec["basis"] = int(basis)
        self.storage.profiles[key] = rec
        self.storage._mark("profiles")

    # ── 重写 ────────────────────────────────────────────────────────────

    def _facts_of(self, sender_id: str) -> list[dict]:
        items = self.storage.facts.get(str(sender_id))
        if not isinstance(items, list):
            return []
        alive = [
            dict(i)
            for i in items
            if str(i.get("status") or "") == "active" and str(i.get("text") or "").strip()
        ]
        order = {TIER_CORE: 0, TIER_DEEP: 1}
        alive.sort(key=lambda i: (order.get(tier_of(i), 2), -int(i.get("seen") or 1)))
        return alive

    def worth_rewriting(self, sender_id: str, now: float | None = None) -> tuple[bool, str]:
        """要不要给这个人重写画像。返回 ``(要不要, 原因)``，原因用于日志。"""
        if not self.enabled():
            return False, "disabled"
        cap = self.quota_of(sender_id)
        if cap <= 0:
            return False, "no_quota"
        at = time.time() if now is None else float(now)
        items = self._facts_of(sender_id)
        min_facts = max(1, self._int_setting("profile_min_facts", 3))
        if len(items) < min_facts:
            return False, f"facts<{min_facts}"

        rec = self.record(sender_id)
        if not str(rec.get("text") or "").strip():
            return True, "first"
        hours = max(1, self._int_setting("profile_rewrite_hours", 24))
        if at - float(rec.get("updated_at") or 0) < hours * 3600:
            return False, "interval"
        # 事实没怎么变就别重写：同样的料写两遍，结果只会抖动
        if len(items) - int(rec.get("basis") or 0) < 2:
            return False, "no_new_facts"
        return True, "stale"

    def _budget_ok(self) -> bool:
        """今天的重写次数还没用光。按北京时间自然日重置。"""
        today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
        if today != self._day:
            self._day = today
            self._day_count = 0
        cap = max(0, self._int_setting("profile_daily_cap", 6))
        return self._day_count < cap

    async def maybe_rewrite(
        self, sender_id: str, session_id: str = "", force: bool = False
    ) -> bool:
        """满足条件就重写一次。任何异常都吞掉——画像坏了不能影响聊天。

        ``force`` 是面板上「重新生成」那个按钮：跳过间隔和「有没有新料」的检查，
        因为用户主动点了就是要现在看到结果。日限仍然生效——不然连点几下
        就能绕过所有节流。
        """
        try:
            if force:
                if not self.enabled() or self.quota_of(sender_id) <= 0:
                    return False
                if len(self._facts_of(sender_id)) < 2:
                    return False
            else:
                ok, reason = self.worth_rewriting(sender_id)
                if not ok:
                    return False
            if not self._budget_ok():
                return False
            key = str(sender_id)
            if key in self._busy:
                return False
            self._busy.add(key)
            try:
                return await self._rewrite(key, session_id)
            finally:
                self._busy.discard(key)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[群聊记忆] 个人画像重写跳过（{sender_id}）: {e}")
            return False

    async def _rewrite(self, sender_id: str, session_id: str) -> bool:
        items = self._facts_of(sender_id)
        cap = self.quota_of(sender_id)
        if not items or cap <= 0:
            return False
        self._day_count += 1

        provider = await self._resolve_provider(session_id)
        text = ""
        if provider is not None:
            text = await self._ask(provider, sender_id, items, cap)
        if not text:
            # 回退：本地按类拼装。不是「失败」而是「这次先用粗糙版本」——
            # 有画像总比没有强，下一轮还会再试
            text = self._local_text(items, cap)
            logger.info(f"[群聊记忆] 个人画像用本地拼装（{sender_id}）")
        if not text:
            return False
        self._save(sender_id, text, len(items))
        return True

    async def _ask(self, provider, sender_id: str, items: list[dict], cap: int) -> str:
        lines = []
        for item in items[:REWRITE_MAX_FACTS]:
            kind = KIND_LABEL.get(str(item.get("kind") or ""), "事")
            lines.append(f"- [{kind}] {str(item.get('text') or '')[:80]}")
        rec = self.record(sender_id)
        manual = str(rec.get("manual") or "").strip()
        prompt = (
            f"下面是机器人记住的、关于某位群友的事实（每行一条）：\n"
            + "\n".join(lines)
            + "\n\n"
            + (
                f"用户还亲手补充了这些，**必须优先保留**：{manual}\n\n" if manual else ""
            )
            + f"把这些整理成一段不超过 {cap} 字的画像，说明「这个人是谁」：\n"
            "- 只写稳定的东西：身份、长期偏好、说话风格、在群里的角色\n"
            "- 会过期的事（这周末要干嘛、今天在忙什么）不要写进画像，那些留给事实记忆\n"
            "- 第三人称，不要出现「他/她」以外的称呼，不要写「根据聊天记录」这类来源说明\n"
            f"- 直接输出正文，控制在 {cap} 字以内"
        )
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt, contexts=[], system_prompt=REWRITE_SYSTEM_PROMPT
                ),
                timeout=30.0,
            )
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[群聊记忆] 画像重写请求失败: {e}")
            return ""
        try:
            self.plugin.usage.record("profile", getattr(resp, "usage", None), session_id)
        except Exception:  # noqa: BLE001 - 记账失败不影响功能
            pass
        return self._clean(str(getattr(resp, "completion_text", "") or ""), cap)

    @staticmethod
    def _clean(text: str, cap: int) -> str:
        """清掉模型爱加的东西：标题、分点、markdown、引号。"""
        body = str(text or "").strip()
        if not body:
            return ""
        body = re.sub(r"^\s*#{1,6}\s*", "", body)
        body = re.sub(r"^\s*(?:画像|人物画像|简介)[：:]\s*", "", body)
        body = body.replace("**", "").replace("`", "")
        body = re.sub(r"^\s*[-*·]\s*", "", body, flags=re.M)
        body = re.sub(r"\s*\n+\s*", "；", body)
        body = body.strip("；。 \"'“”")
        if len(body) > cap:
            body = body[: cap - 1] + "…"
        return body

    def _local_text(self, items: list[dict], cap: int) -> str:
        """本地拼装：按类聚成几句，够用就行，绝不发请求。"""
        buckets: dict[str, list[str]] = {}
        for item in items:
            kind = str(item.get("kind") or "fact")
            text = condense(item.get("text"), LOCAL_FACT_CHARS)
            if text:
                buckets.setdefault(kind, []).append(text)
        segs = []
        for kind, texts in buckets.items():
            segs.append(f"{KIND_LABEL.get(kind, '事')}：{'、'.join(texts)}")
        body = "；".join(segs)
        if len(body) > cap:
            body = body[: cap - 1] + "…"
        return body

    async def _resolve_provider(self, session_id: str):
        """和学习链路用同一个 provider（``analysis_provider_id``）。"""
        try:
            provider_id = str(self.settings.get("analysis_provider_id") or "").strip()
            context = self.plugin.context
            if provider_id:
                provider = context.get_provider_by_id(provider_id=provider_id)
                if provider is not None:
                    return provider
            try:
                return context.get_using_provider(umo=session_id)
            except TypeError:
                return context.get_using_provider()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[群聊记忆] 画像重写拿不到 provider: {e}")
            return None

    # ── 面板 ────────────────────────────────────────────────────────────

    def snapshot(self, sender_id: str) -> dict[str, Any]:
        rec = self.record(sender_id)
        return {
            "text": str(rec.get("text") or ""),
            "manual": str(rec.get("manual") or ""),
            # chars = 手调值（0/空 = 跟着档位走），stage_quota = 档位默认，
            # 面板要同时显示两者，用户才知道自己覆盖了什么
            "chars": int(rec.get("chars") or 0),
            "stage_quota": self.stage_quota_of(sender_id),
            "quota": self.quota_of(sender_id),
            "basis": int(rec.get("basis") or 0),
            "updated_at": float(rec.get("updated_at") or 0),
            "enabled": self.enabled(),
        }

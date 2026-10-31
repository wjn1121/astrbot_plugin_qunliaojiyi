"""学习者：把攒下的聊天记录交给 LLM，提炼成稳定风格 + 场景化表达。

关键成本控制（相对烤箱之心新版的定时任务）：
- **按会话整批提炼**，不是每条消息都请求；
- 只有攒够 ``min_history_for_analysis`` 条才发请求；
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

from .storage import Storage
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
                f"[群聊记忆] {session_id} 学习完成：稳定 {len(parsed['universal'])} 条，"
                f"场景化 {len(parsed['situational'])} 条，耗时 {elapsed:.1f}s"
            )
            return {
                "ok": True,
                "stable": len(parsed["universal"]),
                "situational": len(parsed["situational"]),
                "elapsed": round(elapsed, 2),
                "universal": parsed["universal"],
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

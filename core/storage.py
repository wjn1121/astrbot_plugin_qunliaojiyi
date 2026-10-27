"""数据层：风格特征、聊天缓冲、学习状态的持久化。

沿用旧版 ``universal.json`` / ``chat_history.json`` 的结构，方便直接导入历史备份：
- ``universal.json``   ``{unified_msg_origin: [trait, ...]}``
- ``chat_history.json`` ``{unified_msg_origin: [message, ...]}``

额外新增 ``learn_state.json`` 记录每个会话的学习元信息（上次学习时间、累计次数、
最后一次结果），只供面板展示，不参与注入逻辑，删掉也能自动重建。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .types import ChatMessage, LearnState, Metrics, Trait

CATEGORY_STABLE = "stable"
CATEGORY_SITUATIONAL = "situational"


class Storage:
    """JSON 存储，写盘做 5 秒合并，避免高频小写入。"""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.universal: dict[str, list[Trait]] = self._load("universal.json")
        self.chat_history: dict[str, list[ChatMessage]] = self._load("chat_history.json")
        self.learn_state: dict[str, LearnState] = self._load("learn_state.json")
        self.metrics: Metrics = self._load("metrics.json")

        self._lock = asyncio.Lock()
        self._version = 0
        self._dirty = {
            "universal": False,
            "chat_history": False,
            "learn_state": False,
            "metrics": False,
        }
        self._save_task: asyncio.Task | None = None
        self._save_delay = 5.0

    # ── 读写工具 ────────────────────────────────────────────────────────

    def _load(self, filename: str) -> dict:
        path = self.data_dir / filename
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError) as e:
            logger.error(f"[群聊记忆] 加载 {filename} 失败: {e}")
            return {}

    def _mark(self, *keys: str) -> None:
        for key in keys:
            self._dirty[key] = True
        # 同步上下文（如被别处直接调用）没有事件循环，此时只打脏标记，
        # 由显式 flush() 写盘；有循环时才起延迟合并任务。
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._save_task is None or self._save_task.done():
            self._save_task = loop.create_task(self._delayed_save())

    async def _delayed_save(self) -> None:
        await asyncio.sleep(self._save_delay)
        await self.flush()

    async def flush(self) -> None:
        async with self._lock:
            if self._dirty["universal"]:
                await self._write("universal.json", self.universal)
                self._dirty["universal"] = False
            if self._dirty["chat_history"]:
                await self._write("chat_history.json", self.chat_history)
                self._dirty["chat_history"] = False
            if self._dirty["learn_state"]:
                await self._write("learn_state.json", self.learn_state)
                self._dirty["learn_state"] = False
            if self._dirty["metrics"]:
                await self._write("metrics.json", self.metrics)
                self._dirty["metrics"] = False

    async def _write(self, filename: str, data: dict) -> None:
        try:
            (self.data_dir / filename).write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as e:
            logger.error(f"[群聊记忆] 保存 {filename} 失败: {e}")

    # ── 风格特征 ────────────────────────────────────────────────────────

    def get_version(self) -> int:
        """数据版本号，特征变更时递增，供注入缓存失效。"""
        return self._version

    def traits_of(self, session_id: str) -> list[Trait]:
        return self.universal.get(session_id, [])

    def split(self, session_id: str) -> tuple[list[Trait], list[Trait]]:
        traits = self.traits_of(session_id)
        stable = [t for t in traits if t.get("category", CATEGORY_STABLE) == CATEGORY_STABLE]
        situational = [t for t in traits if t.get("category") == CATEGORY_SITUATIONAL]
        return stable, situational

    def other_stable_except(self, session_id: str) -> list[Trait]:
        out: list[Trait] = []
        for sid, traits in self.universal.items():
            if sid == session_id:
                continue
            out.extend(t for t in traits if t.get("category", CATEGORY_STABLE) == CATEGORY_STABLE)
        return out

    def replace_traits(
        self,
        session_id: str,
        stable: list[str],
        situational: list[dict[str, Any]] | None = None,
        *,
        limits: tuple[int, int] | None = None,
    ) -> None:
        """用新提炼结果替换旧特征，同时保留同名特征的熟练度累积。

        limits=(稳定上限, 场景上限)，超出的按熟练度从低到高淘汰；
        这样长期运行也不会让特征越攒越多、把学习 prompt 撑大。
        """
        now = time.time()
        old = {t.get("content"): t for t in self.universal.get(session_id, [])}

        def make(
            content: str, category: str, context: str = "", triggers: list[str] | None = None
        ) -> Trait:
            prev = old.get(content, {})
            return {
                "content": content,
                "category": category,
                "context": context,
                "triggers": list(triggers or prev.get("triggers") or []),
                "proficiency": min(100, int(prev.get("proficiency", 0) or 0) + 5),
                "confirmed_rounds": int(prev.get("confirmed_rounds", 0) or 0) + 1,
                "last_seen": now,
                "last_updated": now,
            }

        traits = [make(c, CATEGORY_STABLE) for c in stable if c.strip()]
        for item in situational or []:
            content = str(item.get("content") or "").strip()
            if content:
                traits.append(
                    make(
                        content,
                        CATEGORY_SITUATIONAL,
                        str(item.get("context") or "").strip(),
                        item.get("triggers"),
                    )
                )
        if traits:
            self.universal[session_id] = traits
        else:
            self.universal.pop(session_id, None)
        self._version += 1
        if limits:
            self.trim_traits(session_id, limits)
        self._mark("universal")

    def trim_traits(self, session_id: str, limits: tuple[int, int]) -> int:
        """按类别裁剪特征数量，返回被删除的条数。"""
        traits = self.universal.get(session_id, [])
        if not traits:
            return 0
        stable_max, sit_max = max(0, limits[0]), max(0, limits[1])
        stable = [t for t in traits if t.get("category", CATEGORY_STABLE) == CATEGORY_STABLE]
        situational = [t for t in traits if t.get("category") == CATEGORY_SITUATIONAL]

        def top(items: list[dict], n: int) -> list[dict]:
            if n <= 0:
                return []
            return sorted(items, key=lambda t: t.get("proficiency", 0), reverse=True)[:n]

        kept = top(stable, stable_max) + top(situational, sit_max)
        dropped = len(traits) - len(kept)
        if dropped > 0:
            self.universal[session_id] = kept
            self._version += 1
            self._mark("universal")
        return dropped

    async def delete_trait(self, session_id: str, content: str) -> bool:
        traits = self.universal.get(session_id, [])
        remaining = [t for t in traits if t.get("content") != content]
        if len(remaining) == len(traits):
            return False
        if remaining:
            self.universal[session_id] = remaining
        else:
            self.universal.pop(session_id, None)
        self._version += 1
        self._mark("universal")
        return True

    async def clear_session(self, session_id: str) -> bool:
        changed = self.universal.pop(session_id, None) is not None
        self.chat_history.pop(session_id, None)
        self.learn_state.pop(session_id, None)
        self._version += 1
        self._mark("universal", "chat_history", "learn_state")
        return changed

    async def clear_all(self) -> int:
        count = len(self.universal)
        self.universal.clear()
        self.chat_history.clear()
        self.learn_state.clear()
        self._version += 1
        self._mark("universal", "chat_history", "learn_state")
        return count

    # ── 聊天缓冲 ────────────────────────────────────────────────────────

    def add_message(self, session_id: str, message: ChatMessage, limit: int) -> None:
        bucket = self.chat_history.setdefault(session_id, [])
        bucket.append(message)
        if limit > 0 and len(bucket) > limit:
            del bucket[: len(bucket) - limit]
        self._mark("chat_history")

    def history_of(self, session_id: str, limit: int = 50) -> list[ChatMessage]:
        return self.chat_history.get(session_id, [])[-limit:]

    def history_counts(self) -> dict[str, int]:
        return {sid: len(msgs) for sid, msgs in self.chat_history.items() if msgs}

    async def clear_history(self, session_id: str) -> None:
        self.chat_history.pop(session_id, None)
        self._mark("chat_history")

    # ── 学习状态 ────────────────────────────────────────────────────────

    def record_learn(
        self,
        session_id: str,
        *,
        ok: bool,
        stable: int = 0,
        situational: int = 0,
        elapsed: float = 0.0,
        message: str = "",
        message_count: int = 0,
        chars: int = 0,
    ) -> None:
        prev = self.learn_state.get(session_id, {})
        self.learn_state[session_id] = {
            "last_learn_at": time.time(),
            "last_ok": ok,
            "last_stable": stable,
            "last_situational": situational,
            "last_elapsed": round(elapsed, 2),
            "last_message": message[:300],
            "last_message_count": message_count,
            "last_chars": chars,
            "total_learns": int(prev.get("total_learns", 0)) + (1 if ok else 0),
            "fail_count": int(prev.get("fail_count", 0)) + (0 if ok else 1),
        }

        # 全局累计：只用于面板展示「这套机制一共花了多少」，不参与任何逻辑
        m = self.metrics
        m["learn_calls"] = int(m.get("learn_calls", 0)) + 1
        m["learn_ok"] = int(m.get("learn_ok", 0)) + (1 if ok else 0)
        m["learn_fail"] = int(m.get("learn_fail", 0)) + (0 if ok else 1)
        m["messages_analyzed"] = int(m.get("messages_analyzed", 0)) + message_count
        m["chars_analyzed"] = int(m.get("chars_analyzed", 0)) + chars
        m["elapsed_total"] = round(float(m.get("elapsed_total", 0.0)) + elapsed, 1)
        self._mark("learn_state", "metrics")

    # ── 备份导入 ────────────────────────────────────────────────────────

    def import_backup(
        self,
        universal: dict,
        chat_history: dict | None,
        mode: str = "merge",
        limits: tuple[int, int] | None = None,
    ) -> dict:
        """从旧版备份导入。mode=merge 合并同名特征，mode=replace 直接覆盖。"""
        imported_sessions = 0
        imported_traits = 0
        for session_id, traits in (universal or {}).items():
            if not isinstance(traits, list):
                continue
            clean: list[Trait] = []
            for t in traits:
                if not isinstance(t, dict):
                    continue
                content = str(t.get("content") or "").strip()
                if not content:
                    continue
                triggers = t.get("triggers")
                clean.append(
                    {
                        "content": content,
                        "category": t.get("category", CATEGORY_STABLE),
                        "context": str(t.get("context") or ""),
                        "triggers": [str(x) for x in triggers] if isinstance(triggers, list) else [],
                        "proficiency": int(t.get("proficiency", 0) or 0),
                        "confirmed_rounds": int(t.get("confirmed_rounds", 0) or 0),
                        "last_seen": float(t.get("last_seen", time.time()) or time.time()),
                        "last_updated": float(t.get("last_updated", time.time()) or time.time()),
                    }
                )
            if not clean:
                continue
            if mode == "merge":
                existing = self.universal.get(session_id, [])
                by_content = {t["content"]: t for t in existing}
                for t in clean:
                    prev = by_content.get(t["content"])
                    if prev:
                        prev["proficiency"] = max(
                            int(prev.get("proficiency", 0)), t["proficiency"]
                        )
                        prev["confirmed_rounds"] = max(
                            int(prev.get("confirmed_rounds", 0)), t["confirmed_rounds"]
                        )
                    else:
                        existing.append(t)
                        by_content[t["content"]] = t
                self.universal[session_id] = existing
            else:
                self.universal[session_id] = clean
            imported_sessions += 1
            imported_traits += len(clean)

        if chat_history:
            for session_id, msgs in chat_history.items():
                if isinstance(msgs, list) and msgs:
                    self.chat_history.setdefault(session_id, []).extend(
                        m for m in msgs if isinstance(m, dict)
                    )

        if limits:
            for sid in (universal or {}):
                self.trim_traits(sid, limits)

        self._version += 1
        self._mark("universal", "chat_history")
        return {"sessions": imported_sessions, "traits": imported_traits}

    # ── 统计 ────────────────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        total_traits = sum(len(v) for v in self.universal.values())
        stable = situational = 0
        for traits in self.universal.values():
            for t in traits:
                if t.get("category") == CATEGORY_SITUATIONAL:
                    situational += 1
                else:
                    stable += 1
        return {
            "sessions": len(self.universal),
            "traits": total_traits,
            "stable": stable,
            "situational": situational,
            "pending_messages": sum(len(v) for v in self.chat_history.values()),
            "metrics": {
                "learn_calls": int(self.metrics.get("learn_calls", 0)),
                "learn_ok": int(self.metrics.get("learn_ok", 0)),
                "learn_fail": int(self.metrics.get("learn_fail", 0)),
                "messages_analyzed": int(self.metrics.get("messages_analyzed", 0)),
                "chars_analyzed": int(self.metrics.get("chars_analyzed", 0)),
                "elapsed_total": float(self.metrics.get("elapsed_total", 0.0)),
            },
        }

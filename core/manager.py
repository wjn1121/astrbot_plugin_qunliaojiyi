"""调度：按学习链路分两种节奏。

- ``replace``（1.3.x 旧行为）：每 N 小时把所有攒够消息的会话整批重写一遍。
- ``accumulate``（1.4.0）：每分钟检查一次，谁攒够了增量就单独学谁，只处理新消息。
  这样才可能在多轮之间累积权重，而不是每次都从头重写。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from astrbot.api import logger

from .injector import Injector
from .learner import Learner
from .settings import Settings
from .storage import Storage


class Manager:
    def __init__(self, plugin, storage: Storage, settings: Settings):
        self.plugin = plugin
        self.storage = storage
        self.settings = settings
        self.learner = Learner(plugin, storage)
        self.injector = Injector(plugin, storage)

        self._task: asyncio.Task | None = None
        self._running = False
        self.next_run_at: float = 0.0
        self.last_run_at: float = 0.0
        self._first_allowed_at: float = 0.0
        self.last_run_result: dict[str, Any] = {}

    # ── 生命周期 ────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        delay = int(self.settings.get("startup_learn_delay_seconds"))
        interval = int(self.settings.get("analysis_interval_seconds"))
        self._first_allowed_at = time.time() + max(0, delay)
        self.next_run_at = self._first_allowed_at
        self._task = asyncio.create_task(self._loop())
        logger.info(
            f"[群聊记忆] 调度启动：启动后 {delay}s 允许学习，模式 "
            f"{self._learn_mode()}，兜底间隔 {interval}s。"
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self.storage.flush()

    # ── 主循环 ──────────────────────────────────────────────────────────

    def _learn_mode(self) -> str:
        mode = str(self.settings.get("learn_mode") or "accumulate").strip().lower()
        return "replace" if mode == "replace" else "accumulate"

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 调度不能因单轮异常退出
                logger.error(f"[群聊记忆] 调度循环异常: {e}")
            # 固定节拍：accumulate 靠这个节拍发现「谁攒够了增量」，
            # replace 由 next_run_at 自己控制什么时候真正学。
            await asyncio.sleep(30)

    async def _tick(self) -> None:
        if not self.settings.get("enabled"):
            return
        now = time.time()
        if now < self._first_allowed_at:
            return

        mode = self._learn_mode()
        if mode == "replace":
            if now < self.next_run_at:
                return
            candidates = self._replace_candidates()
            interval = max(60, int(self.settings.get("analysis_interval_seconds")))
            self.next_run_at = now + interval
        else:
            candidates = self._accumulate_candidates(now)
        if not candidates:
            if mode == "replace":
                self.last_run_result = {"sessions": 0, "ok": 0, "skipped": "没有攒够消息的会话"}
            return

        self.last_run_at = time.time()
        provider_id = str(self.settings.get("analysis_provider_id") or "").strip()
        results = []
        # 串行执行，避免一次性打满并发触发限流（旧版踩过 429 的坑）
        for session_id in candidates:
            if not self._running:
                break
            result = await self.learner.learn(session_id, provider_id)
            results.append({"session_id": session_id, **result})
            await asyncio.sleep(1.0)

        ok_count = sum(1 for r in results if r.get("ok"))
        self.last_run_result = {
            "mode": mode,
            "sessions": len(results),
            "ok": ok_count,
            "fail": len(results) - ok_count,
            "details": results,
        }
        if results:
            logger.info(f"[群聊记忆] 本轮学习完成（{mode}）：{ok_count}/{len(results)} 个会话成功")
        await self.storage.flush()

    def _replace_candidates(self) -> list[str]:
        """旧链路：凡是攒够 min_history 的会话，整批重学。"""
        min_history = int(self.settings.get("min_history_for_analysis"))
        out: list[str] = []
        for sid, msgs in self.storage.chat_history.items():
            if len(msgs) < min_history:
                continue
            # 1.3.0 这里漏了会话级开关，导致「面板关掉学习」在定时任务里不生效，
            # 1.4.0 补上：关掉的会话连替换式学习也不做。
            if not self.storage.learn_allowed(sid):
                continue
            out.append(sid)
        return out

    def _accumulate_candidates(self, now: float) -> list[str]:
        """新链路：攒够增量 + 过了最小间隔就单独学；久没学过的用兜底间隔捞一次。"""
        min_history = int(self.settings.get("min_history_for_analysis"))
        increment = max(min_history, int(self.settings.get("learn_increment_messages") or 30))
        min_interval = max(0, int(self.settings.get("learn_min_interval") or 1800))
        fallback = max(min_interval, int(self.settings.get("analysis_interval_seconds") or 21600))
        out: list[str] = []
        for sid, msgs in self.storage.chat_history.items():
            count = len(msgs)
            if count < min_history:
                continue
            if not self.storage.learn_allowed(sid):
                continue
            last = float((self.storage.learn_state.get(sid) or {}).get("last_learn_at", 0) or 0)
            idle = now - last if last > 0 else fallback
            if count >= increment and idle >= min_interval:
                out.append(sid)
            elif idle >= fallback:
                out.append(sid)
        return out

    # ── 手动 ────────────────────────────────────────────────────────────

    async def learn_now(self, session_id: str) -> dict:
        provider_id = str(self.settings.get("analysis_provider_id") or "").strip()
        return await self.learner.learn(session_id, provider_id, manual=True)

    def status(self) -> dict[str, Any]:
        now = time.time()
        mode = self._learn_mode()
        return {
            "running": self._running,
            "mode": mode,
            "next_run_at": self.next_run_at if mode == "replace" else None,
            "next_run_in": (
                max(0, int(self.next_run_at - now))
                if self._running and mode == "replace"
                else None
            ),
            "last_run_at": self.last_run_at or None,
            "last_run_result": self.last_run_result,
            "interval": int(self.settings.get("analysis_interval_seconds")),
            "increment_messages": int(self.settings.get("learn_increment_messages") or 30),
            "min_interval": int(self.settings.get("learn_min_interval") or 1800),
        }

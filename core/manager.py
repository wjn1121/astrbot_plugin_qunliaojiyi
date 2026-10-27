"""调度：低频批量学习，把「每轮都问模型」压成「每 N 小时一次整批提炼」。"""

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
        self.last_run_result: dict[str, Any] = {}

    # ── 生命周期 ────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        delay = int(self.settings.get("startup_learn_delay_seconds"))
        interval = int(self.settings.get("analysis_interval_seconds"))
        self.next_run_at = time.time() + max(0, delay)
        self._task = asyncio.create_task(self._loop())
        logger.info(
            f"[群聊记忆] 调度启动：启动后 {delay}s 首轮，之后每 {interval}s 检查一次。"
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self.storage.flush()

    # ── 主循环 ──────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 调度不能因单轮异常退出
                logger.error(f"[群聊记忆] 调度循环异常: {e}")
            # 每轮按当前（可能被面板热调过的）间隔重排下次时间
            interval = max(60, int(self.settings.get("analysis_interval_seconds")))
            self.next_run_at = time.time() + interval
            await asyncio.sleep(min(60, max(1, self.next_run_at - time.time())))

    async def _tick(self) -> None:
        if time.time() < self.next_run_at:
            return
        if not self.settings.get("enabled"):
            return

        self.last_run_at = time.time()
        min_history = int(self.settings.get("min_history_for_analysis"))
        candidates = {
            sid: len(msgs)
            for sid, msgs in self.storage.chat_history.items()
            if len(msgs) >= min_history
        }
        if not candidates:
            self.last_run_result = {"sessions": 0, "ok": 0, "skipped": "没有攒够消息的会话"}
            return

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
            "sessions": len(results),
            "ok": ok_count,
            "fail": len(results) - ok_count,
            "details": results,
        }
        if results:
            logger.info(f"[群聊记忆] 本轮学习完成：{ok_count}/{len(results)} 个会话成功")
        await self.storage.flush()

    # ── 手动 ────────────────────────────────────────────────────────────

    async def learn_now(self, session_id: str) -> dict:
        provider_id = str(self.settings.get("analysis_provider_id") or "").strip()
        return await self.learner.learn(session_id, provider_id, manual=True)

    def status(self) -> dict[str, Any]:
        now = time.time()
        return {
            "running": self._running,
            "next_run_at": self.next_run_at,
            "next_run_in": max(0, int(self.next_run_at - now)) if self._running else None,
            "last_run_at": self.last_run_at or None,
            "last_run_result": self.last_run_result,
            "interval": int(self.settings.get("analysis_interval_seconds")),
        }

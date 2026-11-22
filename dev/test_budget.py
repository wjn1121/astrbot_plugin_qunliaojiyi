"""每日预算闸的断言。用法：python dev/test_budget.py

只测判定逻辑，不碰真实数据：用量用一个假对象顶替，
数据目录指到临时目录，跑完就删。
"""

from __future__ import annotations

import importlib.util
import logging
import shutil
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str):
    """按文件路径加载 core 下的模块，绕开包导入（测试不跑在插件里）。"""
    spec = importlib.util.spec_from_file_location(name, ROOT / "core" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# core.budget 里有 `from .usage import today_key`，先给个能解析的包壳
pkg = types.ModuleType("pluginshim")
pkg.__path__ = [str(ROOT / "core")]
sys.modules["pluginshim"] = pkg

stub_api = types.ModuleType("astrbot")
stub_api_api = types.ModuleType("astrbot.api")
stub_api_api.logger = logging.getLogger("test")
stub_api.api = stub_api_api
sys.modules.setdefault("astrbot", stub_api)
sys.modules.setdefault("astrbot.api", stub_api_api)

usage = _load("usage")
budget_mod = _load("budget")
Budget = budget_mod.Budget

ok = True


def chk(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name, extra)
    ok = ok and bool(cond)


class FakeSettings:
    def __init__(self, cfg):
        self.cfg = cfg

    def get(self, key):
        return self.cfg.get(key)


class FakeUsage:
    def __init__(self, cost):
        self.cost = cost

    def today_cost(self):
        return self.cost


class FakePlugin:
    def __init__(self, data_dir, cost, cfg):
        self.data_dir = data_dir
        self.settings = FakeSettings(cfg)
        self.usage = FakeUsage(cost)


tmp = Path(tempfile.mkdtemp(prefix="budget_test_"))
try:
    on = {"daily_budget_enabled": True, "daily_budget_yuan": 2.0}

    # ── 默认 ──
    p = FakePlugin(tmp, 0.5, on)
    b = Budget(p)
    chk("没到上限不拦", b.exceeded() is False)
    snap = b.snapshot()
    chk("快照带已花与上限", snap["spent"] == 0.5 and snap["limit"] == 2.0, str(snap))
    chk("没超时 over_limit 为假", snap["over_limit"] is False)

    # ── 超支 ──
    p2 = FakePlugin(tmp, 2.6, on)
    b2 = Budget(p2)
    chk("到上限就拦", b2.exceeded() is True)
    chk("快照标出超支", b2.snapshot()["over_limit"] is True)
    chk("超支时 exceeded 也是真", b2.snapshot()["exceeded"] is True)

    # ── 手动放开 ──
    day = b2.release_today()
    chk("放开后拿到今天的日期", day == usage.today_key(), day)
    chk("放开后不拦", b2.exceeded() is False)
    snap2 = b2.snapshot()
    chk("快照标出已放开", snap2["bypassed"] is True)
    chk("已放开但 over_limit 仍为真（钱确实超了）", snap2["over_limit"] is True)
    chk("已放开时 exceeded 为假（闸门不拦）", snap2["exceeded"] is False)

    # ── 落盘：重开还在 ──
    b3 = Budget(FakePlugin(tmp, 2.6, on))
    chk("重启后放开状态还在", b3.bypassed_today() is True)
    chk("重启后仍然不拦", b3.exceeded() is False)

    # ── 跨天自动失效 ──
    b3._bypass_day = "1999-01-01"
    chk("不是今天的放开不算数", b3.bypassed_today() is False)
    chk("跨天后重新开始拦", b3.exceeded() is True)

    # ── 恢复限制 ──
    b3.release_today()
    b3.relock()
    chk("恢复限制后立刻拦", b3.exceeded() is True)
    chk("恢复后文件里也没了", b3.bypassed_today() is False)

    # ── 开关关着时永远不拦 ──
    off = {"daily_budget_enabled": False, "daily_budget_yuan": 2.0}
    b4 = Budget(FakePlugin(tmp, 99.0, off))
    chk("开关关着不拦", b4.exceeded() is False)
    chk("开关关着 over_limit 也是假", b4.snapshot()["over_limit"] is False)

    # ── 金额为 0 等于关掉 ──
    zero = {"daily_budget_enabled": True, "daily_budget_yuan": 0}
    b5 = Budget(FakePlugin(tmp, 99.0, zero))
    chk("上限填 0 等于关掉", b5.exceeded() is False)

    # ── 读不到用量时不把功能锁死 ──
    class Broken:
        def today_cost(self):
            raise RuntimeError("坏了")

    p6 = FakePlugin(tmp, 0, on)
    p6.usage = Broken()
    b6 = Budget(p6)
    chk("用量读不到时按 0 处理、不拦", b6.exceeded() is False)

finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("全部通过" if ok else "有失败项")
sys.exit(0 if ok else 1)

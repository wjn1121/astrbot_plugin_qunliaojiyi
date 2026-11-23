"""消耗分来源的断言：reply / active / bot 各记各的，老日文件照样能读。

用法：python dev/test_usage_source.py
不需要装 AstrBot——这里把 astrbot.api.logger 打成一个空 logger 顶上。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import tempfile
import types
from pathlib import Path

# ── 顶掉 astrbot 依赖（usage.py 只用到 logger）──
_pkg = types.ModuleType("astrbot")
_api = types.ModuleType("astrbot.api")
_api.logger = logging.getLogger("test")
_pkg.api = _api
sys.modules.setdefault("astrbot", _pkg)
sys.modules.setdefault("astrbot.api", _api)

ROOT = Path(__file__).resolve().parent.parent
# 直接按文件加载 core/usage.py：走 ``import core.usage`` 会把 core/__init__.py
# 一起拉进来，那个文件要整个 AstrBot 环境，而这里只想测这一个模块
_spec = importlib.util.spec_from_file_location("qunliao_usage", ROOT / "core" / "usage.py")
usage_mod = importlib.util.module_from_spec(_spec)
sys.modules["qunliao_usage"] = usage_mod
_spec.loader.exec_module(usage_mod)  # type: ignore[union-attr]

ALL_SOURCES = usage_mod.ALL_SOURCES
UsageTracker = usage_mod.UsageTracker
_empty_doc = usage_mod._empty_doc

ok = True


def chk(name: str, cond: bool, extra: str = "") -> None:
    global ok
    print(("PASS " if cond else "FAIL ") + name, extra)
    ok = ok and cond


# 关掉峰谷计价，金额只跟牌价有关，断言才稳定
settings = {"token_price_peak_enabled": False}
tmp = Path(tempfile.mkdtemp(prefix="qunliao_usage_"))
tracker = UsageTracker(tmp, settings)

# 一次「被叫回复」：1000 输入（600 命中缓存）+ 100 输出
usage = {"prompt_tokens": 1000, "prompt_cache_hit_tokens": 600, "completion_tokens": 100}
tracker.record("reply", usage, "bot1:GroupMessage:1")
tracker.record("active", usage, "bot1:GroupMessage:1")
tracker.record("learn", usage, "bot1:GroupMessage:1")
# 认不出来的来源要落进 bot，不能在面板上冒出野键
tracker.record("something_new", usage, "")
# usage 缺失也要计一次调用数
tracker.record("reply", None, "")

data = tracker.summary()
by_source = data.get("by_source") or {}

chk("所有来源都在默认文档里", all(name in _empty_doc("x")["by_source"] for name in ALL_SOURCES))
chk("reply 计了 2 次（含无 usage 那次）", by_source.get("reply", {}).get("calls") == 2)
chk("active 单独计 1 次", by_source.get("active", {}).get("calls") == 1)
chk("learn 单独计 1 次", by_source.get("learn", {}).get("calls") == 1)
chk("陌生来源归入 bot", by_source.get("bot", {}).get("calls") == 1)

reply_tokens = by_source.get("reply", {}).get("tokens") or {}
chk("未命中输入按总量倒推", reply_tokens.get("input_other") == 400, str(reply_tokens))
chk("缓存命中单独记", reply_tokens.get("input_cached") == 600, str(reply_tokens))
chk("输出单独记", reply_tokens.get("output") == 100, str(reply_tokens))
chk("金额算得出来", float(by_source.get("reply", {}).get("cost") or 0) > 0)
chk("总调用数对得上", data["today"]["calls"] == 5, str(data["today"]["calls"]))

# 各来源金额加起来必须等于顶部总额。以前这里是拿当天 token
# 乘「当前时刻倍率」现算的，高峰时段那几笔按 2 倍进总账，
# 面板却按平价重算，用户就会看到「0.49 + 0.56，顶部却是 1.80」
parts = sum(float(b.get("cost") or 0) for b in by_source.values())
total = float(data["today"]["cost"])
chk("分来源金额之和 == 总额", abs(parts - total) < 1e-6, f"{parts:.6f} vs {total:.6f}")

# 高峰时段混着记：一半按 2 倍、一半平价，仍然要对得上。
# 直接伪造一份日文件，模拟「上午高峰 + 下午空闲」的真实账
peak_settings = {"token_price_peak_enabled": True, "token_price_peak_multiplier": 2.0}
old_doc = {
    "date": usage_mod.today_key(),
    "calls": 2,
    "cost": 1.0,
    "tokens": {"input_other": 100000, "input_cached": 0, "output": 0},
    "by_source": {
        # 老文件：有 token 没 cost，得按权重把 1.0 分摊下去
        "learn": {"calls": 1, "input_other": 70000, "input_cached": 0, "output": 0},
        "bot": {"calls": 1, "input_other": 30000, "input_cached": 0, "output": 0},
    },
}
tmp2 = Path(tempfile.mkdtemp(prefix="qunliao_usage2_"))
(tmp2 / "token").mkdir(parents=True, exist_ok=True)
(tmp2 / "token" / f"{usage_mod.today_key()}.json").write_text(
    __import__("json").dumps(old_doc), encoding="utf-8"
)
t2 = UsageTracker(tmp2, peak_settings)
d2 = t2.summary()
b2 = d2.get("by_source") or {}
parts2 = sum(float(b.get("cost") or 0) for b in b2.values())
chk("老账也没有差额", abs(parts2 - float(d2["today"]["cost"])) < 1e-6,
    f"{parts2:.6f} vs {d2['today']['cost']}")
chk("老账按 token 权重分摊", abs(float(b2["learn"]["cost"]) - 0.7) < 1e-3,
    str(b2["learn"]["cost"]))

# 老日文件里只有 learn/bot/search，读进来要能补齐新键而不是丢掉当天数据
old = {
    "date": "2026-01-01",
    "calls": 2,
    "by_source": {
        "bot": {"calls": 1, "input_other": 10, "input_cached": 0, "output": 5},
        "search": {"calls": 1},
    },
}
doc = tracker._normalize(old, "2026-01-01")  # noqa: SLF001 - 断言专用
chk("老文件保留原有来源", doc["by_source"]["bot"]["calls"] == 1)
chk("老文件补出新来源空桶", doc["by_source"].get("reply", {}).get("calls") == 0)
chk("搜索桶不带 token 字段", "input_other" not in doc["by_source"]["search"])

print("\n" + ("全部通过" if ok else "有失败项"))
sys.exit(0 if ok else 1)

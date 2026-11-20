"""群里 /消耗 的输出断言。

用法：python dev/test_cost_summary.py
只测文本组装，不碰 AstrBot —— 假 usage 对象把三个方法顶掉就行。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import types
from pathlib import Path

# admin.py 只用到 logger；FunctionTool 那边有自己的 try/except 兜底
_pkg = types.ModuleType("astrbot")
_api = types.ModuleType("astrbot.api")
_api.logger = logging.getLogger("test")
_pkg.api = _api
sys.modules.setdefault("astrbot", _pkg)
sys.modules.setdefault("astrbot.api", _api)

ROOT = Path(__file__).resolve().parent.parent
# admin.py 里有 ``from .settings import ...``，得给它一个包身份
pkg = types.ModuleType("qunliao")
pkg.__path__ = [str(ROOT / "core")]
sys.modules["qunliao"] = pkg
_spec = importlib.util.spec_from_file_location("qunliao.admin", ROOT / "core" / "admin.py")
admin = importlib.util.module_from_spec(_spec)
sys.modules["qunliao.admin"] = admin
_spec.loader.exec_module(admin)  # type: ignore[union-attr]

ok = True


def chk(name: str, cond: bool, extra: str = "") -> None:
    global ok
    print(("PASS " if cond else "FAIL ") + name, extra)
    ok = ok and cond


class FakeUsage:
    def __init__(self, today, by_source, session_cost=0.0):
        self._today = today
        self._by_source = by_source
        self._session_cost = session_cost

    def today(self):
        return self._today

    def summary(self):
        return {"by_source": self._by_source}

    def cost_of(self, _day):
        return self._session_cost


class FakePlugin:
    def __init__(self, usage):
        self.usage = usage


# ── 正常一天：回复里有被叫和主动，后台有学习 ──
usage = FakeUsage(
    {
        "cost": 0.269,
        "calls": 18,
        "search_calls": 2,
        "by_session": {"bot1:GroupMessage:123": {"calls": 9}},
    },
    {
        "reply": {"calls": 12, "cost": 0.216},
        "active": {"calls": 3, "cost": 0.05},
        "learn": {"calls": 3, "cost": 0.008},
        "search": {"calls": 2, "cost": 0.0},
    },
    session_cost=0.09,
)
text = admin.cost_summary(FakePlugin(usage), "bot1:GroupMessage:123")

chk("头部给总额与调用数", text.startswith("今日消耗 ¥0.27 · 18 次调用"), text.splitlines()[0])
chk("头部带上搜索次数", "2 次搜索" in text.splitlines()[0])
chk("回复块汇总 reply+active", "💬 回复 15 次 · ¥0.27" in text)
chk("后台块汇总", "🧠 后台 3 次 · ¥0.0080" in text)
chk("被叫带平均单价", "被叫 12 次 · ¥0.22 · 平均 ¥0.02/次" in text)
chk("主动次数在列", "主动 3 次 · ¥0.05" in text)
chk("学习次数在列", "学习 3 次 · ¥0.0080" in text)
chk("给了本群金额", "本群 ¥0.09" in text)
chk("搜索不单独占一行", "🌐" not in text and "搜索 2 次 ·" not in text)

# ── 只有回复的群：不该留一个空的后台块 ──
only_reply = admin.cost_summary(
    FakePlugin(
        FakeUsage(
            {"cost": 0.02, "calls": 1, "search_calls": 0, "by_session": {}},
            {"reply": {"calls": 1, "cost": 0.02}},
        )
    )
)
chk("没有后台就不渲染后台块", "🧠 后台" not in only_reply)
chk("只有一笔时不写平均", "平均" not in only_reply)

# ── 分来源上线之前的老数据：只有 bot 桶，仍要能看 ──
old = admin.cost_summary(
    FakePlugin(
        FakeUsage(
            {"cost": 0.5, "calls": 40, "search_calls": 0, "by_session": {}},
            {"bot": {"calls": 40, "cost": 0.5}},
        )
    )
)
chk("老数据归到「其他」", "其他 40 次 · ¥0.50" in old)

# ── 完全没有记录 ──
empty = admin.cost_summary(FakePlugin(FakeUsage({"cost": 0, "calls": 0}, {})))
chk("无记录时给一句说明", "分来源统计上线之前" in empty)

print("\n" + ("全部通过" if ok else "有失败项"))
sys.exit(0 if ok else 1)

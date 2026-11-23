"""事实记忆的断言：达上限后按遗忘曲线淘汰，新条目一定进得来。

用法：python dev/test_facts.py
只测 core/facts.py 里的 _forget_rank / _trim，不碰 AstrBot 运行时。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import time
import types
from pathlib import Path

_pkg = types.ModuleType("astrbot")
_api = types.ModuleType("astrbot.api")
_api.logger = logging.getLogger("test")
_pkg.api = _api
sys.modules.setdefault("astrbot", _pkg)
sys.modules.setdefault("astrbot.api", _api)

ROOT = Path(__file__).resolve().parent.parent
pkg = types.ModuleType("qunliao")
pkg.__path__ = [str(ROOT / "core")]
sys.modules["qunliao"] = pkg


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"qunliao.{name}", ROOT / "core" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"qunliao.{name}"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


facts = _load("facts")
FactStore = facts.FactStore

ok = True

DAY = 86400.0
NOW = time.time()


def chk(name: str, cond: bool, extra: str = "") -> None:
    global ok
    print(("PASS " if cond else "FAIL ") + name, extra)
    ok = ok and cond


def store():
    """只造出 _trim 需要的那点状态：遗忘曲线依赖 shallow_days()。"""
    st = FactStore.__new__(FactStore)
    st.shallow_days = lambda: 15.0  # noqa: SLF001 - 测桩
    return st


def fact(i: int, days_ago: float, *, tier: str = "shallow", importance: int = 3,
         pinned: bool = False, kind: str = "fact") -> dict:
    return {
        "id": f"f_{i:04d}",
        "text": f"第 {i} 条",
        "kind": kind,
        "importance": importance,
        "tier": tier,
        "pinned": pinned,
        "last_seen": NOW - days_ago * DAY,
        "hits": 0,
        "status": "active",
    }


alive = lambda items: [x for x in items if x["status"] != "rejected"]  # noqa: E731


# ── 基本：满额后新的进来，最旧的走 ──
st = store()
items = [fact(1, 14), fact(2, 10), fact(3, 6), fact(4, 0)]  # 第 4 条是刚记的
st._trim(items, 3)
kept = {x["id"] for x in alive(items)}
chk("超出 1 条就淘汰 1 条", len(alive(items)) == 3, str(len(alive(items))))
chk("最久没提的被淘汰", "f_0001" not in kept, str(sorted(kept)))
chk("刚记下来的还在", "f_0004" in kept)

# ── 遗忘曲线优先于重要性 ──
# 一条 30 天前的高重要性、一条 2 天前的低重要性：用户要的是先忘掉快忘了的
st = store()
items = [fact(1, 30, importance=5), fact(2, 2, importance=1), fact(3, 0)]
st._trim(items, 2)
kept = {x["id"] for x in alive(items)}
chk("先淘汰快忘了的，哪怕它更重要", "f_0001" not in kept, str(sorted(kept)))
chk("刚提过的低重要条目留着", "f_0002" in kept)

# ── 钉住 / 核心 / 深层不动 ──
st = store()
items = [
    fact(1, 30, pinned=True),
    fact(2, 29, tier="core"),
    fact(3, 28, tier="deep"),
    fact(4, 20),
    fact(5, 0),
]
st._trim(items, 3)
kept = {x["id"] for x in alive(items)}
chk("钉住的没被淘汰", "f_0001" in kept)
chk("核心的没被淘汰", "f_0002" in kept)
chk("深层的没被淘汰", "f_0003" in kept)

# ── 该淘汰几条就真的淘汰几条（老写法会在跳过时欠账）──
st = store()
items = [
    fact(1, 30, pinned=True),   # 最低分但跳过
    fact(2, 29, tier="core"),   # 次低，也跳过
    fact(3, 20),
    fact(4, 10),
    fact(5, 5),
    fact(6, 0),
]
st._trim(items, 4)  # 6 条要裁到 4 条，得有 2 条真的被淘汰
chk("跳过锁定的之后仍然裁够条数", len(alive(items)) == 4, str(len(alive(items))))
chk("被裁的是浅层里最旧的", "f_0003" not in {x["id"] for x in alive(items)})

# ── 边界 ──
st = store()
items = [fact(1, 1), fact(2, 1)]
st._trim(items, 0)
chk("上限 0 表示不裁", len(alive(items)) == 2, str(len(alive(items))))

st = store()
items = [fact(1, 1), fact(2, 1)]
st._trim(items, 5)
chk("没超上限时一条不动", len(alive(items)) == 2)

# ── 边界类（不衰减）不该被优先淘汰 ──
st = store()
items = [
    fact(1, 60, kind="boundary", importance=5),  # 很久没提，但边界不衰减
    fact(2, 14),
    fact(3, 0),
]
st._trim(items, 2)
kept = {x["id"] for x in alive(items)}
chk("边界类不被优先淘汰", "f_0001" in kept, str(sorted(kept)))
chk("同分下淘汰普通的", "f_0002" not in kept)

print("\n" + ("全部通过" if ok else "有失败项"))
sys.exit(0 if ok else 1)

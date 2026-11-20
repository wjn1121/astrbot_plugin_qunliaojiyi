"""投喂结算与回复提示的断言。

用法：python dev/test_feed_note.py
只测 core/feed.py：settle 的记账/去重/上限，以及回复里那句话的拼法。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import types
from pathlib import Path

_pkg = types.ModuleType("astrbot")
_api = types.ModuleType("astrbot.api")
_api.logger = logging.getLogger("test")
_pkg.api = _api
sys.modules.setdefault("astrbot", _pkg)
sys.modules.setdefault("astrbot.api", _api)

ROOT = Path(__file__).resolve().parent.parent
# feed.py 里有 ``from .bond import bond_key``，给它一个包身份
pkg = types.ModuleType("qunliao")
pkg.__path__ = [str(ROOT / "core")]
sys.modules["qunliao"] = pkg
_spec = importlib.util.spec_from_file_location("qunliao.feed", ROOT / "core" / "feed.py")
feed = importlib.util.module_from_spec(_spec)
sys.modules["qunliao.feed"] = feed
_spec.loader.exec_module(feed)  # type: ignore[union-attr]

ok = True


def chk(name: str, cond: bool, extra: str = "") -> None:
    global ok
    print(("PASS " if cond else "FAIL ") + name, extra)
    ok = ok and cond


chk("2 分写成 +2", feed.build_note(2.0) == "（投喂好感度 +2）", feed.build_note(2.0))
chk("5.5 分保留小数", feed.build_note(5.5) == "（投喂好感度 +5.5）")
chk("0 分不写提示", feed.build_note(0) == "")
chk("撞每日次数写「已到上限」", feed.build_note(0, "limit") == "（今日投喂已到上限）")
chk("被净增上限吃掉时不写", feed.build_note(0, "cap") == "")
chk("负数不写提示", feed.build_note(-1) == "")
chk("非法值不写提示", feed.build_note("abc") == "")


class FakeSettings:
    def __init__(self, **over):
        self.cfg = {
            "feed_enabled": True,
            "feed_gain_random": 2.0,
            "feed_gain_order": 5.0,
            "feed_daily_limit": 1,
        }
        self.cfg.update(over)

    def get(self, key):
        return self.cfg.get(key)


class FakeStorage:
    def __init__(self):
        self.bonds = {}

    def bond_of(self, key):
        return self.bonds.get(key, {})

    def set_bond(self, key, record):
        self.bonds[key] = record


class FakeBond:
    def __init__(self, given=None):
        self.given = given

    def add_bonus(self, _sid, points):
        return points if self.given is None else self.given


class FakeEvent:
    def __init__(self, payload, sender="123"):
        self.extra = {feed.FEED_EXTRA_KEY: payload} if payload else {}
        self.sender = sender

    def get_extra(self, key):
        return self.extra.get(key)

    def set_extra(self, key, value):
        self.extra[key] = value

    def get_sender_id(self):
        return self.sender

    def get_self_id(self):
        return "999"


class FakePlugin:
    def __init__(self, settings=None, bond=None):
        self.settings = settings or FakeSettings()
        self.storage = FakeStorage()
        self.bond = bond or FakeBond()


# ── 正常一次随机投喂 ──
plugin = FakePlugin()
event = FakeEvent({"dish": "红烧肉", "mode": "random"})
info = feed.settle(plugin, event)
chk("返回加点与菜名", info == {"gain": 2.0, "dish": "红烧肉", "order": False, "reason": ""}, str(info))
chk("记了一次投喂", plugin.storage.bonds.get("123", {}).get("fed", {}).get("total") == 1)
chk("记下了菜名", plugin.storage.bonds["123"]["fed"]["last_dish"] == "红烧肉")
chk("打了已结算标记", event.extra.get(feed.FEED_DONE_KEY) is True)

# 同一个 event 再结算一次：必须什么都不做，否则会重复加分
again = feed.settle(plugin, event)
chk("同一 event 不重复记账", again is None)
chk("次数没被加两次", plugin.storage.bonds["123"]["fed"]["total"] == 1)

# 同一人换个 event：每日次数已用完（默认 limit=1）
second = FakeEvent({"dish": "饺子", "mode": "order"})
full = feed.settle(plugin, second)
chk("超过每日次数不加分", (full or {}).get("gain") == 0)
chk("超限时说明原因是 limit", (full or {}).get("reason") == "limit")
chk("超限也打标记（避免重复算）", second.extra.get(feed.FEED_DONE_KEY) is True)

# 点菜走另一档加成
lucky = FakePlugin(settings=FakeSettings(feed_daily_limit=5))
order_event = FakeEvent({"dish": "KFC", "mode": "order"})
order_info = feed.settle(lucky, order_event)
chk("点菜用 order 加成", (order_info or {}).get("gain") == 5.0 and (order_info or {}).get("order") is True, str(order_info))

# 加点被单日净增上限吃掉：不记账，也不占今天的次数
blocked = FakePlugin(bond=FakeBond(given=0))
blocked_event = FakeEvent({"dish": "面条", "mode": "random"})
capped = feed.settle(blocked, blocked_event)
chk("被净增上限吃掉时不加分", (capped or {}).get("gain") == 0)
chk("吃满时原因是 cap", (capped or {}).get("reason") == "cap")
chk("加分为 0 时不写 fed", not blocked.storage.bonds)

# 关闭喂食 / 没有标记 / 加成设成 0
chk("关掉喂食就什么都不做", feed.settle(FakePlugin(settings=FakeSettings(feed_enabled=False)), FakeEvent({"dish": "x"})) is None)
chk("没有投喂标记直接返回", feed.settle(FakePlugin(), FakeEvent(None)) is None)
chk("加成设成 0 不记", feed.settle(FakePlugin(settings=FakeSettings(feed_gain_random=0)), FakeEvent({"dish": "x"})) is None)

print("\n" + ("全部通过" if ok else "有失败项"))
sys.exit(0 if ok else 1)

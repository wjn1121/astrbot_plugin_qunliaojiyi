"""主动回复的断言：额度按时间滴出、发图补记次数。

用法：python dev/test_active.py
只测 core/active.py 里的纯计算，不碰 AstrBot 运行时。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

_pkg = types.ModuleType("astrbot")
_api = types.ModuleType("astrbot.api")
_api.logger = logging.getLogger("test")
_pkg.api = _api
sys.modules.setdefault("astrbot", _pkg)
sys.modules.setdefault("astrbot.api", _api)

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("qunliao_active", ROOT / "core" / "active.py")
active_mod = importlib.util.module_from_spec(_spec)
sys.modules["qunliao_active"] = active_mod
_spec.loader.exec_module(active_mod)  # type: ignore[union-attr]

ActiveReply = active_mod.ActiveReply
BEIJING_TZ = active_mod.BEIJING_TZ

ok = True


def chk(name: str, cond: bool, extra: str = "") -> None:
    global ok
    print(("PASS " if cond else "FAIL ") + name, extra)
    ok = ok and cond


class FakeSettings:
    def __init__(self, data: dict):
        self._data = data

    def get(self, key: str):
        return self._data.get(key)


class FakeStorage:
    """只实现 _load_state / _save 用到的那两个方法。"""

    def __init__(self):
        self.saved: dict[str, dict] = {}

    def active_of(self, session_id: str) -> dict:
        return dict(self.saved.get(session_id) or {})

    def save_active(self, session_id: str, state: dict) -> None:
        self.saved[session_id] = dict(state)

    def session_override(self, session_id: str) -> dict:
        return {}


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 23, hour, minute, tzinfo=BEIJING_TZ)


def make(quiet: str = "", daily: int = 20):
    return ActiveReply(None, FakeStorage(), FakeSettings({"active_reply_quiet_hours": quiet}))


# ── 静默时段不计入分母 ──
ar = make("02:00-07:00")

chk("日限 0 等于不限", ar.paced_quota(0) == 0)
chk("早上 8 点只放出一部分", 0 < ar.paced_quota(20, at(8)) < 20, str(ar.paced_quota(20, at(8))))
chk("中午比早上多", ar.paced_quota(20, at(12)) > ar.paced_quota(20, at(8)))
chk("晚上 23 点给满", ar.paced_quota(20, at(23)) == 20)
chk("半夜 1 点只剩个位数", ar.paced_quota(20, at(1)) <= 5, str(ar.paced_quota(20, at(1))))

# 任何时刻都不超过日限，且随小时单调不减（静默时段内持平或回落都无所谓，
# 但过了静默就该只增不减）
vals = [ar.paced_quota(20, at(h)) for h in range(24)]
chk("任何时刻都不超过日限", max(vals) <= 20, str(max(vals)))
chk("过了静默时段单调不减", vals[7:] == sorted(vals[7:]), str(vals[7:]))
chk("至少放 1 次（不然永远开不了口）", min(vals) >= 1, str(min(vals)))

# 不配静默：分母是 24 小时
plain = make("")
chk("无静默时 12 点约放出六成", 13 <= plain.paced_quota(24, at(12)) <= 15,
    str(plain.paced_quota(24, at(12))))
chk("无静默时 23 点给满", plain.paced_quota(24, at(23)) == 24)

# 跨天静默（22:00-06:00）也要算对
night = make("22:00-06:00")
chk("跨夜静默：早上 7 点很少", night.paced_quota(20, at(7)) <= 5,
    str(night.paced_quota(20, at(7))))
chk("跨夜静默：下午 6 点已放出九成", night.paced_quota(20, at(18)) == 18,
    str(night.paced_quota(20, at(18))))
chk("跨夜静默：21 点收尾前给满", night.paced_quota(20, at(21)) == 20,
    str(night.paced_quota(20, at(21))))

# 关掉「全天平均」：回到 0 点一次性给满
flat = ActiveReply(None, None, FakeSettings({"active_reply_pace_enabled": False}))
chk("关掉全日平均就一次性给满", flat.paced_quota(20, at(8)) == 20)
chk("关掉后也不超过日限", flat.paced_quota(8, at(1)) == 8)

# ── 发图补记次数 ──
store = FakeStorage()
ar2 = ActiveReply(None, store, FakeSettings({}))
ar2.note_extra_reply("qq:Group:1", "10001")
state = store.active_of("qq:Group:1")
chk("补记后今日次数 +1", state.get("today_count") == 1, str(state.get("today_count")))
chk("补记也记到人头", (state.get("per_user") or {}).get("10001") == 1)

ar2.note_extra_reply("qq:Group:1", "10001")
state = store.active_of("qq:Group:1")
chk("再补记一次到 2", state.get("today_count") == 2, str(state.get("today_count")))
chk("同一个人合并计数", (state.get("per_user") or {}).get("10001") == 2)

# 换天要归零，不能把昨天的次数带过来
stale = {
    "day": "2026-11-01",
    "today_count": 99,
    "per_user": {"10001": 9},
}
store.saved["qq:Group:2"] = stale
ar2.note_extra_reply("qq:Group:2", "10002")
state = store.active_of("qq:Group:2")
chk("隔天补记从 1 开始", state.get("today_count") == 1, str(state.get("today_count")))
chk("隔天人头也重算", (state.get("per_user") or {}).get("10002") == 1)

print("\n" + ("全部通过" if ok else "有失败项"))
sys.exit(0 if ok else 1)

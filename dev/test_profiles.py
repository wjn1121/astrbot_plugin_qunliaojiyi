"""预设档位的断言：每个键都能热调、默认档等于真默认值。

用法：python dev/test_profiles.py
点了预设报 KeyError 是这套功能最容易出的错，这里提前挡住。
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
_spec = importlib.util.spec_from_file_location(
    "qunliao_settings", ROOT / "core" / "settings.py"
)
settings = importlib.util.module_from_spec(_spec)
sys.modules["qunliao_settings"] = settings
_spec.loader.exec_module(settings)  # type: ignore[union-attr]

ok = True


def chk(name: str, cond: bool, extra: str = "") -> None:
    global ok
    print(("PASS " if cond else "FAIL ") + name, extra)
    ok = ok and cond


GROUP_PROFILES = settings.GROUP_PROFILES
RUNTIME_KEYS = settings.RUNTIME_KEYS
DEFAULTS = settings.DEFAULTS

chk("四组都有预设", set(GROUP_PROFILES) == {"active", "learn", "style", "facts"})

for group, profiles in GROUP_PROFILES.items():
    chk(f"{group} 至少三档", len(profiles) >= 3, f"{len(profiles)} 档")
    ids = [p["id"] for p in profiles]
    chk(f"{group} 的 id 唯一", len(ids) == len(set(ids)))
    chk(f"{group} 每档都有 desc", all(p.get("desc") for p in profiles))
    # 预设里写了不可热调的键，点按钮就会 500
    bad = [
        key
        for p in profiles
        for key in p["values"]
        if key not in RUNTIME_KEYS
    ]
    chk(f"{group} 的键都能热调", not bad, ",".join(sorted(set(bad))))

# 「平衡 / 人格优先」这类默认档必须等于真实默认值，否则用户点一下就把默认改掉了
defaults_group = {"active": "balanced", "learn": "balanced", "style": "persona_first", "facts": "balanced"}
for group, pid in defaults_group.items():
    preset = next((p for p in GROUP_PROFILES[group] if p["id"] == pid), None)
    chk(f"{group} 有默认档 {pid}", preset is not None)
    if not preset:
        continue
    diff = {
        key: (value, DEFAULTS.get(key))
        for key, value in preset["values"].items()
        if DEFAULTS.get(key) != value
    }
    chk(f"{group} 默认档与 DEFAULTS 一致", not diff, str(diff))

# 档位之间必须真的不同，否则等于给了几个没用的按钮
for group, profiles in GROUP_PROFILES.items():
    signatures = {tuple(sorted(p["values"].items(), key=lambda kv: kv[0])) for p in profiles}
    chk(f"{group} 各档的取值不重复", len(signatures) == len(profiles))

print("\n" + ("全部通过" if ok else "有失败项"))
sys.exit(0 if ok else 1)

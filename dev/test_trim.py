"""会话历史裁剪的断言：只留最近 N 条，system 保留，不从 tool 开头。

用法：python dev/test_trim.py
只测 utils.trim_contexts 这个纯函数。
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
_spec = importlib.util.spec_from_file_location("qunliao_utils", ROOT / "utils.py")
utils = importlib.util.module_from_spec(_spec)
sys.modules["qunliao_utils"] = utils
_spec.loader.exec_module(utils)  # type: ignore[union-attr]

trim = utils.trim_contexts
is_peak = utils.is_peak_time
from datetime import datetime  # noqa: E402

TZ = utils.BEIJING_TZ

ok = True


def chk(name: str, cond: bool, extra: str = "") -> None:
    global ok
    print(("PASS " if cond else "FAIL ") + name, extra)
    ok = ok and cond


def msg(role: str, text: str = "x") -> dict:
    return {"role": role, "content": text}


def convo(turns: int) -> list[dict]:
    out = [msg("system", "你是群友")]
    for i in range(turns):
        out.append(msg("user", f"问 {i}"))
        out.append(msg("assistant", f"答 {i}"))
    return out


# ── 基本裁剪 ──
long_ctx = convo(40)  # 81 条
got = trim(long_ctx, 20)
chk("裁到 20 条 + system", len(got) == 21, str(len(got)))
chk("system 还在", got[0]["role"] == "system")
chk("留的是最新的", got[-1]["content"] == "答 39", str(got[-1]))

chk("没超上限不裁", trim(convo(5), 20) is None)
chk("上限 0 等于不裁", trim(long_ctx, 0) is None)
chk("不是列表返回 None", trim("nope", 10) is None)

# ── 不能以 assistant / tool 开头 ──
odd = [msg("system")] + [msg("user", "a"), msg("assistant", "b")] * 5 + [msg("assistant", "尾巴")]
res = trim(odd, 3)  # 最后 3 条是 user / assistant / assistant，得往前退到 user
chk("裁完不以 assistant 开头", res[0]["role"] == "system" and res[1]["role"] == "user",
    str([m["role"] for m in res]))

tool_ctx = [msg("system"), msg("user", "查一下")]
tool_ctx += [msg("assistant", "调用工具"), msg("tool", "结果"), msg("assistant", "答案")]
tool_ctx += [msg("user", "再问"), msg("tool", "结果2"), msg("assistant", "答案2")]
res2 = trim(tool_ctx, 3)  # 最后 3 条是 user / tool / assistant，开头合法
chk("裁完首条不是 tool", res2[1]["role"] == "user", str([m["role"] for m in res2]))
# 从 tool / assistant 开始的那一版：最后 2 条是 tool + assistant，得一路退到 user
res3 = trim(tool_ctx, 2)
chk("只有 tool/assistant 时退到合法开头", res3 is None or res3[1]["role"] == "user",
    "None" if res3 is None else str([m["role"] for m in res3]))

# ── 全是 tool / assistant 时宁可原样不裁 ──
only_tools = [msg("tool", "a"), msg("tool", "b")]
chk("裁不出合法开头就返回 None", trim(only_tools, 1) is None)

# ── 峰谷判断 ──
mon_10 = datetime(2026, 11, 23, 10, 0, tzinfo=TZ)   # 周一 10 点：高峰
mon_13 = datetime(2026, 11, 23, 13, 0, tzinfo=TZ)   # 周一 13 点：空闲
mon_15 = datetime(2026, 11, 23, 15, 0, tzinfo=TZ)   # 周一 15 点：高峰
sat_10 = datetime(2026, 11, 28, 10, 0, tzinfo=TZ)   # 周六 10 点：空闲
chk("工作日 10 点是高峰", is_peak(mon_10) is True)
chk("工作日 13 点不是高峰", is_peak(mon_13) is False)
chk("工作日 15 点是高峰", is_peak(mon_15) is True)
chk("周末全天空闲价", is_peak(sat_10) is False)

# ── 学习时间模式 ──
defer = utils.defer_learning
chk("默认模式低谷学：高峰推迟", defer("off_peak", True, mon_10) is True)
chk("默认模式低谷学：空闲正常学", defer("off_peak", True, mon_13) is False)
chk("随时学：高峰也照学", defer("anytime", True, mon_10) is False)
chk("峰谷计价关掉就不推迟", defer("off_peak", False, mon_10) is False)
chk("周末不推迟", defer("off_peak", True, sat_10) is False)
chk("空模式按默认低谷处理", defer("", True, mon_10) is True)

print("\n" + ("全部通过" if ok else "有失败项"))
sys.exit(0 if ok else 1)

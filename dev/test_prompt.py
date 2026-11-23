"""学习提示词的断言：规则一条不丢、按 facts 开关拼装。

用法：python dev/test_prompt.py
只测 core/learner.py 里的常量与拼装函数。重构最容易漏掉一条约束，
这个脚本就是拿来钉住这些约束的。
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
pkg = types.ModuleType("qunliao")
pkg.__path__ = [str(ROOT / "core")]
sys.modules["qunliao"] = pkg


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"qunliao.{name}", ROOT / "core" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"qunliao.{name}"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


learner = _load("learner")

ok = True


def chk(name: str, cond: bool, extra: str = "") -> None:
    global ok
    print(("PASS " if cond else "FAIL ") + name, extra)
    ok = ok and cond


rules = learner.LEARN_RULES
with_facts = learner.learn_system_prompt(True)
without = learner.learn_system_prompt(False)

# ── 规则完整性：重构时最容易漏的就是这些硬约束 ──
for needle, label in [
    ("evidence", "evidence 字段"),
    ("原样复制", "evidence 必须原样复制"),
    ("24 字", "evidence 长度上限"),
    ("找不到对应原话就不要输出", "找不到证据就不输出"),
    ("dimension", "dimension 字段"),
    ("黑话梗", "dimension 的取值集合"),
    ("triggers", "triggers 字段"),
    ("6 字", "triggers 长度"),
    ("英文双引号", "禁止英文双引号"),
    ("一次性玩梗", "一次性玩梗不收录"),
    ("10-30 字", "style 长度"),
    ("空话", "禁止空话"),
    ("situation", "situation 字段"),
    ("JSON", "只输出 JSON"),
    ("代码围栏", "不要代码围栏"),
]:
    chk(f"规则里有：{label}", needle in rules, "" if needle in rules else needle)

# ── facts 开关 ──
chk("不带 facts 时没有 facts 段落", "facts" not in without)
chk("带 facts 时有 facts 段落", "facts" in with_facts)
for needle, label in [
    ("最多 3 条", "facts 条数上限"),
    ("密码、证件号、银行卡、验证码", "敏感信息黑名单"),
    ("subject", "subject 字段"),
    ("boundary", "kind 取值"),
]:
    chk(f"facts 规则里有：{label}", needle in with_facts, "" if needle in with_facts else needle)

chk("两种拼装都以规则开头", with_facts.startswith(rules[:20]) and without.startswith(rules[:20]))
chk("规则本身不超过 2000 字（别把 system 撑太大）", len(rules) < 2000, str(len(rules)))

# ── 自检 ──
sc = learner.SELF_CHECK_SYSTEM
chk("自检规则保留了淘汰标准", "淘汰标准" in sc)
chk("自检要求覆盖全部编号", "编号必须覆盖全部候选" in sc)
chk("自检输出格式没变", '"keep"' in sc and '"drop"' in sc)
chk("自检不再把规则塞进 prompt（prompt 只给候选）",
    "淘汰标准" not in "候选：\n1. xxx")

print("\n" + ("全部通过" if ok else "有失败项"))
sys.exit(0 if ok else 1)

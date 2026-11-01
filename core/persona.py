"""群人格档案：把学习库里「已生效」的特征整理成一份可读、可改的 markdown。

为什么单独存文件而不是只放面板里：人格是长期资产，用户会想直接看、想备份、
想粘到 AstrBot 的人设里。文件放在**插件自己的数据目录**里
（``AstrBot/data/plugin_data/astrbot_plugin_qunliaojiyi/persona/``）：市场规范要求
持久化数据都在 plugin_data 内，放外面会导致用户迁移、审计时漏掉这份数据。
1.4.0 及更早写到 data 顶层的 ``qunliao_persona/``，升级后仍能读到（只读不写），
下一次重建时内容会落到新目录。

文件分成两段，用 HTML 注释隔开：

    <!-- AUTO:BEGIN ... -->      自动生成，学习或审核后重写
    <!-- MANUAL:BEGIN ... -->    人工补充，程序永远不改

物理隔离而不是「合并后一起写」：只要程序只碰 AUTO 段，用户手写的内容就
不可能被下一轮学习冲掉，这比任何「保留用户修改」的启发式规则都可靠。
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .storage import CATEGORY_SITUATIONAL, STATUS_ACTIVE, trait_score
from ..utils import display_name

# 与 usage.py 一致：人格文件里写的时间戳一律北京时间
BEIJING_TZ = timezone(timedelta(hours=8))

# 自动段与人工段的边界标记。标记文本写死，改动会影响已有文件的解析，
# 所以换措辞要同时兼容旧标记。
AUTO_BEGIN = "<!-- AUTO:BEGIN 以下内容由插件自动生成，学习或审核后重写；改这里会被覆盖 -->"
AUTO_END = "<!-- AUTO:END -->"
MANUAL_BEGIN = "<!-- MANUAL:BEGIN 以下内容来自人工补充，插件永远不会修改这一段 -->"
MANUAL_END = "<!-- MANUAL:END -->"

MANUAL_PLACEHOLDER = (
    "（在这里补充你想让 bot 记住的人格设定，保存后会写进这个文件。"
    "学习只会重写上面的自动段，这段永远不会被动。）"
)

_MANUAL_RE = re.compile(
    r"<!--\s*MANUAL:BEGIN.*?-->(?P<body>.*?)<!--\s*MANUAL:END\s*-->", re.S
)
_AUTO_RE = re.compile(r"<!--\s*AUTO:BEGIN.*?-->(?P<body>.*?)<!--\s*AUTO:END\s*-->", re.S)

# 会话 ID 里的冒号在 Windows 上是非法文件名字符，统一换掉
_UNSAFE_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff_-]+")

# 自动段里维度标题的固定顺序。dimension 是学习链路给的自由文本，
# 认不出来的排在后面，不丢内容。
DIMENSION_ORDER = ("语气", "节奏", "互动", "话题", "黑话")


def resolve_persona_dir(plugin_data_dir: str | Path) -> Path:
    """人格文件放哪。

    **必须在插件自己的数据目录里**（``data/plugin_data/<插件名>/persona/``）：
    AstrBot 市场要求持久化数据都落在 plugin_data 内，否则用户迁移数据、
    审计存储时会漏掉。1.4.0 及更早写到了 data 顶层的 ``qunliao_persona/``，
    那份现在只读不写（见 :func:`resolve_legacy_persona_dir`），升级不会丢内容。

    任何异常都不冒出去，最坏返回一个可能写不进的路径，由写盘处记日志。
    """
    try:
        base = Path(plugin_data_dir)
    except (TypeError, ValueError):
        base = Path(".")

    target = base / "persona"
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".write_probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError:
        pass
    return target


def resolve_legacy_persona_dir(plugin_data_dir: str | Path) -> Path | None:
    """1.4.0 及更早的人格档案目录（``data/qunliao_persona/``）。只读。"""
    try:
        base = Path(plugin_data_dir)
    except (TypeError, ValueError):
        return None
    try:
        # 不判断层级的话，自定义数据目录会一路往上算，读到不该读的地方
        if base.parent.name != "plugin_data":
            return None
        legacy = base.parent.parent / "qunliao_persona"
        return legacy if legacy.is_dir() else None
    except OSError:
        return None


def file_stem(session_id: str) -> str:
    """会话 ID 转文件名。中文平台实例名保留，冒号等换成下划线。"""
    cleaned = _UNSAFE_RE.sub("_", str(session_id or "")).strip("_")
    return cleaned or "session"


def _fmt_time(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts), BEIJING_TZ).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError, TypeError):
        return "—"


def build_auto_body(traits: list[dict], *, now: float | None = None) -> str:
    """把已生效的特征渲染成 markdown 正文（不含边界标记与文件头）。

    只吃已经通过审核的特征：待审的、被否决的、已归档的都不进来 —— 人格档案
    是「这个群已经确认的样子」，不是「还在观察的候选池」。
    """
    at = now if now is not None else time.time()
    active = [
        t
        for t in traits
        if str(t.get("content") or "").strip()
        and t.get("status", STATUS_ACTIVE) == STATUS_ACTIVE
    ]
    if not active:
        return "还没有可用的特征。等学习库里有条目通过审核（或跨两轮被确认）后，这里会自动长出来。\n"

    stable = [t for t in active if t.get("category") != CATEGORY_SITUATIONAL]
    situational = [t for t in active if t.get("category") == CATEGORY_SITUATIONAL]
    stable.sort(key=lambda t: (1 if t.get("pinned") else 0, trait_score(t, at)), reverse=True)
    situational.sort(key=lambda t: (1 if t.get("pinned") else 0, trait_score(t, at)), reverse=True)

    lines: list[str] = []

    # 有 dimension 就按维度分小节，没有就一起放在「说话风格」里
    buckets: dict[str, list[dict]] = {}
    for trait in stable:
        key = str(trait.get("dimension") or "").strip() or "说话风格"
        buckets.setdefault(key, []).append(trait)
    ordered = [k for k in DIMENSION_ORDER if k in buckets]
    ordered += [k for k in buckets if k not in ordered]

    for name in ordered:
        lines.append(f"## {name}")
        lines.append("")
        for trait in buckets[name]:
            lines.append(f"- {str(trait.get('content') or '').strip()}")
        lines.append("")

    if situational:
        lines.append("## 场景反应")
        lines.append("")
        for trait in situational:
            context = str(trait.get("context") or "").strip()
            content = str(trait.get("content") or "").strip()
            if context:
                lines.append(f"- 当{context}时：{content}")
            else:
                lines.append(f"- {content}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_file(
    session_id: str,
    auto_body: str,
    manual_body: str,
    *,
    trait_count: int,
    stable_count: int,
    situational_count: int,
    updated_at: float,
    rel_path: str,
) -> str:
    """拼出完整的文件内容。头部信息也属于自动区，跟着一起更新。"""
    manual = manual_body.strip("\n") or MANUAL_PLACEHOLDER
    return (
        f"# {display_name(session_id)} · 人格档案\n"
        f"\n"
        f"> 本文件由「群聊记忆」插件维护，可以直接编辑，但只有最后一段属于你。\n"
        f"> 自动生成于 {_fmt_time(updated_at)}，取自学习库中已生效的 {trait_count} 条特征"
        f"（稳定 {stable_count} / 场景 {situational_count}）。\n"
        f"> 文件位置：`{rel_path}`\n"
        f"\n"
        f"{AUTO_BEGIN}\n"
        f"\n"
        f"{auto_body.strip()}\n"
        f"\n"
        f"{AUTO_END}\n"
        f"\n"
        f"{MANUAL_BEGIN}\n"
        f"\n"
        f"{manual}\n"
        f"\n"
        f"{MANUAL_END}\n"
    )


class PersonaStore:
    """人格档案的读写。只管文件，不碰学习与注入。"""

    def __init__(self, plugin_data_dir: str | Path):
        self.dir = resolve_persona_dir(plugin_data_dir)
        # 1.4.0 及更早写在 data 顶层，升级后先读旧位置，不动它
        self.legacy_dir = resolve_legacy_persona_dir(plugin_data_dir)

    # ── 路径 ────────────────────────────────────────────────────────────

    def path_of(self, session_id: str) -> Path:
        """写入目标（新目录）。读的时候要走 :meth:`source_path_of`。"""
        return self.dir / f"{file_stem(session_id)}.md"

    def source_path_of(self, session_id: str) -> Path | None:
        """实际该从哪读：新目录优先，没有再退到 1.4.0 的旧目录。

        只读不搬：旧文件留在原地，等这份人格下一次被重建（读旧 + 写新）时，
        内容自然就落到新目录了，不需要单独写迁移脚本。
        """
        target = self.path_of(session_id)
        try:
            if target.is_file():
                return target
            if self.legacy_dir is not None:
                legacy = self.legacy_dir / target.name
                if legacy.is_file():
                    return legacy
        except OSError:
            return None
        return None

    def exists(self, session_id: str) -> bool:
        return self.source_path_of(session_id) is not None

    def updated_at(self, session_id: str) -> float:
        path = self.source_path_of(session_id)
        if path is None:
            return 0.0
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    # ── 读 ──────────────────────────────────────────────────────────────

    def read(self, session_id: str) -> dict[str, Any]:
        """读一份人格档案，解析出自动段与人工段。

        文件被手改坏了（标记丢了、内容被清空）不抛异常：整体当人工段返回，
        至少不丢用户写的字。
        """
        target = self.path_of(session_id)
        path = self.source_path_of(session_id)
        base = {
            "session_id": session_id,
            "display_name": display_name(session_id),
            "path": str(target),
            "legacy": bool(path is not None and path != target),
            "exists": False,
            "auto": "",
            "manual": "",
            "full": "",
            "updated_at": 0.0,
        }
        if path is None:
            return base
        try:
            full = path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning(f"[群聊记忆] 读取人格档案失败 {session_id}: {e}")
            return base

        auto_match = _AUTO_RE.search(full)
        manual_match = _MANUAL_RE.search(full)
        if manual_match:
            manual = manual_match.group("body").strip("\n")
        else:
            # MANUAL 标记被改坏了（少了开头或结尾）。这时把 AUTO 段之后的内容
            # 整体当人工段 —— 宁可多留几个标记残片，也不能让用户手写的字在面板里
            # 凭空消失：面板显示为空的话，下一次「保存」就真的会把它覆盖掉。
            auto_end = full.rfind(AUTO_END)
            tail = full[auto_end + len(AUTO_END) :] if auto_end >= 0 else full
            manual = (
                tail.replace(MANUAL_END, "").replace(MANUAL_BEGIN, "").strip("\n")
            )
        if manual.strip() == MANUAL_PLACEHOLDER:
            manual = ""

        return {
            **base,
            "exists": True,
            "auto": auto_match.group("body").strip("\n") if auto_match else "",
            "manual": manual,
            "full": full,
            "updated_at": self.updated_at(session_id),
        }

    # ── 写 ──────────────────────────────────────────────────────────────

    def _write(self, session_id: str, text: str) -> bool:
        path = self.path_of(session_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # 先写临时文件再替换：写到一半崩了也不会留下半份人格档案
            temp = path.with_suffix(".md.tmp")
            temp.write_text(text, encoding="utf-8")
            temp.replace(path)
            return True
        except OSError as e:
            logger.error(f"[群聊记忆] 保存人格档案失败 {session_id}: {e}")
            return False

    def rebuild(
        self,
        session_id: str,
        traits: list[dict],
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """按当前特征重新生成自动段，保留人工段。

        ``force=False`` 时如果自动段没变化就跳过写盘 —— 每次学习后都会调一次，
        不比对的话会造成「文件修改时间一直在变、但内容一样」的假象，用户会以为
        人格在乱动。
        """
        current = self.read(session_id)
        manual = current.get("manual") or ""
        auto_body = build_auto_body(traits)

        if not force and current.get("exists") and current.get("auto", "").strip() == auto_body.strip():
            return {
                "session_id": session_id,
                "changed": False,
                "path": str(self.path_of(session_id)),
                "manual": manual,
            }

        active = [
            t
            for t in traits
            if str(t.get("content") or "").strip()
            and t.get("status", STATUS_ACTIVE) == STATUS_ACTIVE
        ]
        situational = sum(1 for t in active if t.get("category") == CATEGORY_SITUATIONAL)
        text = render_file(
            session_id,
            auto_body,
            manual,
            trait_count=len(active),
            stable_count=len(active) - situational,
            situational_count=situational,
            updated_at=time.time(),
            rel_path=f"{self.dir.name}/{self.path_of(session_id).name}",
        )
        ok = self._write(session_id, text)
        return {
            "session_id": session_id,
            "changed": ok,
            "path": str(self.path_of(session_id)),
            "manual": manual,
        }

    def save_manual(self, session_id: str, manual: str) -> dict[str, Any]:
        """只改人工段。

        实现上是「就地替换 MANUAL 标记之间的内容」，文件其余部分一字不动：
        这样保存人工补充不会顺手刷新头部时间戳和自动段，也就不会让人误以为
        学习重跑了一遍。
        """
        cleaned = (manual or "").strip()
        body = cleaned or MANUAL_PLACEHOLDER
        replacement = f"{MANUAL_BEGIN}\n\n{body}\n\n{MANUAL_END}"
        path = self.path_of(session_id)

        if path.is_file():
            try:
                full = path.read_text(encoding="utf-8")
            except OSError as e:
                logger.warning(f"[群聊记忆] 读取人格档案失败 {session_id}: {e}")
                full = ""
            if _MANUAL_RE.search(full):
                ok = self._write(session_id, _MANUAL_RE.sub(lambda _m: replacement, full, count=1))
                return {
                    "session_id": session_id,
                    "saved": ok,
                    "path": str(path),
                    "manual": cleaned,
                }

        # 文件还不存在（或标记被人删了）：新建一份，自动段留空，
        # 等下次学习或审核后由 rebuild 填上
        text = render_file(
            session_id,
            build_auto_body([]),
            cleaned,
            trait_count=0,
            stable_count=0,
            situational_count=0,
            updated_at=time.time(),
            rel_path=f"{self.dir.name}/{path.name}",
        )
        ok = self._write(session_id, text)
        return {
            "session_id": session_id,
            "saved": ok,
            "path": str(path),
            "manual": cleaned,
        }

    def delete(self, session_id: str) -> bool:
        try:
            self.path_of(session_id).unlink(missing_ok=True)
            return True
        except OSError as e:
            logger.warning(f"[群聊记忆] 删除人格档案失败 {session_id}: {e}")
            return False

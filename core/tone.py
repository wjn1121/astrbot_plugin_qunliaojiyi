"""输出层：让回复看起来像人打的，而不是模型吐的。

三件事，全是本地正则，**不产生任何 LLM 调用**：

1. **清理**（默认开）：删掉「作为一个 AI」「希望这对你有帮助」这类模型腔，
   把中文双破折号换成逗号。只做删除和标点替换，不改语义。
2. **压连续标点 / 去 markdown**（默认关）：群里发 ``！！！`` 和 ``**加粗**``
   很难看，但会改变表达力度，所以默认不碰。
3. **分段**（默认关）：把长回复拆成多个 ``Plain`` 组件。宿主开了「分段回复」
   时 AstrBot 会逐段发送并在段间加打字延迟（``respond/stage.py:283``）；
   宿主没开时整条一起发，视觉上等同原文。两种都安全。

**代码块、行内代码、URL 一律不碰**：先把它们换成占位符，处理完再还原。
含代码块或链接的回复不做分段——那种内容本来就不该切成好几条。
"""

from __future__ import annotations

import re
from typing import Any

# 占位符用 \x00 包裹序号，正常中文和代码里不会出现这两个字符
_PH_OPEN = "\x00"
_PH_CLOSE = "\x01"
_PH_RE = re.compile(r"\x00(\d+)\x01")

# 保护顺序要紧：先整块代码，再行内代码，最后 URL
_PROTECT_PATTERNS = (
    re.compile(r"```[\s\S]*?```"),
    re.compile(r"`[^`\n]+`"),
    re.compile(r"https?://[^\s，。；、）】》,]+"),
)

# 句末切分：保留标点。英文句点不切，避免切坏 1.5 / v1.0 / 域名
_SENT_RE = re.compile(r"[^。！？!?\n]*[。！？!?]+|[^。！？!?\n]+")

# 带换行的切分：删句子时要把换行当独立片段保留，否则一整段会挤成一行
_PIECE_RE = re.compile(r"[^。！？!?\n]*[。！？!?]+|[^。！？!?\n]+|\n")

# 开场自报身份。只在开头匹配，且删空就放弃
_AI_OPENERS = (
    re.compile(
        r"^\s*作为(一个|一名|一个所谓的)?\s*(AI|人工智能|人工智能助手|AI助手|"
        r"AI语言模型|大语言模型|语言模型)\s*[^，。！？\n]{0,8}[，,]\s*"
    ),
    re.compile(r"^\s*我(是|作为)(一个|一名)?\s*(AI|人工智能)\s*(助手|语言模型)?\s*[，,]\s*"),
)

# 结尾套话。逐句从尾部往回看，最多砍两句，且只在句子够短时砍
_TAIL_SENTENCES = (
    re.compile(
        r"^(希望|但愿)(这|以上|这些)?(对你|对您|能对你|能对您)?(有所|有)?"
        r"(帮助|帮到你|帮到您|用处)"
    ),
    re.compile(r"^(如果|如|若)(你|您)?(还)?(有|需要)(其他|其它|任何)?(问题|疑问|需要)"),
    re.compile(r"^(还有|请问还有|另外还有|此外)(什么|其他|其它)"),
    re.compile(r"^(随时|欢迎随时|可以随时)(告诉|问|找|联系)我"),
    re.compile(r"^(有什么|还有什么)(我)?(可以|能|需要)(帮|为)(你|您)"),
    re.compile(r"^(需要我|要不要我)(帮|为)(你|您)?(再|继续)"),
    re.compile(r"^祝(你|您)(今天|一天|接下来)?(愉快|开心|顺利|好运)"),
)

# 套话都很短。超过这个长度的句子不当套话砍，避免删掉真内容
_TAIL_MAX_CHARS = 30

_PUNCT_RULES = (
    (re.compile(r"([！!])\1+"), r"\1"),
    (re.compile(r"([？?])\1+"), r"\1"),
    (re.compile(r"([。])\1+"), r"\1"),
    (re.compile(r"([，,])\1+"), r"\1"),
    (re.compile(r"(、)\1+"), r"\1"),
    (re.compile(r"([~～])\1+"), r"\1"),
)

_MD_RULES = (
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"\1"),
    (re.compile(r"(?<!\*)\*(?!\*)([^*\n]+?)(?<!\*)\*(?!\*)"), r"\1"),
    (re.compile(r"__(.+?)__", re.S), r"\1"),
    (re.compile(r"^#{1,6}[ \t]+", re.M), ""),
    (re.compile(r"^[ \t]*>[ \t]?", re.M), ""),
)


# ── 保护与还原 ──────────────────────────────────────────────────────────


def _protect(text: str) -> tuple[str, list[str]]:
    """把代码块 / 行内代码 / URL 换成占位符，返回 (处理后的文本, 原文片段)。"""
    slots: list[str] = []

    def take(match: re.Match[str]) -> str:
        token = match.group(0)
        tail = ""
        # URL 尾部的感叹号、句号是句子标点，不属于地址。留在原位才能被后续规则处理
        if token.startswith(("http://", "https://")):
            trimmed = token.rstrip("！!～~。，,")
            tail = token[len(trimmed) :]
            token = trimmed
        slots.append(token)
        return f"{_PH_OPEN}{len(slots) - 1}{_PH_CLOSE}{tail}"

    for pattern in _PROTECT_PATTERNS:
        text = pattern.sub(take, text)
    return text, slots


def _restore(text: str, slots: list[str]) -> str:
    if not slots:
        return text

    def put(match: re.Match[str]) -> str:
        idx = int(match.group(1))
        return slots[idx] if 0 <= idx < len(slots) else match.group(0)

    return _PH_RE.sub(put, text)


# ── 清理 ────────────────────────────────────────────────────────────────


def _strip_openers(text: str) -> str:
    """去开场自报身份。删完为空就不删——宁可留着也不发空消息。"""
    for pattern in _AI_OPENERS:
        stripped = pattern.sub("", text, count=1)
        if stripped != text:
            if stripped.strip():
                text = stripped
            break
    return text


def _strip_tail(text: str) -> str:
    """从尾部往回砍套话，最多两句，且至少留一句。

    用 ``_PIECE_RE`` 而不是 ``_SENT_RE``：换行要作为独立片段保留，
    否则砍完一拼，整段就挤成一行了。
    """
    pieces = _PIECE_RE.findall(text)
    if not pieces:
        return text

    # 尾部纯空白先摘出来，最后原样接回去
    trail: list[str] = []
    while pieces and not pieces[-1].strip():
        trail.insert(0, pieces.pop())

    removed = 0
    while pieces and removed < 2:
        body = pieces[-1].strip().strip("。！？!?~～")
        if not body or len(body) > _TAIL_MAX_CHARS:
            break
        if not any(p.match(body) for p in _TAIL_SENTENCES):
            break
        if len(pieces) == 1:
            break  # 整条回复就这一句套话，留着
        pieces.pop()
        removed += 1
        # 删完露出的换行也一起删，否则会留下空行
        while pieces and not pieces[-1].strip():
            pieces.pop()

    return "".join(pieces + trail)


def _fix_dash(text: str) -> str:
    """中文双破折号换逗号。单个 — 不动（可能表示年份区间）。"""
    if "——" not in text:
        return text
    text = text.replace("——", "，")
    return re.sub(r"，{2,}", "，", text)


def _squeeze_punct(text: str) -> str:
    for pattern, repl in _PUNCT_RULES:
        text = pattern.sub(repl, text)
    return text


def _strip_markdown(text: str) -> str:
    for pattern, repl in _MD_RULES:
        text = pattern.sub(repl, text)
    return text


def _tidy(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_text(text: str, cfg: dict[str, Any]) -> str:
    """按配置清理一条回复。任何一步出错都退回原文。"""
    if not text or not text.strip():
        return text

    try:
        original = text
        text, slots = _protect(text)

        if cfg.get("markdown"):
            text = _strip_markdown(text)
        if cfg.get("clean"):
            text = _strip_openers(text)
            text = _strip_tail(text)
            text = _fix_dash(text)
        if cfg.get("squeeze"):
            text = _squeeze_punct(text)

        text = _tidy(text)
        restored = _restore(text, slots)
        # 清理后不能变成空消息：占位符之外的字全被删掉时退回原文
        if not restored.strip():
            return original
        return restored
    except Exception:  # noqa: BLE001 - 清理失败就用原文发出去
        return text


# ── 分段 ────────────────────────────────────────────────────────────────


def _atoms(text: str) -> list[str]:
    """把文本切成尽量小的语义单位：按行，行内按句。"""
    out: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        for sent in _SENT_RE.findall(line):
            sent = sent.strip()
            if sent:
                out.append(sent)
    return out


def _hard_split(atom: str, limit: int) -> list[str]:
    """单句超长时在逗号/顿号/空格处断开，找不到就硬切。"""
    parts: list[str] = []
    rest = atom
    while len(rest) > limit:
        floor = max(1, limit // 2)
        cut = -1
        for i in range(limit - 1, floor - 1, -1):
            if rest[i] in "，、,;； ":
                cut = i + 1
                break
        if cut <= 0:
            cut = limit
        parts.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        parts.append(rest)
    return [p for p in parts if p]


def split_segments(text: str, max_chars: int, max_parts: int) -> list[str]:
    """把一段话拆成若干条，每条不超过 ``max_chars``，总段数不超过 ``max_parts``。

    超过段数上限时不丢内容：把多出来的部分合并到最后一段。
    """
    text = (text or "").strip()
    if not text:
        return []
    if "```" in text or "`" in text or "http://" in text or "https://" in text:
        return [text]  # 含代码或链接：整条发，切开会把格式切坏

    max_chars = max(10, int(max_chars or 60))
    max_parts = max(1, int(max_parts or 4))

    if len(text) <= max_chars:
        return [text]

    atoms: list[str] = []
    for atom in _atoms(text):
        atoms.extend(_hard_split(atom, max_chars) if len(atom) > max_chars else [atom])

    segments: list[str] = []
    current = ""
    for atom in atoms:
        if not current:
            current = atom
        elif len(current) + len(atom) <= max_chars:
            current += atom
        else:
            segments.append(current)
            current = atom
    if current:
        segments.append(current)

    if len(segments) > max_parts:
        tail = "".join(segments[max_parts - 1 :])
        segments = segments[: max_parts - 1] + [tail]

    return [s for s in segments if s.strip()]

"""群菜单图。

群友 @ 机器人说「群聊记忆帮助」时回一张命令菜单图片。

**为什么是图片**：命令有十几条，纯文字刷屏，而且群聊里消息一多就被顶走了。
一张图能一次看完，还能被群友转发。

**字体怎么办**：不打包字体（Noto CJK 一家就 19MB，插件不值得为一张图胖这么多），
按顺序找现成的：

1. 插件自己的 ``data/fonts/``（用户想换字体丢一个进去就行）
2. AstrBot 的 ``data/fonts/``（AstrBot 自己渲染卡片要用，一般都有）
3. 系统常见路径（Linux / macOS / Windows 各找一遍）
4. 都找不到 → 返回 None，调用方退回纯文字菜单，绝不画一屏方块

Pillow 是可选依赖，缺了同样退文字。
"""

from __future__ import annotations

import time
from pathlib import Path

from astrbot.api import logger

# 菜单内容。改这里就是改图，不用碰绘制逻辑。
SECTIONS: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "学习风格",
        [
            ("/学习风格", "立刻提炼一次本群"),
            ("/风格状态", "看强度、特征条数、今日消耗"),
            ("/记忆审核", "看待审条目，可批准 / 否决"),
            ("/记忆审核 全批准", "全部通过"),
            ("/清空风格", "清掉本群学到的风格"),
        ],
    ),
    (
        "人格与关系",
        [
            ("/关系", "好感档位、认识几天、聊过几轮"),
            ("/称呼 老王", "改称呼；不带参数则清除"),
            ("/记得 我不吃香菜", "记住一件事（60 字内）"),
            ("/忘掉 香菜", "删掉一条记忆"),
        ],
    ),
    (
        "表情包",
        [
            ("/表情包", "图库统计和今日用量"),
            ("/收图 笑", "把消息里的图收进「笑」"),
            ("/收表情", "看这个号收藏了多少张"),
            ("/收表情 50 笑", "同步前 50 张到「笑」"),
        ],
    ),
    (
        "消费",
        [
            ("/消耗", "今天花了多少、花在哪"),
            ("/token", "同上，两个命令等价"),
        ],
    ),
]

# 找不到自带字体时按顺序试这些。系统路径排在后面——
# AstrBot 自带的那份优先级更高，因为它一定和 AstrBot 的语言环境匹配
FONT_CANDIDATES = [
    "/AstrBot/data/fonts/NotoSansCJK-Regular.ttc",
    "/AstrBot/data/fonts/card/01-NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
]

W, PAD = 760, 34
BG = (20, 22, 28)
FG = (232, 236, 244)
MUTED = (139, 147, 163)
ACCENT = (110, 168, 254)
CMD = (126, 200, 140)


def find_font(plugin=None) -> str:
    """找一个能画中文的字体文件。找不到返回空串。"""
    dirs = []
    if plugin is not None:
        base = Path(getattr(plugin, "data_dir", "") or "")
        if base:
            dirs.append(base / "fonts")
    for folder in dirs:
        try:
            for ext in ("*.ttf", "*.ttc", "*.otf"):
                hit = next(iter(sorted(folder.glob(ext))), None)
                if hit is not None:
                    return str(hit)
        except OSError:
            continue
    for path in FONT_CANDIDATES:
        if Path(path).is_file():
            return path
    return ""


def _fit(draw, text: str, font, max_width: int) -> str:
    """超宽就截断加省略号。中文没有断词，只能按字符砍。"""
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…"


def build_menu_image(plugin) -> Path | None:
    """画一张菜单图，返回文件路径。Pillow 或字体缺一个就返回 None。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        logger.debug("[群聊记忆] 没装 Pillow，菜单图退回文字")
        return None

    out = Path(plugin.data_dir) / "menu.png"
    # 内容写死在 SECTIONS 里，一小时之内不用重画。群友连点几次也不会一直跑 PIL
    try:
        if out.is_file() and time.time() - out.stat().st_mtime < 3600:
            return out
    except OSError:
        pass

    font_path = find_font(plugin)
    if not font_path:
        logger.debug("[群聊记忆] 找不到中文字体，菜单图退回文字")
        return None

    title_f = ImageFont.truetype(font_path, 34)
    section_f = ImageFont.truetype(font_path, 21)
    cmd_f = ImageFont.truetype(font_path, 19)
    desc_f = ImageFont.truetype(font_path, 17)

    # 先按排版规则把 y 走一遍，量出真实高度再开画布。
    # 按「估算值」开画布踩过：最后一行和页脚叠在一起了
    top = PAD + 88
    y_walk = top
    for _, items in SECTIONS:
        y_walk += 44 + len(items) * 33
    height = y_walk + PAD + 34

    img = Image.new("RGB", (W, height), BG)
    draw = ImageDraw.Draw(img)

    draw.text((PAD, PAD), "群聊记忆 · 命令菜单", font=title_f, fill=FG)
    draw.text(
        (PAD, PAD + 44),
        "@我 说「群聊记忆帮助」随时调出这张图",
        font=desc_f,
        fill=MUTED,
    )

    y = PAD + 88
    for name, items in SECTIONS:
        # 一根 4px 的竖条当分组标记：比 emoji 稳，任何字体都画得出来
        draw.rounded_rectangle(
            [PAD, y + 3, PAD + 4, y + 23], radius=2, fill=ACCENT
        )
        draw.text((PAD + 14, y), name, font=section_f, fill=ACCENT)
        y += 36
        for cmd, desc in items:
            draw.text((PAD + 8, y), cmd, font=cmd_f, fill=CMD)
            # 说明文字靠右对齐，命令长短不一时看起来整齐
            text = _fit(draw, desc, desc_f, W - PAD * 2 - 250)
            tw = draw.textlength(text, font=desc_f)
            draw.text((W - PAD - tw, y + 2), text, font=desc_f, fill=MUTED)
            y += 33
        y += 8

    tail = "面板里能改所有开关；命令只是快捷方式"
    draw.text((PAD, y + 14), tail, font=desc_f, fill=MUTED)

    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG", optimize=True)
    logger.info(f"[群聊记忆] 菜单图已生成: {out}")
    return out


def menu_text() -> str:
    """画不出图时的纯文字版。内容同源，改 SECTIONS 两边一起变。"""
    lines = ["群聊记忆 · 命令菜单", ""]
    for name, items in SECTIONS:
        lines.append(name)
        for cmd, desc in items:
            lines.append(f"  {cmd}　—　{desc}")
        lines.append("")
    lines.append("面板里能改所有开关，命令只是快捷方式")
    return "\n".join(lines)

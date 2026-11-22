"""群菜单图的断言。用法：python dev/test_menu.py

只验证「能画出来、内容没漏、缺依赖时能退回文字」，不比对像素。
"""

from __future__ import annotations

import importlib.util
import logging
import shutil
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

stub = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
api.logger = logging.getLogger("test")
stub.api = api
sys.modules.setdefault("astrbot", stub)
sys.modules.setdefault("astrbot.api", api)

spec = importlib.util.spec_from_file_location("menu", ROOT / "core" / "menu.py")
menu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(menu)

ok = True


def chk(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name, extra)
    ok = ok and bool(cond)


tmp = Path(tempfile.mkdtemp(prefix="menu_test_"))


class FakePlugin:
    def __init__(self, data_dir):
        self.data_dir = data_dir


try:
    plugin = FakePlugin(tmp)

    # ── 内容 ──
    text = menu.menu_text()
    chk("文字版有标题", "命令菜单" in text)
    chk("文字版有全部分组", all(name in text for name, _ in menu.SECTIONS))
    cmds = [c for _, items in menu.SECTIONS for c, _ in items]
    chk("命令数不少于 15 条", len(cmds) >= 15, str(len(cmds)))
    chk("文字版列出全部命令", all(c in text for c in cmds))
    # 这些是用户在群里最常打错的，必须有
    for must in ("/学习风格", "/关系", "/记得", "/表情包", "/收表情", "/消耗"):
        chk(f"含 {must}", must in text)

    # ── 画图 ──
    font = menu.find_font(plugin)
    if font:
        chk("找到字体", bool(font), font)
        path = menu.build_menu_image(plugin)
        chk("生成图片", path is not None and path.is_file(), str(path))
        if path:
            from PIL import Image

            with Image.open(path) as im:
                chk("宽度固定 760", im.width == menu.W, str(im.width))
                chk("高度够放下全部内容", im.height > 600, str(im.height))
                chk("是真图片不是空白", im.getbbox() is not None)
        # 第二次调用走缓存，不再重画
        again = menu.build_menu_image(plugin)
        chk("一小时内复用同一份", again == path, str(again))
    else:
        chk("没有字体时返回 None", menu.build_menu_image(plugin) is None)

    # ── 找不到字体 ──
    orig = menu.FONT_CANDIDATES
    menu.FONT_CANDIDATES = []
    empty = FakePlugin(tmp / "nofont")
    empty.data_dir.mkdir(parents=True, exist_ok=True)
    chk("无字体时不画图、返回 None", menu.build_menu_image(empty) is None)
    menu.FONT_CANDIDATES = orig

    # ── Pillow 缺失 ──
    orig_pil = sys.modules.get("PIL")
    sys.modules["PIL"] = None  # type: ignore[assignment]
    try:
        chk("没 Pillow 时返回 None", menu.build_menu_image(FakePlugin(tmp / "nopil")) is None)
    finally:
        if orig_pil is not None:
            sys.modules["PIL"] = orig_pil
        else:
            sys.modules.pop("PIL", None)

finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("全部通过" if ok else "有失败项")
sys.exit(0 if ok else 1)

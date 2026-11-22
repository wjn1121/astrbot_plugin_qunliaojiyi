"""表情包：本地图库 + 关键词/跟图两种触发。

成本模型：全程不发请求、不进 prompt。判定是本地的字符串命中加一次概率，
发图走 ``Comp.Image.fromFileSystem`` 直读本地文件，不经过模型，token 为 0。
这和「学习」那条链路互不影响——学不学、注不注入，表情包都照发。

图库放在 ``data/stickers/<标签>/`` 下，用英文目录名（中文目录在个别系统上
会因为编码问题打不开），面板和命令看到的都是中文标签。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import io
import json
import math
import os
import random
import re
import shutil
import time
import uuid
from pathlib import Path

from astrbot.api import logger

from .feed import today_str

# 内置分类。用户可以在面板改描述和关键词，也可以自己加新的。
#
# 字段说明：
#   key      目录名。内置的用英文（老数据就在这些目录里，不能改）；
#            用户新建的自动生成 s<时间戳>，不用他们操心目录长什么样。
#   name     显示名，也是模型输出 <emo:名字> 时用的那个词。
#   desc     给模型看的一句话。marker 模式下会拼进注入规则——
#            这是「分类描述进 prompt」的全部价值：让模型知道这个桶装什么。
#   keywords 本地匹配用。关键词模式下命中就发，不花一分钱。
DEFAULT_CATEGORIES: list[dict] = [
    {
        "key": "laugh",
        "name": "笑",
        "desc": "开心、大笑、忍不住了",
        "keywords": ["哈哈", "笑死", "绷不住", "好笑", "乐死", "笑鼠"],
    },
    {
        "key": "speechless",
        "name": "无语",
        "desc": "无话可说、服了、离谱",
        "keywords": ["无语", "服了", "离谱", "沉默", "麻了", "逆天"],
    },
    {
        "key": "praise",
        "name": "夸",
        "desc": "称赞、佩服、觉得厉害",
        "keywords": ["牛逼", "太强", "厉害", "yyds", "大佬", "666"],
    },
    {
        "key": "cry",
        "name": "哭",
        "desc": "难过、破防、撑不住",
        "keywords": ["呜呜", "破防", "难受", "emo", "心累", "裂开"],
    },
    {
        "key": "shock",
        "name": "惊",
        "desc": "惊讶、没想到、震惊",
        "keywords": ["卧槽", "我去", "什么鬼", "好家伙", "真的假的", "震惊"],
    },
    {
        "key": "chill",
        "name": "摆烂",
        "desc": "摸鱼、躺平、不想干了",
        "keywords": ["摸鱼", "摆烂", "躺平", "下班", "开润", "累了"],
    },
    {
        "key": "ok",
        "name": "好",
        "desc": "答应、收到、没问题",
        "keywords": ["好的", "收到", "没问题", "可以", "行吧", "ok"],
    },
    {
        "key": "awkward",
        "name": "尴尬",
        "desc": "社死、捂脸、不知道该怎么接",
        "keywords": ["尴尬", "社死", "捂脸", "没眼看", "尬住", "难绷"],
    },
    {
        "key": "love",
        "name": "爱",
        "desc": "贴贴、比心、喜欢",
        "keywords": ["贴贴", "比心", "喜欢你", "爱你", "抱抱", "可爱"],
    },
    {
        "key": "disdain",
        "name": "嫌弃",
        "desc": "就这、不感兴趣、看不下去",
        "keywords": ["嫌弃", "就这", "辣眼睛", "恶心", "笨蛋", "滚蛋"],
    },
]

ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
# 单张上限。GIF 另算——动图压了就废了，只能靠上传时拦。
MAX_BYTES = 5 * 1024 * 1024
# 目录索引缓存：每个群消息都 listdir 一次没必要，5 秒内复用
INDEX_TTL = 5.0
# 分类数量上限。每个分类都会进注入规则，太多就不是「省钱」了
MAX_CATEGORIES = 24
# 分类名里不能出现的字符（路径分隔符 + Windows 保留字符）
BAD_NAME_CHARS = set('/\\:*?"<>|')

# 模型打的情绪标记。括号、冒号中英文、空格都留活口——
# 它写不成 <emo:笑> 是常态，写成 [emo：笑] 也得认。
# 必须有 emo 字样：不然「【笑】」这种正常写法也会被当成标记吃掉。
MARK_RE = re.compile(
    r"[<\[【(（]\s*(?:emo|emoji)\s*[:：]\s*([^<>\[\]【】()（）\s]{1,12})\s*[>\]】)）]",
    re.IGNORECASE,
)

MOOD_MODES = ("off", "keyword", "analyze", "marker")

# 自动打标的提示词。三条要求里最要紧的是最后一条——
# 模型很容易写「美食展示」这种书面语，而群里说的是「放毒」「馋死」。
# 标签是拿来撞回复原文的，用书面语等于白标。
TAG_PROMPT = (
    "这是聊天用的表情包。分析后只输出 JSON，不要解释、不要代码块：\n"
    '{"text": "图上文字原文，没有就空字符串", '
    '"tags": ["3-6个关键词"], '
    '"desc": "一句话说什么时候发这张图，20字以内"}\n'
    "tags 要求：\n"
    "- 必须包含图上文字里的词\n"
    "- 加上情绪词（崩溃、无语、得意、嫌弃…）\n"
    "- 加上画面主体（熊猫头、猫、狗头、某个角色）\n"
    "- 用群里会说的口语，比如写「放毒」而不是「美食展示」"
)

# 打标只压缩到 512 就够了。表情包不需要高清，
# 一张 512 的图进模型约 200 token，原图可能翻几倍。
TAG_MAX_SIDE = 512

# 分析模式用的系统提示。分类列表是动态拼的，见 StickerStore.analyze_system()。
ANALYZE_SYSTEM_HEAD = (
    "判断这句话适合配哪种表情，只输出一个词，不要解释。\n"
    "可选：{names}\n"
    "判断不了就输出「无」。"
)


def clean_category_name(name: str) -> str:
    """规范化用户填的分类名。空、带路径分隔符、太长的都拒绝。"""
    text = (name or "").strip()
    if not text:
        return ""
    if len(text) > 12:
        return ""
    if any(ch in BAD_NAME_CHARS for ch in text):
        return ""
    if text.startswith("."):
        return ""
    return text


def _json_payload(tags: list, desc: str) -> str:
    """给「只识别不落库」那条路用的返回体。"""
    return json.dumps({"tags": tags, "desc": desc}, ensure_ascii=False)


def _parse_tag_json(raw: str) -> dict | None:
    """从模型输出里抠出 JSON。

    有人规规矩矩只给 JSON，有人裹一层 ```json，还有人前后加一句话。
    三种都兜住，抠不出来就返回 None，不硬猜。
    """
    if not raw:
        return None

    text = raw.strip()
    # 去 markdown 围栏
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()

    # 找第一个完整的 {...}
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
                return data if isinstance(data, dict) else None
    return None


def _ext_ok(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in ALLOWED_EXT


class StickerStore:
    """表情包索引、触发判定与发送记账。

    不持有消息对象，只回答「这次该不该发、发哪张」，由 hooks.py 负责真正发出去。
    """

    def __init__(self, plugin):
        self.plugin = plugin
        self.root: Path = plugin.data_dir / "stickers"
        self.stats_path: Path = plugin.data_dir / "sticker_stats.json"
        self.labels_path: Path = plugin.data_dir / "sticker_labels.json"
        # 每张图的元数据：标签、用过几次、上次用是什么时候。
        # 键是 "分类key/文件名"，和目录结构一一对应。
        self.index_path: Path = plugin.data_dir / "sticker_index.json"

        self.root.mkdir(parents=True, exist_ok=True)
        self.categories: list[dict] = self._load_categories()
        self._name_map: dict[str, str] = {}
        self._rebuild_name_map()

        self._index: dict[str, list[str]] = {}
        self._index_at: float = 0.0
        self._stats: dict = self._load_stats()
        self.meta: dict = self._load_meta()
        self._dirty = False
        self._meta_dirty = False
        # 标签 IDF 缓存：图库或标签变了就失效
        self._idf_cache: dict[str, float] = {}
        self._idf_at: float = 0.0
        self._save_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

        # 同群冷却的临时表：session_id -> 上次发图时间（秒）
        self._last_sent: dict[str, float] = {}
        # 队形检测：session_id -> 最近几张图的时间戳
        self._img_burst: dict[str, list[float]] = {}

    # ── 分类表 ──────────────────────────────────────────────────────────
    #
    # 分类不再写死在代码里：sticker_labels.json 是唯一事实来源，用户可以
    # 改描述、改关键词、加新分类。内置那十个只在文件不存在时写一次，
    # 之后用户删掉的也照删，不会被「升级时补回来」。

    def _load_categories(self) -> list[dict]:
        raw: list | None = None
        if self.labels_path.exists():
            try:
                data = json.loads(self.labels_path.read_text(encoding="utf-8"))
                items = data.get("items") if isinstance(data, dict) else None
                if isinstance(items, list):
                    raw = items
            except (OSError, json.JSONDecodeError) as e:
                logger.error(f"[群聊记忆] 读取分类表失败，先用内置的: {e}")

        source = raw if raw is not None else DEFAULT_CATEGORIES
        items: list[dict] = []
        seen: set[str] = set()
        for row in source:
            if not isinstance(row, dict):
                continue
            key = str(row.get("key") or "").strip()
            name = clean_category_name(str(row.get("name") or ""))
            if not key or not name or key in seen:
                continue
            if any(ch in BAD_NAME_CHARS for ch in key):
                continue
            seen.add(key)
            kws = row.get("keywords")
            items.append(
                {
                    "key": key,
                    "name": name,
                    "desc": str(row.get("desc") or "").strip()[:60],
                    "keywords": [
                        str(x).strip() for x in (kws or []) if str(x).strip()
                    ][:30],
                    "builtin": bool(row.get("builtin")),
                }
            )

        if not items:
            items = [
                {
                    "key": c["key"],
                    "name": c["name"],
                    "desc": c["desc"],
                    "keywords": list(c["keywords"]),
                    "builtin": True,
                }
                for c in DEFAULT_CATEGORIES
            ]

        if raw is None:
            # 第一次跑：把内置的写出去，用户之后能在面板里改
            self.categories = items
            self._save_categories()
        return items

    def _save_categories(self) -> bool:
        try:
            self.labels_path.parent.mkdir(parents=True, exist_ok=True)
            self.labels_path.write_text(
                json.dumps(
                    {"version": 2, "items": self.categories},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            return True
        except OSError as e:
            logger.error(f"[群聊记忆] 保存分类表失败: {e}")
            return False

    def _rebuild_name_map(self) -> None:
        """显示名和目录名都指向同一个 key，模型写哪个都认。"""
        mapping: dict[str, str] = {}
        for cat in self.categories:
            mapping[cat["name"]] = cat["key"]
            mapping[cat["key"]] = cat["key"]
        self._name_map = mapping
        self._index_at = 0.0

    def names(self) -> list[str]:
        return [cat["name"] for cat in self.categories]

    def find(self, token: str) -> dict | None:
        key = self._name_map.get((token or "").strip())
        if not key:
            return None
        return next((c for c in self.categories if c["key"] == key), None)

    def category_of(self, key: str) -> dict | None:
        return next((c for c in self.categories if c["key"] == key), None)

    def add_category(
        self, name: str, desc: str = "", keywords: list | None = None
    ) -> tuple[bool, str]:
        clean = clean_category_name(name)
        if not clean:
            return False, "分类名不能为空，最多 12 个字，也不能带 / : ? 这类符号"
        if any(c["name"] == clean for c in self.categories):
            return False, f"已经有「{clean}」这个分类了"
        if len(self.categories) >= MAX_CATEGORIES:
            return False, f"最多 {MAX_CATEGORIES} 个分类，再多注入规则就太长了"

        key = f"s{int(time.time() * 1000)}"
        self.categories.append(
            {
                "key": key,
                "name": clean,
                "desc": (desc or "").strip()[:60],
                "keywords": [str(x).strip() for x in (keywords or []) if str(x).strip()][
                    :30
                ],
                "builtin": False,
            }
        )
        if not self._save_categories():
            self.categories.pop()
            return False, "写分类表失败"
        self._rebuild_name_map()
        try:
            (self.root / key).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning(f"[群聊记忆] 建分类目录失败: {e}")
        return True, key

    def update_category(
        self, key: str, *, name=None, desc=None, keywords=None
    ) -> tuple[bool, str]:
        cat = self.category_of(key)
        if cat is None:
            return False, "没有这个分类"

        if name is not None:
            clean = clean_category_name(name)
            if not clean:
                return False, "分类名不合法"
            if clean != cat["name"] and any(c["name"] == clean for c in self.categories):
                return False, f"已经有「{clean}」了"
            cat["name"] = clean
        if desc is not None:
            cat["desc"] = str(desc).strip()[:60]
        if keywords is not None:
            cat["keywords"] = [
                str(x).strip() for x in keywords if str(x).strip()
            ][:30]

        if not self._save_categories():
            return False, "写分类表失败"
        self._rebuild_name_map()
        return True, "已保存"

    def remove_category(self, key: str) -> tuple[bool, str]:
        cat = self.category_of(key)
        if cat is None:
            return False, "没有这个分类"
        folder = self.root / key
        try:
            if folder.is_dir() and any(folder.iterdir()):
                return False, "这个分类里还有图，先把图删掉或改到别的分类"
        except OSError:
            pass

        self.categories = [c for c in self.categories if c["key"] != key]
        if not self._save_categories():
            return False, "写分类表失败"
        self._rebuild_name_map()
        try:
            folder.rmdir()
        except OSError:
            pass
        return True, f"已删除「{cat['name']}」"

    def rule_text(self) -> str:
        """拼注入给模型的规则。

        「分类描述进 prompt」就在这一段：模型读完这些描述才知道每个桶
        装的是什么，而不是只看到几个孤立的名词。
        """
        # 只列有图的分类。空分类写给模型，它选中了也发不出来，
        # 白白把这一轮的配图机会浪费掉，还多占几行 token。
        idx = self.index()
        lines = []
        for cat in self.categories:
            if not idx.get(cat["key"]):
                continue
            desc = cat["desc"]
            lines.append(f"- {cat['name']}：{desc}" if desc else f"- {cat['name']}")
        if not lines:
            return ""
        return (
            "<sticker_rule>\n"
            "如果这句话适合配一张表情图，就在回复最末尾加上 <emo:分类名>。\n"
            "可选分类：\n" + "\n".join(lines) + "\n"
            "贴切才加，不贴切就不加；一句话最多一个；不要解释这个标记。\n"
            "</sticker_rule>"
        )

    def rule_tokens(self) -> int:
        """注入规则大概多少 token。

        粗估按「一个中文字 ≈ 一个 token」——DeepSeek 实际比这更省，
        但估算往高了报，用户才不会觉得面板在骗他。
        """
        return len(self.rule_text())

    def analyze_system(self) -> str:
        """分析模式的系统提示，分类列表跟着分类表走。

        只列有图的分类，理由和 rule_text 一样：列了没图的，模型答了也白答。
        """
        idx = self.index()
        names = [c["name"] for c in self.categories if idx.get(c["key"])]
        return ANALYZE_SYSTEM_HEAD.format(names="、".join(names) or "无")

    def strip_marks(self, text: str) -> tuple[str, list[str]]:
        """剥掉情绪标记，返回 (清理后文本, 命中的分类 key 列表)。

        只剥认识的分类。模型自己编的（比如 <emo:开心>）原样留着——
        宁可多留一个古怪标记，也不能把正常内容吃掉。
        """
        if not text:
            return "", []

        hits: list[str] = []

        def repl(match: re.Match) -> str:
            token = (match.group(1) or "").strip()
            key = self._name_map.get(token)
            if key is None:
                return match.group(0)
            hits.append(key)
            return ""

        cleaned = MARK_RE.sub(repl, text)
        if hits:
            # 标记删掉后可能留下「你好 。」这种空隙，收一下
            cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
            cleaned = re.sub(r"[ \t]+([。！？，、；：])", r"\1", cleaned).strip()
        return cleaned, hits

    # ── 配置 ────────────────────────────────────────────────────────────

    def _cfg(self) -> dict:
        """读 11 个扁平配置项。

        做成扁平而不是分组，是为了让面板的参数页能热调——那边的热调
        写 runtime.json，只认扁平 key。settings.get 会先看 runtime 再回落
        到配置文件，所以面板改完下一轮就生效。
        """

        def read(key: str, default):
            try:
                value = self.plugin.settings.get(key)
            except Exception:  # noqa: BLE001 - 老配置或测试桩没有这一项
                return default
            return default if value is None else value

        mode = str(read("sticker_mood_mode", "keyword") or "keyword").strip().lower()
        if mode not in MOOD_MODES:
            mode = "keyword"

        return {
            "enabled": read("sticker_enabled", False),
            "mode": mode,
            "mood_rate": read("sticker_mood_rate", 30),
            "probability": read("sticker_probability", 25),
            "follow_image": read("sticker_follow_image", True),
            "follow_probability": read("sticker_follow_probability", 15),
            "chain": read("sticker_chain", False),
            "formation": read("sticker_formation", False),
            "cooldown_seconds": read("sticker_cooldown_seconds", 120),
            "daily_limit": read("sticker_daily_limit", 40),
            "analyze_provider_id": str(read("sticker_analyze_provider_id", "") or ""),
            "search_first": read("sticker_search_first", True),
            "search_threshold": read("sticker_search_threshold", 1.0),
            "tag_provider_id": str(read("sticker_tag_provider_id", "") or ""),
        }

    def config(self) -> dict:
        """给命令和面板读当前值用。"""
        return self._cfg()

    def enabled(self) -> bool:
        return bool(self._cfg().get("enabled"))

    # ── 索引 ────────────────────────────────────────────────────────────

    def index(self, *, force: bool = False) -> dict[str, list[str]]:
        """扫一遍各标签目录，返回 {标签: [文件名...]}。

        只收有内容的标签；空目录不进索引，这样「还没往里放图」的标签
        不会在触发时被选中再发现没图。
        """
        now = time.time()
        if not force and self._index and (now - self._index_at) < INDEX_TTL:
            return self._index

        found: dict[str, list[str]] = {}
        for cat in self.categories:
            key = cat["key"]
            folder = self.root / key
            if not folder.is_dir():
                continue
            try:
                names = [
                    n
                    for n in os.listdir(folder)
                    if _ext_ok(n) and (folder / n).is_file()
                ]
            except OSError as e:
                logger.debug(f"[群聊记忆] 读取表情包目录 {folder} 失败: {e}")
                continue
            if names:
                found[key] = sorted(names)

        self._index = found
        self._index_at = now
        return found

    def invalidate(self) -> None:
        """上传/删除后调用，让下一次判定重新扫目录、重算标签权重。"""
        self._index_at = 0.0
        self._idf_at = 0.0

    def counts(self) -> dict[str, int]:
        idx = self.index()
        return {c["key"]: len(idx.get(c["key"], [])) for c in self.categories}

    def total(self) -> int:
        return sum(self.counts().values())

    def path_of(self, label: str, name: str) -> Path:
        return self.root / label / name

    # ── 触发判定 ────────────────────────────────────────────────────────

    def match_label(self, text: str) -> str | None:
        """文字里命中哪个分类。多条命中时取最先出现的那条关键词所属分类。"""
        if not text:
            return None
        low = text.lower()
        best_pos = len(low) + 1
        best: str | None = None
        for cat in self.categories:
            for word in cat["keywords"]:
                pos = low.find(str(word).lower())
                if pos < 0:
                    continue
                if pos < best_pos:
                    best_pos = pos
                    best = cat["key"]
        return best

    def _today(self) -> dict:
        """今天的记账槽。跨天自动换新，旧的不留。"""
        day = today_str()
        if self._stats.get("date") != day:
            self._stats = {"date": day, "total": 0, "by_label": {}, "by_session": {}}
            self._dirty = True
        return self._stats

    def today_total(self) -> int:
        return int(self._today().get("total") or 0)

    def _cooling(self, session_id: str, cooldown: float) -> bool:
        if cooldown <= 0:
            return False
        last = self._last_sent.get(session_id, 0.0)
        return (time.time() - last) < cooldown

    def decide(
        self,
        session_id: str,
        text: str = "",
        has_image: bool = False,
        mood: str | None = None,
    ) -> tuple[str, Path] | None:
        """决定这一轮发不发、发哪张。返回 (标签, 文件路径) 或 None。

        ``mood`` 是外部给的标签——模型打的标记，或者事后分析出的情绪。
        给了就用它，不再看本地关键词；没给才回落到关键词 / 跟图。

        判定顺序：总开关 → 模式 → 日限 → 冷却 → 标签来源 → 概率。
        日限和冷却是所有来源共用的硬闸，不管标签从哪来。
        """
        got = self._gate(session_id)
        if got is None:
            return None
        cfg, idx = got

        mode = cfg.get("mode", "keyword")
        probability = float(cfg.get("probability", 25) or 0)

        label = ""
        hit_word = self.match_label(text) if mode == "keyword" else None
        if mood:
            if mood in idx:
                # 外部标签已经判断过一次，不再掷骰——再打折等于把那次判断当噪音
                label = mood
            elif random.random() * 100 < probability:
                # 给了标签但那个分类还没图，退一步从有图的里挑：
                # 至少这一轮"该发表情"这件事是成立的
                label = random.choice(list(idx))
        elif hit_word:
            if hit_word in idx:
                if random.random() * 100 < probability:
                    label = hit_word
            elif random.random() * 100 < probability * 0.5:
                # 命中标签但那个标签空着，折半概率换一个
                label = random.choice(list(idx))
        elif has_image and bool(cfg.get("follow_image", True)):
            # 群友发了图，就跟着甩一张。不判断图里是什么——
            # 那要视觉模型，每张都是钱，而斗图本来也不需要看懂对方在发什么
            if random.random() * 100 < float(cfg.get("follow_probability", 15) or 0):
                label = random.choice(list(idx))

        if not label:
            return None
        return self._pack(idx, label, text)

    def pick_formation(self, session_id: str) -> tuple[str, Path] | None:
        """队形触发：群友在刷图，跟着甩一张。

        不看概率也不看模式——这是"大家都在发，我也发"的从众行为，
        和"该不该发表情"是两回事。日限和冷却照样要过。
        """
        got = self._gate(session_id)
        if got is None:
            return None
        cfg, idx = got
        if not bool(cfg.get("formation")):
            return None
        return self._pack(idx, random.choice(list(idx)))

    def note_image(self, session_id: str, window: float = 30.0, need: int = 3) -> bool:
        """记一张图，返回这一下是不是刚好凑够「群友在刷图」。

        凑够之后清空计数，避免后面每张图都触发一次。
        """
        now = time.time()
        bucket = self._img_burst.setdefault(session_id, [])
        bucket.append(now)
        bucket[:] = [t for t in bucket if now - t <= window]
        if len(bucket) >= need:
            bucket.clear()
            return True
        return False

    def chain_follow(self, session_id: str, label: str) -> tuple[str, Path] | None:
        """连发：刚发完一张，按概率再补一张同类的。"""
        got = self._gate(session_id)
        if got is None:
            return None
        cfg, idx = got
        if not bool(cfg.get("chain")):
            return None
        if random.random() * 100 >= float(cfg.get("follow_probability", 15) or 0):
            return None
        # 优先同一标签，那个标签只有一张图就换一个
        names = idx.get(label) or []
        if len(names) <= 1:
            label = random.choice([k for k in idx if k != label] or list(idx))
        return self._pack(idx, label)

    def _gate(self, session_id: str):
        """总开关 / 模式 / 图库 / 日限 / 冷却。过了返回 (cfg, index)。"""
        if not self.enabled():
            return None
        cfg = self._cfg()
        if cfg.get("mode", "keyword") == "off":
            return None
        idx = self.index()
        if not idx:
            return None
        if self.today_total() >= int(cfg.get("daily_limit", 40) or 0):
            return None
        if self._cooling(session_id, float(cfg.get("cooldown_seconds", 120) or 0)):
            return None
        return cfg, idx

    def gate_ok(self, session_id: str) -> bool:
        """日限和冷却还让不让发。

        检索路径不经过 decide，但这两道闸是全局的，一样要过。
        """
        return self._gate(session_id) is not None

    def _pack(
        self, idx: dict[str, list[str]], label: str, text: str = ""
    ) -> tuple[str, Path] | None:
        names = idx.get(label) or []
        if not names:
            return None
        name = self._choose(label, names, text)
        if not name:
            return None  # 这一类全被停用了
        return label, self.path_of(label, name)

    def _choose(self, label: str, names: list[str], text: str = "") -> str:
        """在这一类里挑一张。

        两级：标签命中消息里的词就优先（多个同分随机，避免总挑同一张）；
        都没命中就按「用得少 + 久没用」加权，新图和冷门图更容易被抽到。
        加权这一步顺带解决了纯随机下「那几张顺手的被反复抽」的问题。
        """
        # 停用的图先剔掉。放在最前面：后面所有的打分都基于可用集合，
        # 不然会出现「排序第一的那张被停用，于是这一轮不发」的哑火
        names = [n for n in names if self.is_enabled(label, n)]
        if not names:
            return ""
        if len(names) <= 1:
            return names[0]

        if text:
            scored: list[tuple[int, str]] = []
            for name in names:
                tags = self._meta_of(label, name).get("tags") or []
                hits = sum(1 for tag in tags if tag and str(tag) in text)
                if hits:
                    scored.append((hits, name))
            if scored:
                best = max(hits for hits, _ in scored)
                return random.choice([n for hits, n in scored if hits == best])

        now = time.time()
        weights: list[float] = []
        for name in names:
            row = self._meta_of(label, name)
            used = int(row.get("used") or 0)
            last = float(row.get("last_used") or 0)
            # 没记录过的当「七天没发」处理，给它一点新鲜度加成
            stale = min((now - last) / 86400.0, 14.0) if last else 7.0
            weights.append(1.0 / (1.0 + used * 0.25) * (1.0 + stale * 0.15))
        total = sum(weights)
        if total <= 0:
            return random.choice(names)
        return random.choices(names, weights=weights, k=1)[0]

    # ── 全库检索（参考 meme神偷 / 智能图片对话 的思路）──────────────────
    #
    # 不看分类，直接拿回复原文去撞每张图的标签。命中了就用那张——
    # 这是「选哪张」这一步能做到的最准的方式，而且全程不花钱。

    def _idf(self) -> dict[str, float]:
        """每个标签的信息量。图库里越少图带的标签，越能说明问题。

        「笑」可能五十张图都有，命中它几乎等于没命中；
        「熊猫头」只有三张有，撞上就该是那三张。
        和 BM25 里的 IDF 是同一件事，只是这里不需要词频。
        """
        now = time.time()
        if self._idf_cache and (now - self._idf_at) < 10:
            return self._idf_cache

        idx = self.index()
        df: dict[str, int] = {}
        total = 0
        for cat in self.categories:
            key = cat["key"]
            for name in idx.get(key, []):
                total += 1
                for tag in set(self._meta_of(key, name).get("tags") or []):
                    if tag:
                        df[tag] = df.get(tag, 0) + 1

        scores = {
            tag: math.log((total + 1) / (count + 1)) + 1.0
            for tag, count in df.items()
        }
        self._idf_cache = scores
        self._idf_at = now
        return scores

    def search(self, text: str) -> tuple[str, Path] | None:
        """全库撞标签，返回 (分类key, 文件路径) 或 None。

        不分区、不分词——标签本身就是 2~8 字的短词，直接判断它有没有
        出现在这段文本里。100 张图 300 个标签也就是几百次字符串包含，
        微秒级，不发请求、不耗 token。
        """
        text = (text or "").strip()
        if not text:
            return None
        idx = self.index()
        if not idx:
            return None

        cfg = self._cfg()
        threshold = float(cfg.get("search_threshold", 1.0) or 0)
        idf = self._idf()

        best_score = 0.0
        best: list[tuple[str, str]] = []

        for cat in self.categories:
            key = cat["key"]
            for name in idx.get(key, []):
                if not self.is_enabled(key, name):
                    continue
                tags = self._meta_of(key, name).get("tags") or []
                score = 0.0
                for tag in tags:
                    if not tag or tag not in text:
                        continue
                    # 权重就是标签的信息量。单字标签不做额外打折——
                    # 那会让「猫」这类很实用的标签直接失效，而 IDF
                    # 已经按「多少张图带它」把常见词的权重压下去了。
                    score += idf.get(tag, 1.0)
                if score <= 0:
                    continue
                if score > best_score + 1e-9:
                    best_score = score
                    best = [(key, name)]
                elif abs(score - best_score) <= 1e-9:
                    best.append((key, name))

        if not best or best_score < threshold:
            return None
        key, name = random.choice(best)
        return key, self.path_of(key, name)

    def budget_blocked(self) -> bool:
        """今天钱花超了没。超了就不再让模型参与「该不该发」的判断。

        注意只拦**判断**，不拦发图本身：从本地图库里挑一张不产生请求，
        超支了还能继续斗图，只是退回纯关键词模式。
        """
        budget = getattr(self.plugin, "budget", None)
        return budget is not None and budget.exceeded()

    def should_inject_rule(self) -> bool:
        """这一轮要不要告诉模型「你可以发表情」。

        只在 marker 模式下掷骰，命中率来自 sticker_mood_rate。
        没命中的轮次模型不知道有这回事，自然不会想着加标记——
        这比"告诉了但事后按概率丢掉"更省，也更像"偶尔想发一条"。
        """
        cfg = self._cfg()
        if not cfg.get("enabled") or cfg.get("mode") != "marker":
            return False
        if self.budget_blocked():
            return False
        rate = float(cfg.get("mood_rate", 30) or 0)
        if rate <= 0:
            return False
        if self.today_total() >= int(cfg.get("daily_limit", 40) or 0):
            return False
        return random.random() * 100 < rate

    def should_analyze(self) -> bool:
        """analyze 模式下这一轮要不要真的去调模型。"""
        cfg = self._cfg()
        if not cfg.get("enabled") or cfg.get("mode") != "analyze":
            return False
        # 分析模式每轮真调一次模型，超支了直接不发图（关键词模式仍可用）
        if self.budget_blocked():
            return False
        rate = float(cfg.get("mood_rate", 30) or 0)
        if rate <= 0:
            return False
        if self.today_total() >= int(cfg.get("daily_limit", 40) or 0):
            return False
        return random.random() * 100 < rate

    def record(self, session_id: str, label: str, name: str = "") -> None:
        """发出去之后记账：今日总数、按标签、按会话，以及这张图用了第几次。"""
        stats = self._today()
        stats["total"] = int(stats.get("total") or 0) + 1
        by_label = stats.setdefault("by_label", {})
        by_label[label] = int(by_label.get(label) or 0) + 1
        by_session = stats.setdefault("by_session", {})
        by_session[session_id] = int(by_session.get(session_id) or 0) + 1

        if name:
            key = f"{label}/{name}"
            row = dict(self.meta.get(key) or {})
            row["used"] = int(row.get("used") or 0) + 1
            row["last_used"] = time.time()
            self.meta[key] = row
            self._meta_dirty = True

        self._last_sent[session_id] = time.time()
        self._mark_dirty()

    # ── 事后分析（analyze 模式）────────────────────────────────────────

    async def analyze_mood(self, session_id: str, text: str) -> str | None:
        """拿 bot 这条回复问一次小模型「这是什么情绪」。

        失败一律返回 None（当作不发图）。配图是锦上添花，
        它坏了绝不能影响已经发出去的正常回复。
        """
        text = (text or "").strip()
        if not text:
            return None

        provider = await self._resolve_provider(session_id)
        if provider is None:
            return None

        try:
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=text[:400], contexts=[], system_prompt=self.analyze_system()
                ),
                timeout=8,
            )
        except Exception as e:  # noqa: BLE001 - 超时/断网/格式错都当不发
            logger.debug(f"[群聊记忆] 表情包情绪分析失败: {e}")
            return None

        try:
            # 面板的「表情包判断」那一行就是这笔账
            self.plugin.usage.record("sticker", getattr(resp, "usage", None), session_id)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[群聊记忆] 记录表情包消耗失败（可忽略）: {e}")

        return self._parse_mood(getattr(resp, "completion_text", "") or "")

    def _parse_mood(self, raw: str) -> str | None:
        """从模型回答里揪出一个分类。

        先看有没有整个等于某个分类名（避免「好」命中「好像」这类），
        再退到包含匹配，长的分类名优先。
        """
        if not raw:
            return None
        cleaned = re.sub(r"[\s。！？，、,.!?\"'`：:；;（）()\[\]【】]", "", raw)
        if not cleaned or cleaned == "无":
            return None
        for cat in self.categories:
            if cleaned == cat["name"]:
                return cat["key"]
        for cat in sorted(self.categories, key=lambda c: -len(c["name"])):
            if cat["name"] in cleaned:
                return cat["key"]
        return None

    async def _resolve_provider(self, session_id: str):
        """分析模式用的模型。指定了就用指定的，否则跟随当前会话。"""
        context = getattr(self.plugin, "context", None)
        if context is None:
            return None

        pid = (self._cfg().get("analyze_provider_id") or "").strip()
        if pid:
            try:
                provider = context.get_provider_by_id(provider_id=pid)
                if provider is not None:
                    return provider
                logger.warning(f"[群聊记忆] 表情包分析指定的 Provider 不存在: {pid}，回退默认")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[群聊记忆] 获取表情包分析 Provider 失败: {e}")

        try:
            return context.get_using_provider(umo=session_id)
        except TypeError:
            return context.get_using_provider()
        except Exception as e:  # noqa: BLE001
            logger.error(f"[群聊记忆] 获取默认 Provider 失败: {e}")
            return None

    # ── 增删（面板和 /收图 都走这里）──────────────────────────────────

    def add_file(
        self, label: str, name: str, blob: bytes, tags: list | None = None
    ) -> tuple[bool, str]:
        """把一张图写进某个标签。名字重名时自动加序号。"""
        if self.category_of(label) is None:
            return False, "没有这个分类"
        if not blob:
            return False, "图片是空的"
        if len(blob) > MAX_BYTES:
            return False, f"图片超过 {MAX_BYTES // 1024 // 1024}MB"

        folder = self.root / label
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return False, f"建目录失败：{e}"

        ext = os.path.splitext(name)[1].lower()
        if ext not in ALLOWED_EXT:
            ext = ".jpg"
        # 文件名必须唯一。以前用「秒_毫秒后三位」，批量上传时连续调用会撞名，
        # 后一张直接覆盖前一张——用户传五张只剩最后一张。
        stem = f"{int(time.time())}_{uuid.uuid4().hex[:8]}"

        target = folder / f"{stem}{ext}"
        try:
            target.write_bytes(blob)
        except OSError as e:
            return False, f"写文件失败：{e}"

        # 一条 meta 一次写完：标签、计数、哈希、入库时间。
        # 以前分两段写，第二次是「读出来改一个字段再写回去」，
        # 中间只要有一次异常，前面写的标签就白丢了。
        row = dict(self.meta.get(f"{label}/{target.name}") or {})

        # 标签：调用方给的最优先，没给就从原文件名猜（「笑死熊猫头.gif」这种）
        auto = [str(x).strip()[:12] for x in (tags or []) if str(x).strip()]
        if not auto:
            auto = self._tags_from_name(name)
        if auto:
            row["tags"] = list(dict.fromkeys(auto))[:12]

        row.setdefault("used", 0)
        row.setdefault("last_used", 0)
        # 入库时间。图库按「什么时候收的」排序要靠它；
        # 老图没有这个字段，读的时候用文件 mtime 兜底，不用改历史数据。
        row["added_at"] = time.time()
        try:
            row["hash"] = hashlib.md5(blob).hexdigest()
        except Exception:  # noqa: BLE001
            pass

        self.meta[f"{label}/{target.name}"] = row
        self._mark_meta_dirty()

        self.invalidate()
        return True, target.name

    def remove(self, label: str, name: str) -> tuple[bool, str]:
        """删一张图。文件名里带路径分隔符的直接拒绝，避免跳出目录。"""
        if self.category_of(label) is None:
            return False, "没有这个分类"
        if not name or os.path.basename(name) != name:
            return False, "文件名不合法"
        target = self.root / label / name
        if not target.is_file():
            return False, "这张图已经没了"
        try:
            target.unlink()
        except OSError as e:
            return False, f"删除失败：{e}"
        if self.meta.pop(f"{label}/{name}", None) is not None:
            self._mark_meta_dirty()
        self.invalidate()
        return True, "已删除"

    def move_file(self, label: str, name: str, target: str) -> tuple[bool, str]:
        """把一张图挪到另一个分类。重名时自动加后缀，不覆盖。"""
        if label == target:
            return False, "已经在这个分类里了"
        if self.category_of(label) is None or self.category_of(target) is None:
            return False, "分类不存在"
        if not name or os.path.basename(name) != name:
            return False, "文件名不合法"

        src = self.root / label / name
        if not src.is_file():
            return False, "这张图已经没了"
        dst_dir = self.root / target
        try:
            dst_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return False, f"建目录失败：{e}"

        dst = dst_dir / name
        if dst.exists():
            stem, ext = os.path.splitext(name)
            dst = dst_dir / f"{stem}_{int(time.time()) % 100000}{ext}"

        try:
            shutil.move(str(src), str(dst))
        except OSError as e:
            return False, f"移动失败：{e}"

        row = self.meta.pop(f"{label}/{name}", None)
        if row:
            self.meta[f"{target}/{dst.name}"] = row
            self._mark_meta_dirty()
        self.invalidate()
        return True, f"已挪到「{(self.category_of(target) or {}).get('name', target)}」"

    # ── 自动打标（视觉模型）──────────────────────────────────────────
    #
    # 一次性成本：每张图一次调用。标完之后检索和发图都在本地，
    # 不再有任何请求。所以「标 100 张」的钱是一次性的，不是每轮。

    def _image_data_url(self, path: Path) -> str:
        """把图片压到 512 再转 data URL。

        Pillow 缺失就传原图——打标失败总比整个功能不可用好。
        """
        raw: bytes
        try:
            from PIL import Image as PILImage  # 可选依赖

            with PILImage.open(path) as im:
                try:
                    im.seek(0)  # GIF 取第一帧
                except (EOFError, AttributeError):
                    pass
                frame = im.convert("RGB")
                if max(frame.size) > TAG_MAX_SIDE:
                    frame.thumbnail((TAG_MAX_SIDE, TAG_MAX_SIDE), PILImage.LANCZOS)
                buf = io.BytesIO()
                frame.save(buf, "JPEG", quality=82, optimize=True)
                raw = buf.getvalue()
        except Exception:  # noqa: BLE001 - 没 Pillow 或图坏了都直接传原图
            raw = path.read_bytes()
        return "data:image/jpeg;base64," + base64.b64encode(raw).decode()

    async def _resolve_tag_provider(self, provider_id: str = ""):
        """打标用的模型。优先用指定的，其次 AstrBot 全局的图片转述模型。"""
        context = getattr(self.plugin, "context", None)
        if context is None:
            return None

        pid = (provider_id or self._cfg().get("tag_provider_id") or "").strip()
        if pid:
            try:
                provider = context.get_provider_by_id(provider_id=pid)
                if provider is not None:
                    return provider
                logger.warning(f"[群聊记忆] 打标指定的 Provider 不存在: {pid}，回退默认")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[群聊记忆] 获取打标 Provider 失败: {e}")

        # AstrBot 全局配的「默认图片转述模型」，没有就试当前会话的
        for getter in ("get_using_provider",):
            try:
                return getattr(context, getter)()
            except Exception:  # noqa: BLE001
                continue
        return None

    async def auto_tag(
        self, label: str, name: str, apply: bool = True
    ) -> tuple[bool, str]:
        """让视觉模型给一张图打标签，结果合并进元数据。

        合并规则：新标签**追加**，不删旧的。模型偶尔标歪，
        一次误判不该把用户手写的标签冲掉。
        """
        path = self.path_of(label, name)
        if not path.is_file():
            return False, "这张图已经没了"
        if self.category_of(label) is None:
            return False, "没有这个分类"

        provider = await self._resolve_tag_provider()
        if provider is None:
            return False, "没有可用的图片识别模型"

        try:
            data_url = self._image_data_url(path)
        except Exception as e:  # noqa: BLE001
            return False, f"读图失败：{e}"

        prompt = TAG_PROMPT
        if path.suffix.lower() == ".gif":
            prompt += "\n（这是动图，只给了第一帧）"

        resp = None
        # 先试着关掉推理：deepseek-flash 带推理时一张 5 秒、花四倍的钱。
        # 这个参数不是所有 provider 都认，不认就退回普通调用。
        for kwargs in ({"thinking": {"type": "disabled"}}, {}):
            try:
                resp = await asyncio.wait_for(
                    provider.text_chat(
                        prompt=prompt,
                        image_urls=[data_url],
                        contexts=[],
                        **kwargs,
                    ),
                    timeout=60,
                )
                break
            except TypeError:
                continue  # 这个 provider 不接 thinking，换一个写法
            except Exception as e:  # noqa: BLE001 - 超时/断网/额度
                logger.warning(f"[群聊记忆] 表情包打标失败: {e}")
                return False, f"识别失败：{e}"

        if resp is None:
            return False, "识别失败"

        try:
            self.plugin.usage.record("sticker_tag", getattr(resp, "usage", None), "")
        except Exception:  # noqa: BLE001
            pass

        raw = (getattr(resp, "completion_text", "") or "").strip()
        parsed = _parse_tag_json(raw)
        if parsed is None:
            return False, f"模型没给出能用的结果：{raw[:60]}"

        tags = parsed.get("tags") or []
        desc = str(parsed.get("desc") or "").strip()[:60]
        text_on_image = str(parsed.get("text") or "").strip()[:20]
        if text_on_image and text_on_image not in tags:
            tags.insert(0, text_on_image)

        if not apply:
            # 面板单张识别：只把结果交回去填进输入框，用户点保存才落库。
            # 不然用户想改一下标签，取消之后发现库里已经被改了。
            return True, _json_payload(tags, desc)

        merged = self.meta_of(label, name)
        row = dict(self.meta.get(f"{label}/{name}") or {})

        # 图上文字排最前：它是检索时最值钱的信号，群友常直接把图上的话
        # 打出来当回复。其次是模型给的、再其次是用户手写的。
        final: list[str] = []
        if text_on_image:
            final.append(text_on_image)
        for tag in list(tags) + [str(t) for t in (merged["tags"] or [])]:
            tag = str(tag).strip()[:12]
            if tag and tag not in final:
                final.append(tag)
        row["tags"] = final[:16]
        if desc:
            row["desc"] = desc
        self.meta[f"{label}/{name}"] = row
        self._mark_meta_dirty()

        return True, f"标了 {len(tags)} 个标签"

    def untagged(self, limit: int = 200) -> list[tuple[str, str]]:
        """还没打标的图，给批量识别用。按分类顺序排，稳定。"""
        idx = self.index()
        out: list[tuple[str, str]] = []
        for cat in self.categories:
            key = cat["key"]
            for name in idx.get(key, []):
                if not self._meta_of(key, name).get("tags"):
                    out.append((key, name))
                    if len(out) >= limit:
                        return out
        return out

    # ── 从 QQ 收藏夹导入 ──────────────────────────────────────────────
    #
    # 协议端（LLBot / NapCat / LLOneBot）都有 fetch_custom_face，
    # 返回收藏表情的图片 URL 列表。URL 走 p.qpic.cn，有防盗链，
    # 不带 Referer 会 403。

    async def fetch_qq_faces(self) -> list[str]:
        """从协议端拉收藏表情的 URL。拿不到就返回空表，不抛异常。"""
        context = getattr(self.plugin, "context", None)
        manager = getattr(context, "platform_manager", None)
        insts = getattr(manager, "platform_insts", None) or []

        for platform in insts:
            bot = getattr(platform, "bot", None)
            if bot is None or not hasattr(bot, "call_action"):
                continue
            try:
                # aiocqhttp 的 call_action 是 async，但个别实现是同步的，
                # 两种都兜住，别因为一个 await 把功能整个弄挂
                pending = bot.call_action("fetch_custom_face")
                if inspect.isawaitable(pending):
                    data = await asyncio.wait_for(pending, timeout=20)
                else:
                    data = pending
            except Exception as e:  # noqa: BLE001 - 协议端不支持就跳过
                logger.debug(f"[群聊记忆] fetch_custom_face 失败: {e}")
                continue

            # aiocqhttp 的 call_action 一般已经把 data 解出来了，
            # 但不同实现有的给整包，两种都兜
            if isinstance(data, dict):
                data = data.get("data")
            if isinstance(data, list):
                return [str(x) for x in data if str(x).startswith("http")]
        return []

    def known_hashes(self) -> set[str]:
        out: set[str] = set()
        for row in self.meta.values():
            if isinstance(row, dict) and row.get("hash"):
                out.add(str(row["hash"]))
        return out

    async def import_qq_faces(
        self, limit: int = 50, category: str = ""
    ) -> dict:
        """把 QQ 收藏表情拉到图库里。

        返回 {fetched, saved, skipped, failed, category}。
        按内容哈希跳过已经有的——收藏夹里重复的图很多。
        """
        urls = await self.fetch_qq_faces()
        if not urls:
            return {"fetched": 0, "saved": 0, "skipped": 0, "failed": 0, "category": ""}

        target = (category or "").strip() or "待整理"
        cat = next((c for c in self.categories if c["name"] == target), None)
        if cat is None:
            ok, info = self.add_category(target, "刚从 QQ 收藏夹同步过来，还没归类；不归类也能用，检索靠标签")
            if not ok:
                return {
                    "fetched": len(urls),
                    "saved": 0,
                    "skipped": 0,
                    "failed": 0,
                    "category": "",
                    "error": info,
                }
            key = info
        else:
            key = cat["key"]

        limit = max(0, min(int(limit or 0), 200))
        todo = urls[:limit] if limit else urls
        seen = self.known_hashes()

        saved = skipped = failed = 0
        try:
            import aiohttp
        except ImportError:
            return {
                "fetched": len(urls),
                "saved": 0,
                "skipped": 0,
                "failed": 0,
                "category": key,
                "error": "缺少 aiohttp",
            }

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://qun.qq.com/",
        }
        timeout = aiohttp.ClientTimeout(total=20)
        sem = asyncio.Semaphore(2)  # 并发 2，别把图床打急

        async with aiohttp.ClientSession(timeout=timeout) as session:

            async def one(url: str) -> None:
                nonlocal saved, skipped, failed
                async with sem:
                    try:
                        async with session.get(url, headers=headers) as resp:
                            if resp.status != 200:
                                failed += 1
                                return
                            blob = await resp.read()
                    except Exception:  # noqa: BLE001
                        failed += 1
                        return

                if not blob or len(blob) > MAX_BYTES:
                    failed += 1
                    return

                digest = hashlib.md5(blob).hexdigest()
                if digest in seen:
                    skipped += 1
                    return
                seen.add(digest)

                ext = ".jpg"
                if blob[:3] == b"GIF":
                    ext = ".gif"
                elif blob[:4] == b"\x89PNG":
                    ext = ".png"
                elif blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
                    ext = ".webp"

                ok, _ = self.add_file(key, f"qqface{ext}", blob)
                if ok:
                    saved += 1
                else:
                    failed += 1

            await asyncio.gather(*(one(u) for u in todo))

        self.invalidate()
        return {
            "fetched": len(urls),
            "saved": saved,
            "skipped": skipped,
            "failed": failed,
            "category": key,
        }

    def usage_note(self) -> str:
        """面板顶部那句人话说明。"""
        n = self.total()
        if n == 0:
            return "图库是空的，先往分类里放几张。"
        used = sum(1 for k, v in self.counts().items() if v > 0)
        return f"{n} 张图 / {used} 个分类有内容，今天发了 {self.today_total()} 次。"

    # ── 统计持久化 ──────────────────────────────────────────────────────

    def _load_stats(self) -> dict:
        if not self.stats_path.exists():
            return {}
        try:
            data = json.loads(self.stats_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError) as e:
            logger.error(f"[群聊记忆] 读取表情包统计失败: {e}")
            return {}

    # ── 图片元数据（标签 / 使用次数）──────────────────────────────────────
    #
    # 一张图一个条目，键是 "分类key/文件名"。标签只在本地检索时用，
    # 不进 prompt——100 张图的标签塞进提示词就是几百 token，
    # 而分类描述那十行已经把「该发哪一类」讲清楚了。

    def _load_meta(self) -> dict:
        if not self.index_path.exists():
            return {}
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            items = data.get("items") if isinstance(data, dict) else None
            return items if isinstance(items, dict) else {}
        except (OSError, json.JSONDecodeError) as e:
            logger.error(f"[群聊记忆] 读取图片元数据失败: {e}")
            return {}

    def _meta_of(self, label: str, name: str) -> dict:
        row = self.meta.get(f"{label}/{name}")
        return row if isinstance(row, dict) else {}

    def meta_of(self, label: str, name: str) -> dict:
        row = self._meta_of(label, name)
        return {
            "tags": [str(x) for x in (row.get("tags") or []) if str(x).strip()],
            "used": int(row.get("used") or 0),
            "last_used": float(row.get("last_used") or 0),
            "disabled": bool(row.get("disabled")),
        }

    def is_enabled(self, label: str, name: str) -> bool:
        """这张图还能不能发。

        停用不删文件：收藏夹里那些截图、用错场合的图，删了就找不回来，
        停用只是让它不参与检索和挑选。
        """
        return not bool(self._meta_of(label, name).get("disabled"))

    def set_enabled(self, label: str, name: str, on: bool) -> tuple[bool, str]:
        if self.category_of(label) is None:
            return False, "没有这个分类"
        if not name or os.path.basename(name) != name:
            return False, "文件名不合法"
        if not (self.root / label / name).is_file():
            return False, "这张图已经没了"

        key = f"{label}/{name}"
        row = dict(self.meta.get(key) or {})
        if on:
            # 启用就把字段删掉，不留一堆 disabled: false
            row.pop("disabled", None)
        else:
            row["disabled"] = True
        self.meta[key] = row
        self._mark_meta_dirty()
        return True, "已启用" if on else "已停用"

    def set_tags(self, label: str, name: str, tags: list) -> tuple[bool, str]:
        """改一张图的标签。空列表等于清空。"""
        if self.category_of(label) is None:
            return False, "没有这个分类"
        if not (self.root / label / name).is_file():
            return False, "这张图已经没了"

        cleaned = []
        for tag in tags or []:
            text = str(tag).strip()[:12]
            if text and text not in cleaned:
                cleaned.append(text)
        if len(cleaned) > 12:
            cleaned = cleaned[:12]

        key = f"{label}/{name}"
        row = dict(self.meta.get(key) or {})
        row["tags"] = cleaned
        self.meta[key] = row
        self._mark_meta_dirty()
        return True, "已保存"

    @staticmethod
    def _tags_from_name(filename: str) -> list[str]:
        """从文件名猜标签。

        只认带分隔符的（「笑死_熊猫头.jpg」）。没有分隔符的一律不猜：
        「没标.jpg」「微信图片_2024.jpg」猜出来都是垃圾标签，
        进了检索库反而稀释精度。
        """
        stem = os.path.splitext(filename or "")[0].strip()
        if not stem:
            return []
        parts = [p for p in re.split(r"[\s_\-.·()（）\[\]]+", stem) if p]
        if len(parts) <= 1:
            # 没有分隔符就不猜。「没标.jpg」「微信图片_2024.jpg」这种名字
            # 猜出来的都是垃圾标签，进了检索库反而稀释精度。
            # 想要标签就走 AI 识别或手动填。
            return []
        # 纯数字（时间戳、编号）不算标签，那是文件名里的噪声
        return [p[:12] for p in parts if 1 < len(p) <= 12 and not p.isdigit()][:6]

    def _mark_meta_dirty(self) -> None:
        self._meta_dirty = True
        self._mark_dirty()

    def _mark_dirty(self) -> None:
        self._dirty = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._save_task is None or self._save_task.done():
            self._save_task = loop.create_task(self._delayed_save())

    async def _delayed_save(self) -> None:
        await asyncio.sleep(5.0)
        await self.flush()

    async def flush(self) -> None:
        if not self._dirty and not self._meta_dirty:
            return
        async with self._lock:
            if self._dirty:
                try:
                    self.stats_path.write_text(
                        json.dumps(self._stats, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    self._dirty = False
                except OSError as e:
                    logger.error(f"[群聊记忆] 保存表情包统计失败: {e}")
            if self._meta_dirty:
                try:
                    self.index_path.write_text(
                        json.dumps(
                            {"version": 1, "items": self.meta},
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                    self._meta_dirty = False
                except OSError as e:
                    logger.error(f"[群聊记忆] 保存图片元数据失败: {e}")

    def snapshot(self) -> dict:
        """面板用：分类、数量、今日统计。"""
        idx = self.index()
        stats = self._today()
        return {
            "enabled": self.enabled(),
            "labels": [
                {
                    "key": cat["key"],
                    "name": cat["name"],
                    "desc": cat["desc"],
                    "keywords": list(cat["keywords"]),
                    "builtin": cat["builtin"],
                    "count": len(idx.get(cat["key"], [])),
                }
                for cat in self.categories
            ],
            "total": sum(len(v) for v in idx.values()),
            "today_total": int(stats.get("total") or 0),
            "today_by_label": stats.get("by_label") or {},
            "keywords": {c["name"]: list(c["keywords"]) for c in self.categories},
            "rule_tokens": self.rule_tokens(),
            "max_categories": MAX_CATEGORIES,
            "note": self.usage_note(),
        }

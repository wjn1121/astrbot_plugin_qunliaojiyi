"""数据层：风格特征、聊天缓冲、学习状态的持久化。

沿用旧版 ``universal.json`` / ``chat_history.json`` 的结构，方便直接导入历史备份：
- ``universal.json``   ``{unified_msg_origin: [trait, ...]}``

特征按来源分两个文件存，互不干扰：
- ``universal.json``      定时学习产出，会被下一轮学习整表替换
- ``manual_traits.json``  面板手动添加，学习流程永远不读取、不写入

分开存的原因：学习是「整表替换」语义，手动加的东西如果混在同一张表里，
下一轮提炼没复述到就会被静默删掉。分文件后两者在物理上就不可能互相覆盖，
上限裁剪、备份导入也各管各的。注入时才合并成一份。

额外还有 ``learn_state.json`` 记录每个会话的学习元信息（上次学习时间、累计次数、
最后一次结果），只供面板展示，不参与注入逻辑，删掉也能自动重建。

``active.json`` 存主动回复的会话状态（最后发言时间、冷却计时等），结构由 active.py
定义，这里只做透传存取。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .types import ChatMessage, LearnState, Metrics, Trait

CATEGORY_STABLE = "stable"
CATEGORY_SITUATIONAL = "situational"

SOURCE_LEARNED = "learned"
SOURCE_MANUAL = "manual"

# ── 特征生命周期（1.4.0）────────────────────────────────────────────────
STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_ARCHIVED = "archived"
STATUS_REJECTED = "rejected"
ALL_STATUSES = (STATUS_PENDING, STATUS_ACTIVE, STATUS_ARCHIVED, STATUS_REJECTED)

MODIFIED_AI = "ai"
MODIFIED_MANUAL = "manual"

# 新条目入库时的原始权重。攒到 ACTIVATE_PROFICIENCY 才进注入，
# 也就是「至少被两个不同轮次观察到」——一次性玩梗升不上来。
NEW_TRAIT_PROFICIENCY = 10
ACTIVATE_PROFICIENCY = 20
INJECT_MIN_PROFICIENCY = 20
# 每次被确认的增益，按「这条出现在多少个不同日期」递减：
# 同一天被刷屏提十次也顶不上去，跨天反复出现的才是真风格。
GAIN_BY_DAYS = (10, 5, 2)

# 时效折扣：不改存储值，排序时按距上次出现的天数打折。
# 好处是不需要后台定期跑衰减任务，也不会因为学习频率变化而改变衰减速度；
# 一旦重新被观察到，last_seen 刷新，权重自动「复活」。
FRESHNESS_DAYS = 30
FRESHNESS_FLOOR = 0.08
ARCHIVE_SCORE = 5.0

# 合并阈值：二元组相似度。「短句碎发一条一发连刷，极少合并成长段」与
# 「短句碎发一条一发，极少合并长段」≈0.61，「互怼调侃随意不破防」与「互怼调侃不破防」≈0.60，
# 都能合并；不相关的两条风格通常 <0.2。
MERGE_SIMILARITY = 0.55
DIFF_JACCARD_WEIGHT = 0.85
EVIDENCE_KEEP = 5
ALIAS_KEEP = 8

# 手动添加的硬性边界：面板会校验，这里再拦一道，防止外部接口绕过
MANUAL_CONTENT_MAX = 100
MANUAL_TRIGGER_MAX = 3
MANUAL_TRIGGER_LEN = 12
# 单会话手动记忆条数上限。它不占 max_stable_traits / max_situational_traits 的名额，
# 但完全不限制的话，一次批量粘贴几百条会把注入 prompt 直接撑爆。
MANUAL_MAX_PER_SESSION = 50
# 手动添加时的默认熟练度：略高于学习产出的起步值 5，让新加的条目在注入里能排上号
DEFAULT_MANUAL_PROFICIENCY = 60

# set_override 需要区分「调用方没传这个参数」和「显式传了 None」：
# search 是 三态开关，None 本身有含义（跟随全局），不能像 learn/inject 那样用它当缺省。
_UNSET = object()

_PUNCT_CHARS = " \t\r\n，。、！？；：“”‘’（）《》【】,.!?;:'\"()[]{}~`…-—"
_PUNCT_RE = re.compile("[" + re.escape(_PUNCT_CHARS) + "]+")


def normalize_text(text: Any) -> str:
    """去掉标点、空白与大小写差异，只留内容用于比较。"""
    return _PUNCT_RE.sub("", str(text or "")).lower()


def _bigrams(text: str) -> set[str]:
    """字符二元组集合。中文不需要分词，二元组足够判断「是不是在说同一件事」。"""
    raw = normalize_text(text)
    if not raw:
        return set()
    if len(raw) == 1:
        return {raw}
    return {raw[i : i + 2] for i in range(len(raw) - 1)}


def text_similarity(a: Any, b: Any) -> float:
    """两条文本的相似度（0~1）。

    单纯用 Jaccard 会漏掉「一句是另一句的压缩/改写」的情况，所以同时算重叠系数
    （交集 / 较短那条的大小），再按 0.85 折算后取较大值——重叠系数不折算的话，
    短文本会把长文本吞掉。
    """
    sa, sb = _bigrams(str(a or "")), _bigrams(str(b or ""))
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    jaccard = inter / len(sa | sb)
    overlap = inter / min(len(sa), len(sb))
    return max(jaccard, overlap * DIFF_JACCARD_WEIGHT)


def _day_of(ts: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return ""


def trait_score(trait: dict, now: float | None = None) -> float:
    """特征当前的**有效权重**：原始权重按距上次出现的天数打折。

    刚出现过的条目几乎不打折，越久越淡，30 天见底。
    钉住的条目不打折（人工指定优先）。
    """
    base = float(trait.get("proficiency", 0) or 0)
    if trait.get("pinned"):
        return base
    last = float(trait.get("last_seen", 0) or 0)
    if last <= 0:
        return base
    days = max(0.0, ((now or time.time()) - last) / 86400.0)
    if days <= 0:
        return base
    ratio = min(days, FRESHNESS_DAYS) / FRESHNESS_DAYS
    factor = 1.0 - ratio * ratio * (1.0 - FRESHNESS_FLOOR)
    return base * max(FRESHNESS_FLOOR, factor)



class Storage:
    """JSON 存储，写盘做 5 秒合并，避免高频小写入。"""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.universal: dict[str, list[Trait]] = self._load("universal.json")
        self.manual: dict[str, list[Trait]] = self._load("manual_traits.json")
        self.chat_history: dict[str, list[ChatMessage]] = self._load("chat_history.json")
        self.learn_state: dict[str, LearnState] = self._load("learn_state.json")
        self.metrics: Metrics = self._load("metrics.json")
        # 会话级开关：{"bot1:GroupMessage:123": {"learn": false, "inject": true}}
        # search 是三态，值可能是 None（跟随全局），所以类型放宽到 bool | None
        self.overrides: dict[str, dict[str, bool | None]] = self._load("overrides.json")
        # 主动回复状态：{"bot1:GroupMessage:123": {...}}，结构由 active.py 定义，
        # 这里只负责存取，不解释内容
        self.active: dict[str, dict] = self._load("active.json")
        # 面板置顶：{"sessions": ["bot1:GroupMessage:123", ...]}，纯展示顺序偏好。
        # 单独一个文件而不是塞进 overrides，因为置顶不影响学习/注入行为，
        # 混进去会让「恢复默认」把用户排好的顺序一起清掉。
        self.pins: dict[str, Any] = self._load("pins.json")

        self._lock = asyncio.Lock()
        self._version = 0
        self._dirty = {
            "universal": False,
            "manual": False,
            "chat_history": False,
            "learn_state": False,
            "metrics": False,
            "overrides": False,
            "active": False,
            "pins": False,
        }
        self._save_task: asyncio.Task | None = None
        self._save_delay = 5.0

    # ── 读写工具 ────────────────────────────────────────────────────────

    def _load(self, filename: str) -> dict:
        path = self.data_dir / filename
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError) as e:
            logger.error(f"[群聊记忆] 加载 {filename} 失败: {e}")
            return {}

    def _mark(self, *keys: str) -> None:
        for key in keys:
            self._dirty[key] = True
        # 同步上下文（如被别处直接调用）没有事件循环，此时只打脏标记，
        # 由显式 flush() 写盘；有循环时才起延迟合并任务。
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._save_task is None or self._save_task.done():
            self._save_task = loop.create_task(self._delayed_save())

    async def _delayed_save(self) -> None:
        await asyncio.sleep(self._save_delay)
        await self.flush()

    async def flush(self) -> None:
        async with self._lock:
            if self._dirty["universal"]:
                await self._write("universal.json", self.universal)
                self._dirty["universal"] = False
            if self._dirty["manual"]:
                await self._write("manual_traits.json", self.manual)
                self._dirty["manual"] = False
            if self._dirty["chat_history"]:
                await self._write("chat_history.json", self.chat_history)
                self._dirty["chat_history"] = False
            if self._dirty["learn_state"]:
                await self._write("learn_state.json", self.learn_state)
                self._dirty["learn_state"] = False
            if self._dirty["metrics"]:
                await self._write("metrics.json", self.metrics)
                self._dirty["metrics"] = False
            if self._dirty["overrides"]:
                await self._write("overrides.json", self.overrides)
                self._dirty["overrides"] = False
            if self._dirty["active"]:
                await self._write("active.json", self.active)
                self._dirty["active"] = False
            if self._dirty["pins"]:
                await self._write("pins.json", self.pins)
                self._dirty["pins"] = False

    async def _write(self, filename: str, data: dict) -> None:
        try:
            (self.data_dir / filename).write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as e:
            logger.error(f"[群聊记忆] 保存 {filename} 失败: {e}")

    # ── 面板置顶（pins.json）───────────────────────────────────────────
    #
    # 只影响面板里的展示顺序：置顶的会话（群号 / QQ 号）排在列表最前面。
    # 不进注入、不进学习、不参与排序权重，所以清数据时也不清它。

    def pinned_sessions(self) -> list[str]:
        """置顶的会话 ID，按用户设定的先后顺序。"""
        raw = self.pins.get("sessions")
        if not isinstance(raw, list):
            return []
        return [str(x) for x in raw if str(x or "").strip()]

    def is_pinned(self, session_id: str) -> bool:
        return session_id in self.pinned_sessions()

    async def set_pin(self, session_id: str, pinned: bool) -> bool:
        """加 / 取消置顶，返回状态是否真的变了。"""
        session_id = (session_id or "").strip()
        if not session_id:
            return False
        current = self.pinned_sessions()
        if pinned:
            if session_id in current:
                return False
            current.append(session_id)
        else:
            if session_id not in current:
                return False
            current = [x for x in current if x != session_id]
        self.pins["sessions"] = current
        self._mark("pins")
        return True

    # ── 会话级开关 ──────────────────────────────────────────────────────

    def session_override(self, session_id: str) -> dict[str, bool | None]:
        """原始覆盖记录。缺键表示走默认值，不在这里补默认。"""
        return self.overrides.get(session_id, {})

    def learn_allowed(self, session_id: str) -> bool:
        return self.session_override(session_id).get("learn", True)

    def inject_allowed(self, session_id: str) -> bool:
        return self.session_override(session_id).get("inject", True)

    def search_allowed(self, session_id: str) -> bool | None:
        """三态：None 跟随全局，True 本群开，False 本群关。"""
        value = self.session_override(session_id).get("search")
        return value if isinstance(value, bool) else None

    async def set_override(
        self,
        session_id: str,
        *,
        learn: bool | None = None,
        inject: bool | None = None,
        search: bool | None = _UNSET,
    ) -> dict[str, bool | None]:
        current = dict(self.overrides.get(session_id, {}))
        if learn is not None:
            current["learn"] = bool(learn)
        if inject is not None:
            current["inject"] = bool(inject)
        # 用哨兵区分「不传」和「传 None」：传 None 是「恢复跟随全局」，要真的写进去
        if search is not _UNSET:
            current["search"] = None if search is None else bool(search)
        if (
            current.get("learn", True)
            and current.get("inject", True)
            and current.get("search") is None
        ):
            # 三项都在默认值时才不留冗余记录
            self.overrides.pop(session_id, None)
        else:
            self.overrides[session_id] = current
        self._mark("overrides")
        # 返回落库后的真实状态：全部恢复默认时这里是空 dict
        return self.overrides.get(session_id, {})

    async def clear_override(self, session_id: str) -> bool:
        removed = self.overrides.pop(session_id, None) is not None
        if removed:
            self._mark("overrides")
        return removed

    # ── 主动回复状态（active.json）─────────────────────────────────────

    def active_of(self, session_id: str) -> dict:
        """读某个群的主动回复状态，没有则返回空 dict。"""
        return self.active.get(session_id, {})

    def save_active(self, session_id: str, state: dict) -> None:
        """写入主动回复状态并打脏标记。state 是完整替换。"""
        self.active[session_id] = state
        # 这里不动 _version：主动状态只影响调度的节流判断，跟注入缓存无关，
        # 每次都失效会让 trait 缓存白重建。
        self._mark("active")

    def clear_active(self, session_id: str) -> bool:
        """清除某个群的主动回复状态，返回是否真的删除了东西。"""
        removed = self.active.pop(session_id, None) is not None
        if removed:
            self._mark("active")
        return removed

    # ── 风格特征 ────────────────────────────────────────────────────────

    def get_version(self) -> int:
        """数据版本号，特征变更时递增，供注入缓存失效。"""
        return self._version

    def traits_of(self, session_id: str, include_manual: bool = True) -> list[Trait]:
        """会话的生效特征。默认合并手动记忆，注入与学习 prompt 都读这里。

        手动条目返回的是加了 ``source`` 标记的副本，调用方据此区分来源；
        要改数据请走 update_trait / delete_trait，它们会定位到真实所在的那张表。
        """
        learned = self.universal.get(session_id, [])
        if not include_manual:
            return learned
        manual = self.manual.get(session_id)
        if not manual:
            return learned
        return learned + [{**t, "source": SOURCE_MANUAL} for t in manual]

    def manual_of(self, session_id: str) -> list[Trait]:
        """只看面板手动添加的记忆（带 source 标记副本）。"""
        return [{**t, "source": SOURCE_MANUAL} for t in self.manual.get(session_id, [])]

    @staticmethod
    def _partition(traits: list[Trait]) -> tuple[list[Trait], list[Trait]]:
        stable = [t for t in traits if t.get("category", CATEGORY_STABLE) == CATEGORY_STABLE]
        situational = [t for t in traits if t.get("category") == CATEGORY_SITUATIONAL]
        return stable, situational

    def split(self, session_id: str, include_manual: bool = True) -> tuple[list[Trait], list[Trait]]:
        """注入用的特征：只含已生效且权重过线的，按钉住 → 有效权重排序。

        面板要展示「库里的全部条目」请用 learned_split / traits_of，
        这里刻意只给能进 prompt 的那部分，避免待审和已归档的条目被注入。
        """
        return self._partition(self.active_traits_of(session_id, include_manual=include_manual))

    def learned_split(self, session_id: str) -> tuple[list[Trait], list[Trait]]:
        """只看学习产出的特征，面板分组展示用。"""
        return self._partition(self.universal.get(session_id, []))

    def other_stable_except(self, session_id: str) -> list[Trait]:
        """跨群参考用的稳定风格。

        只取学习产出：手动记忆是针对具体群的人工指定，跨群扩散会串味，
        比如给 A 群加的黑话被注入到 B 群，属于明显的负收益。
        """
        out: list[Trait] = []
        for sid, traits in self.universal.items():
            if sid == session_id:
                continue
            out.extend(t for t in traits if t.get("category", CATEGORY_STABLE) == CATEGORY_STABLE)
        return out

    def replace_traits(
        self,
        session_id: str,
        stable: list[str],
        situational: list[dict[str, Any]] | None = None,
        *,
        limits: tuple[int, int] | None = None,
    ) -> None:
        """用新提炼结果替换旧特征，同时保留同名特征的熟练度累积。

        limits=(稳定上限, 场景上限)，超出的按熟练度从低到高淘汰；
        这样长期运行也不会让特征越攒越多、把学习 prompt 撑大。

        注意这里**只动 universal.json**：手动添加的记忆在另一个文件里，
        学习流程读不到也写不到，天然不会被覆盖。如果提炼结果里出现与手动记忆
        同名的内容，直接丢弃——以人工指定为准，避免同一条内容在面板和注入里出现两次。
        """
        now = time.time()
        old = {t.get("content"): t for t in self.universal.get(session_id, [])}
        manual_contents = {t.get("content") for t in self.manual.get(session_id, [])}

        def make(
            content: str, category: str, context: str = "", triggers: list[str] | None = None
        ) -> Trait:
            prev = old.get(content, {})
            return {
                "content": content,
                "category": category,
                "context": context,
                "triggers": list(triggers or prev.get("triggers") or []),
                "proficiency": min(100, int(prev.get("proficiency", 0) or 0) + 5),
                "confirmed_rounds": int(prev.get("confirmed_rounds", 0) or 0) + 1,
                "last_seen": now,
                "last_updated": now,
            }

        traits = [make(c, CATEGORY_STABLE) for c in stable if c.strip()]
        for item in situational or []:
            content = str(item.get("content") or "").strip()
            if content:
                traits.append(
                    make(
                        content,
                        CATEGORY_SITUATIONAL,
                        str(item.get("context") or "").strip(),
                        item.get("triggers"),
                    )
                )
        if manual_contents:
            traits = [t for t in traits if t.get("content") not in manual_contents]
        if traits:
            self.universal[session_id] = traits
        else:
            self.universal.pop(session_id, None)
        self._version += 1
        if limits:
            self.trim_traits(session_id, limits)
        self._mark("universal")

    def trim_traits(self, session_id: str, limits: tuple[int, int]) -> int:
        """按类别裁剪学习特征数量，返回被删除的条数。

        只裁 universal。手动记忆不占 max_stable_traits / max_situational_traits 的名额，
        由 MANUAL_MAX_PER_SESSION 单独约束，否则手工加几条就会把学到的一路挤掉。
        """
        traits = self.universal.get(session_id, [])
        if not traits:
            return 0
        stable_max, sit_max = max(0, limits[0]), max(0, limits[1])
        stable = [t for t in traits if t.get("category", CATEGORY_STABLE) == CATEGORY_STABLE]
        situational = [t for t in traits if t.get("category") == CATEGORY_SITUATIONAL]

        def top(items: list[dict], n: int) -> list[dict]:
            if n <= 0:
                return []
            return sorted(items, key=lambda t: t.get("proficiency", 0), reverse=True)[:n]

        kept = top(stable, stable_max) + top(situational, sit_max)
        dropped = len(traits) - len(kept)
        if dropped > 0:
            self.universal[session_id] = kept
            self._version += 1
            self._mark("universal")
        return dropped

    # ── 累积链路（1.4.0）───────────────────────────────────────────────
    #
    # 旧链路 replace_traits 是「本轮 LLM 输出即整张表」，本轮没提到的旧条目会消失。
    # 新链路改成 upsert：候选要么合并进已有条目（累加权重），要么作为新条目入库，
    # 已有条目不会因为「这轮没被提到」而消失，只在权重见底后归档。

    @staticmethod
    def _match_trait(bucket: list[dict], content: str, context: str) -> dict | None:
        """在库里找这条候选对应的已有特征，找不到返回 None。

        两级匹配：先比归一化后的完整文本，再用二元组相似度兜措辞漂移。
        场景标签也要对得上，否则「同一句话在不同场景」会被错误合并。
        """
        norm_content = normalize_text(content)
        norm_context = normalize_text(context)
        best: tuple[float, dict] | None = None
        for trait in bucket:
            if trait.get("status") == STATUS_REJECTED:
                continue
            if normalize_text(trait.get("content")) == norm_content and (
                normalize_text(trait.get("context")) == norm_context
            ):
                return trait
            score = text_similarity(content, trait.get("content"))
            if score < MERGE_SIMILARITY:
                continue
            # 场景也要合得上：都为空，或有一边为空，或彼此相似
            ctx_score = text_similarity(context, trait.get("context"))
            if norm_context and normalize_text(trait.get("context")) and ctx_score < MERGE_SIMILARITY:
                continue
            if best is None or score > best[0]:
                best = (score, trait)
        return best[1] if best else None

    @staticmethod
    def _norm_evidence(raw: Any, now: float) -> list[dict]:
        """证据统一成 ``{text, at, sender}``。学习链路给字符串，导入/接口可能给字典。"""
        out: list[dict] = []
        items = raw if isinstance(raw, list) else ([raw] if raw else [])
        for item in items:
            if isinstance(item, dict):
                text = str(item.get("text") or "").strip()
                try:
                    at = float(item.get("at") or now)
                except (TypeError, ValueError):
                    at = now
                sender = str(item.get("sender") or "")
            else:
                text = str(item or "").strip()
                at, sender = now, ""
            if text:
                out.append({"text": text[:120], "at": at, "sender": sender[:24]})
        return out

    def upsert_traits(
        self,
        session_id: str,
        candidates: list[dict],
        *,
        limits: tuple[int, int] | None = None,
        max_total: int = 300,
        activate_at: int = ACTIVATE_PROFICIENCY,
        ladder: tuple[int, ...] = GAIN_BY_DAYS,
    ) -> dict[str, int]:
        """把本轮候选合并进库，返回统计。

        候选结构：``{content, context, triggers, dimension, evidence, category}``。
        合并时**主措辞保持首次入库的写法**，新说法只进 alias —— 这样注入文本不会
        这周「连刷」下周「连发」，人设描述始终稳定。
        """
        now = time.time()
        bucket = self.universal.setdefault(session_id, [])
        stats = {"added": 0, "merged": 0, "activated": 0}
        today = _day_of(now)

        for cand in candidates:
            content = str(cand.get("content") or "").strip()
            if not content:
                continue
            context = str(cand.get("context") or "").strip()
            category = (
                CATEGORY_SITUATIONAL if context else str(cand.get("category") or CATEGORY_STABLE)
            )
            if category not in (CATEGORY_STABLE, CATEGORY_SITUATIONAL):
                category = CATEGORY_STABLE
            evidence = self._norm_evidence(cand.get("evidence"), now)

            hit = self._match_trait(bucket, content, context)
            if hit is None:
                trait: dict[str, Any] = {
                    "content": content,
                    "category": category,
                    "context": context,
                    "triggers": list(cand.get("triggers") or [])[:3],
                    "proficiency": NEW_TRAIT_PROFICIENCY,
                    "confirmed_rounds": 1,
                    "seen": 1,
                    "alias": [],
                    "evidence": list(evidence)[:EVIDENCE_KEEP],
                    "status": STATUS_PENDING,
                    "checked_by": "",
                    "modified_by": MODIFIED_AI,
                    "pinned": False,
                    "dimension": str(cand.get("dimension") or "").strip(),
                    "first_seen": now,
                    "last_seen": now,
                    "last_updated": now,
                }
                bucket.append(trait)
                stats["added"] += 1
                continue

            # 合并：用证据覆盖的日期数决定这次涨多少
            days = {_day_of(ev.get("at")) for ev in hit.get("evidence") or [] if ev.get("at")}
            days.add(today)
            days.discard("")
            step = ladder[min(max(len(days) - 1, 0), len(ladder) - 1)]
            hit["proficiency"] = min(100, int(hit.get("proficiency", 0) or 0) + step)
            hit["confirmed_rounds"] = int(hit.get("confirmed_rounds", 0) or 0) + 1
            hit["seen"] = int(hit.get("seen", 1) or 1) + 1
            hit["last_seen"] = now
            hit["last_updated"] = now
            if not hit.get("first_seen"):
                hit["first_seen"] = hit["last_seen"]
            if content != hit.get("content"):
                alias = hit.setdefault("alias", [])
                if content not in alias:
                    alias.append(content)
                    del alias[:-ALIAS_KEEP]
            if evidence:
                merged_evidence = list(hit.get("evidence") or []) + list(evidence)
                hit["evidence"] = merged_evidence[-EVIDENCE_KEEP:]
            if not hit.get("triggers") and cand.get("triggers"):
                hit["triggers"] = list(cand.get("triggers") or [])[:3]
            if hit.get("status") == STATUS_PENDING and hit["proficiency"] >= activate_at:
                hit["status"] = STATUS_ACTIVE
                stats["activated"] += 1
            stats["merged"] += 1

        # 存量数据（迁移后 status=active）第一次被新版确认时也补齐结构
        self._version += 1
        self._mark("universal")
        if limits:
            self.trim_traits(session_id, limits)
        self.cap_total(session_id, max_total)
        return stats

    def cap_total(self, session_id: str, max_total: int, *, keep_active: bool = True) -> int:
        """库里条数超上限时，从有效权重最低的开始归档。返回归档条数。"""
        if max_total <= 0:
            return 0
        bucket = self.universal.get(session_id) or []
        if len(bucket) <= max_total:
            return 0
        now = time.time()
        # 钉住的排最前（永不淘汰），其余按有效权重降序
        ordered = sorted(
            bucket,
            key=lambda t: (1 if t.get("pinned") else 0, trait_score(t, now)),
            reverse=True,
        )
        keep, drop = ordered[:max_total], ordered[max_total:]
        if not drop:
            return 0
        for trait in drop:
            trait["status"] = STATUS_ARCHIVED if keep_active else trait.get("status", STATUS_ACTIVE)
        self.universal[session_id] = keep + drop
        self._version += 1
        self._mark("universal")
        return len(drop)

    def archive_stale(self, session_id: str, *, now: float | None = None) -> int:
        """把权重见底的生效特征转归档。归档不删数据，面板可恢复。"""
        bucket = self.universal.get(session_id) or []
        moved = 0
        for trait in bucket:
            if trait.get("status") != STATUS_ACTIVE or trait.get("pinned"):
                continue
            if trait_score(trait, now) < ARCHIVE_SCORE:
                trait["status"] = STATUS_ARCHIVED
                moved += 1
        if moved:
            self._version += 1
            self._mark("universal")
        return moved

    def active_traits_of(self, session_id: str, include_manual: bool = True) -> list[Trait]:
        """参与注入的特征：已生效且有效权重过线，按钉住 → 有效权重排序。"""
        now = time.time()
        # 留 0.5 的容差：权重刚好等于门槛的条目，几小时的时效折扣就会算出
        # 19.9998 这种数，不该因为浮点噪声把它判出局。
        threshold = INJECT_MIN_PROFICIENCY - 0.5
        learned = [
            t
            for t in self.universal.get(session_id, [])
            if t.get("status", STATUS_ACTIVE) == STATUS_ACTIVE
            and trait_score(t, now) >= threshold
        ]
        learned.sort(key=lambda t: (1 if t.get("pinned") else 0, trait_score(t, now)), reverse=True)
        if not include_manual:
            return learned
        manual = self.manual.get(session_id) or []
        # 手动记忆不看时效，人工指定的一直有效
        return learned + [{**t, "source": SOURCE_MANUAL} for t in manual]

    def set_trait_status(
        self,
        session_id: str,
        content: str,
        status: str,
        *,
        source: str | None = SOURCE_LEARNED,
    ) -> bool:
        """人工审核：批准 / 否决 / 归档 / 恢复。改过之后自动自检不再覆盖。"""
        if status not in ALL_STATUSES:
            return False
        for table, _key in self._table_pairs(source):
            for trait in table.get(session_id, []):
                if trait.get("content") != content:
                    continue
                trait["status"] = status
                trait["modified_by"] = MODIFIED_MANUAL
                trait["checked_by"] = MODIFIED_MANUAL
                trait["last_updated"] = time.time()
                if status == STATUS_ACTIVE:
                    # 人工批准即视为可信，权重至少抬到生效线，否则下一轮又掉回待审
                    trait["proficiency"] = max(
                        int(trait.get("proficiency", 0) or 0), ACTIVATE_PROFICIENCY
                    )
                    trait["last_seen"] = trait["last_updated"]
                self._version += 1
                self._mark("manual" if table is self.manual else "universal")
                return True
        return False

    def set_pinned(self, session_id: str, content: str, pinned: bool) -> bool:
        for trait in self.universal.get(session_id, []):
            if trait.get("content") != content:
                continue
            trait["pinned"] = bool(pinned)
            trait["modified_by"] = MODIFIED_MANUAL
            trait["last_updated"] = time.time()
            self._version += 1
            self._mark("universal")
            return True
        return False

    def pending_traits(self, session_id: str | None = None) -> list[dict]:
        """待审条目，供面板「学习库」和管理员命令使用。"""
        out: list[dict] = []
        sessions = [session_id] if session_id else list(self.universal.keys())
        for sid in sessions:
            for trait in self.universal.get(sid or "", []):
                if trait.get("status") == STATUS_PENDING:
                    out.append({**trait, "session_id": sid})
        return out

    def trait_stats(self, session_id: str) -> dict[str, int]:
        """按状态统计条数，面板顶部的角标用。"""
        counts = {status: 0 for status in ALL_STATUSES}
        for trait in self.universal.get(session_id, []):
            status = trait.get("status", STATUS_ACTIVE)
            if status in counts:
                counts[status] += 1
        counts["manual"] = len(self.manual.get(session_id) or [])
        counts["total"] = len(self.universal.get(session_id) or [])
        return counts

    def migrate_traits(self) -> int:
        """把 1.3.x 的旧特征补齐 1.4.0 字段，返回改动的条目数。

        旧数据没有生命周期概念，一律按「已生效」处理，不重新走审核，
        否则升级当天所有记忆会集体失效。proficiency 沿用原值（0-100），
        直接当作原始权重使用，不需要换算。
        """
        changed = 0
        now = time.time()
        for table, _key in ((self.universal, "universal"), (self.manual, "manual")):
            for _sid, traits in table.items():
                for trait in traits:
                    touched = False
                    if "status" not in trait:
                        trait["status"] = STATUS_ACTIVE
                        touched = True
                    if "seen" not in trait:
                        trait["seen"] = max(1, int(trait.get("confirmed_rounds", 1) or 1))
                        touched = True
                    if "alias" not in trait:
                        trait["alias"] = []
                        touched = True
                    if "evidence" not in trait:
                        trait["evidence"] = []
                        touched = True
                    if "pinned" not in trait:
                        # 手动记忆一律钉住：人工指定的不该被时效折扣吃掉
                        trait["pinned"] = table is self.manual
                        touched = True
                    if "modified_by" not in trait:
                        trait["modified_by"] = MODIFIED_MANUAL if table is self.manual else MODIFIED_AI
                        touched = True
                    if "first_seen" not in trait:
                        trait["first_seen"] = float(trait.get("last_seen", now) or now)
                        touched = True
                    if "dimension" not in trait:
                        trait["dimension"] = ""
                        touched = True
                    # 存量条目在旧链路里是「已生效」的，不能因为新门槛（有效权重 ≥20）
                    # 突然集体失效，这里统一抬到门槛之上留 5 分余量，避免几小时的
                    # 时效折扣就把它们挤出注入线。新建条目仍从 10 起步，
                    # 要靠第二轮观察才能进注入。
                    if (
                        trait.get("status") == STATUS_ACTIVE
                        and int(trait.get("proficiency", 0) or 0) < INJECT_MIN_PROFICIENCY + 5
                    ):
                        trait["proficiency"] = INJECT_MIN_PROFICIENCY + 5
                        touched = True
                    if touched:
                        changed += 1
                        self._mark("manual" if table is self.manual else "universal")
        if changed:
            self._version += 1
            logger.info(f"[群聊记忆] 特征结构已迁移到 1.4.0 格式，共 {changed} 条")
        return changed

    # ── 手动记忆（独立文件，学习流程不读不写）──────────────────────────

    def manual_count(self, session_id: str) -> int:
        return len(self.manual.get(session_id, []))

    @staticmethod
    def clamp_proficiency(value: Any, default: int = DEFAULT_MANUAL_PROFICIENCY) -> int:
        """熟练度归一化到 0-100 的整数，非法值回退到默认值。"""
        try:
            number = int(round(float(value)))
        except (TypeError, ValueError):
            return default
        return max(0, min(100, number))

    @staticmethod
    def clean_triggers(raw: Any) -> list[str]:
        """规范化触发词：去空、去超长、去重，最多 3 个。"""
        out: list[str] = []
        for item in raw if isinstance(raw, list) else []:
            text = str(item or "").strip()
            if text and text not in out and len(text) <= MANUAL_TRIGGER_LEN:
                out.append(text)
            if len(out) >= MANUAL_TRIGGER_MAX:
                break
        return out

    def _table_pairs(self, source: str | None) -> list[tuple[dict, str]]:
        """按来源定位要操作的表；不指定时两表都找，手动优先（人工指定更权威）。"""
        if source == SOURCE_MANUAL:
            return [(self.manual, "manual")]
        if source == SOURCE_LEARNED:
            return [(self.universal, "universal")]
        return [(self.manual, "manual"), (self.universal, "universal")]

    def has_content(self, session_id: str, content: str) -> bool:
        """同名内容跨表查重。

        update_trait / delete_trait 都按内容定位，允许两张表存在同名条目会让
        「到底改的是哪一条」出现歧义，所以从头就禁止。
        """
        return any(
            t.get("content") == content
            for t in self.manual.get(session_id, []) + self.universal.get(session_id, [])
        )

    async def add_manual_trait(
        self,
        session_id: str,
        content: str,
        *,
        category: str = CATEGORY_STABLE,
        context: str = "",
        triggers: Any = None,
        proficiency: Any = None,
    ) -> tuple[Trait | None, str]:
        """往手动记忆表里加一条，返回 (条目, 错误原因)。

        和自动学习完全隔离：不参与 replace_traits、不占 max_*_traits 名额、
        不会被 trim_traits 淘汰。上限由 MANUAL_MAX_PER_SESSION 单独约束。
        """
        session_id = str(session_id or "").strip()
        if not session_id:
            return None, "会话 ID 不能为空"
        text = str(content or "").strip()
        if not text:
            return None, "内容不能为空"
        if len(text) > MANUAL_CONTENT_MAX:
            return None, f"内容最多 {MANUAL_CONTENT_MAX} 字"
        if category not in (CATEGORY_STABLE, CATEGORY_SITUATIONAL):
            return None, "类型只能是 stable 或 situational"

        bucket = self.manual.setdefault(session_id, [])
        if len(bucket) >= MANUAL_MAX_PER_SESSION:
            return None, f"该会话手动记忆已达上限 {MANUAL_MAX_PER_SESSION} 条"
        if self.has_content(session_id, text):
            return None, "该会话已有相同内容"

        now = time.time()
        trait: Trait = {
            "content": text,
            "category": category,
            "context": str(context or "").strip(),
            "triggers": self.clean_triggers(triggers),
            # confirmed_rounds 保持 0：它是学习确认次数的计数器，手动添加不该伪造它
            "confirmed_rounds": 0,
            "proficiency": self.clamp_proficiency(proficiency),
            "last_seen": now,
            "last_updated": now,
        }
        bucket.append(trait)
        self._version += 1
        self._mark("manual")
        return {**trait, "source": SOURCE_MANUAL}, ""

    async def delete_trait(
        self, session_id: str, content: str, source: str | None = None
    ) -> bool:
        """按内容删除。source 指定在哪张表删，不指定就两表都删。"""
        removed = False
        for table, key in self._table_pairs(source):
            traits = table.get(session_id, [])
            remaining = [t for t in traits if t.get("content") != content]
            if len(remaining) == len(traits):
                continue
            if remaining:
                table[session_id] = remaining
            else:
                table.pop(session_id, None)
            self._version += 1
            self._mark(key)
            removed = True
        return removed

    async def update_trait(
        self,
        session_id: str,
        old_content: str,
        *,
        new_content: str | None = None,
        context: str | None = None,
        triggers: list[str] | None = None,
        proficiency: Any = None,
        category: str | None = None,
        source: str | None = None,
    ) -> tuple[Trait | None, str]:
        """按内容定位并修改一条特征，先找手动表再找学习表。

        改名或改类型时若与同会话其他特征重名则拒绝；返回 (条目, 错误原因)。
        """
        target: Trait | None = None
        hit_key = ""
        for table, key in self._table_pairs(source):
            found = next(
                (t for t in table.get(session_id, []) if t.get("content") == old_content), None
            )
            if found is not None:
                target = found
                hit_key = key
                break
        if target is None:
            return None, "未找到该特征"

        if new_content is not None:
            renamed = str(new_content).strip()
            if not renamed:
                return None, "内容不能为空"
            if len(renamed) > MANUAL_CONTENT_MAX:
                return None, f"内容最多 {MANUAL_CONTENT_MAX} 字"
            if renamed != old_content and self.has_content(session_id, renamed):
                return None, "该会话已有相同内容"
            target["content"] = renamed
        if category is not None:
            if category not in (CATEGORY_STABLE, CATEGORY_SITUATIONAL):
                return None, "类型只能是 stable 或 situational"
            target["category"] = category
        if context is not None:
            target["context"] = str(context).strip()
        if triggers is not None:
            target["triggers"] = self.clean_triggers(triggers)
        if proficiency is not None:
            target["proficiency"] = self.clamp_proficiency(
                proficiency, int(target.get("proficiency", 0) or 0)
            )
        target["last_updated"] = time.time()

        self._version += 1
        self._mark(hit_key)
        return {**target, "source": SOURCE_MANUAL if hit_key == "manual" else SOURCE_LEARNED}, ""

    async def clear_session(self, session_id: str, scope: str = "all") -> bool:
        """清空会话。scope 控制清哪部分：learned / manual / history / active / all。

        分开清是必要的：手动记忆删掉不可恢复，不该被「清空学习产出」顺手带走。
        待学习消息（history）也是独立的一项：清掉学习产出后往往想留着消息重新学，
        所以 learned 不再连带清 chat_history。
        """
        if scope not in ("learned", "manual", "history", "active", "all"):
            logger.warning(f"[群聊记忆] clear_session 收到未知 scope: {scope}")
            return False

        changed = False
        if scope in ("all", "learned"):
            if self.universal.pop(session_id, None) is not None:
                changed = True
            if self.learn_state.pop(session_id, None) is not None:
                changed = True
            self._mark("universal", "learn_state")
            self._version += 1
        if scope in ("all", "manual"):
            if self.manual.pop(session_id, None) is not None:
                changed = True
            self._mark("manual")
            self._version += 1
        if scope in ("all", "history"):
            if self.chat_history.pop(session_id, None) is not None:
                changed = True
            self._mark("chat_history")
        if scope in ("all", "active"):
            if self.active.pop(session_id, None) is not None:
                changed = True
            self._mark("active")
        return changed

    async def clear_all(self, scope: str = "all") -> dict[str, int]:
        """清空所有会话，返回各部分被清掉的会话数。"""
        result = {"learned": 0, "manual": 0, "history": 0, "active": 0}
        if scope not in ("learned", "manual", "history", "active", "all"):
            logger.warning(f"[群聊记忆] clear_all 收到未知 scope: {scope}")
            return result
        if scope in ("all", "learned"):
            result["learned"] = len(self.universal)
            self.universal.clear()
            self.learn_state.clear()
            self._mark("universal", "learn_state")
            self._version += 1
        if scope in ("all", "manual"):
            result["manual"] = len(self.manual)
            self.manual.clear()
            self._mark("manual")
            self._version += 1
        if scope in ("all", "history"):
            result["history"] = len(self.chat_history)
            self.chat_history.clear()
            self._mark("chat_history")
        if scope in ("all", "active"):
            result["active"] = len(self.active)
            self.active.clear()
            self._mark("active")
        return result

    # ── 聊天缓冲 ────────────────────────────────────────────────────────

    def add_message(self, session_id: str, message: ChatMessage, limit: int) -> None:
        bucket = self.chat_history.setdefault(session_id, [])
        bucket.append(message)
        if limit > 0 and len(bucket) > limit:
            del bucket[: len(bucket) - limit]
        self._mark("chat_history")

    def history_of(self, session_id: str, limit: int = 50) -> list[ChatMessage]:
        return self.chat_history.get(session_id, [])[-limit:]

    def history_counts(self) -> dict[str, int]:
        return {sid: len(msgs) for sid, msgs in self.chat_history.items() if msgs}

    async def clear_history(self, session_id: str) -> None:
        self.chat_history.pop(session_id, None)
        self._mark("chat_history")

    # ── 学习状态 ────────────────────────────────────────────────────────

    def record_learn(
        self,
        session_id: str,
        *,
        ok: bool,
        stable: int = 0,
        situational: int = 0,
        elapsed: float = 0.0,
        message: str = "",
        message_count: int = 0,
        chars: int = 0,
    ) -> None:
        prev = self.learn_state.get(session_id, {})
        now = time.time()
        # 保留最近 20 次学习记录，供面板做时间线（只用于展示，不参与逻辑）
        history = list(prev.get("history") or [])
        history.append(
            {
                "at": now,
                "ok": ok,
                "stable": stable,
                "situational": situational,
                "messages": message_count,
                "chars": chars,
                "elapsed": round(elapsed, 2),
                "message": message[:120],
            }
        )
        self.learn_state[session_id] = {
            "last_learn_at": now,
            "last_ok": ok,
            "last_stable": stable,
            "last_situational": situational,
            "last_elapsed": round(elapsed, 2),
            "last_message": message[:300],
            "last_message_count": message_count,
            "last_chars": chars,
            "total_learns": int(prev.get("total_learns", 0)) + (1 if ok else 0),
            "fail_count": int(prev.get("fail_count", 0)) + (0 if ok else 1),
            "history": history[-20:],
        }

        # 全局累计：只用于面板展示「这套机制一共花了多少」，不参与任何逻辑
        m = self.metrics
        m["learn_calls"] = int(m.get("learn_calls", 0)) + 1
        m["learn_ok"] = int(m.get("learn_ok", 0)) + (1 if ok else 0)
        m["learn_fail"] = int(m.get("learn_fail", 0)) + (0 if ok else 1)
        m["messages_analyzed"] = int(m.get("messages_analyzed", 0)) + message_count
        m["chars_analyzed"] = int(m.get("chars_analyzed", 0)) + chars
        m["elapsed_total"] = round(float(m.get("elapsed_total", 0.0)) + elapsed, 1)
        self._mark("learn_state", "metrics")

    # ── 备份导入 ────────────────────────────────────────────────────────

    @staticmethod
    def _clean_traits(traits: Any) -> list[Trait]:
        """把备份里的原始列表规范成 Trait 列表，脏数据直接丢。"""
        clean: list[Trait] = []
        for t in traits if isinstance(traits, list) else []:
            if not isinstance(t, dict):
                continue
            content = str(t.get("content") or "").strip()
            if not content:
                continue
            triggers = t.get("triggers")
            category = t.get("category", CATEGORY_STABLE)
            clean.append(
                {
                    "content": content,
                    "category": (
                        category if category in (CATEGORY_STABLE, CATEGORY_SITUATIONAL) else CATEGORY_STABLE
                    ),
                    "context": str(t.get("context") or ""),
                    "triggers": [str(x) for x in triggers] if isinstance(triggers, list) else [],
                    "proficiency": max(0, min(100, int(t.get("proficiency", 0) or 0))),
                    "confirmed_rounds": int(t.get("confirmed_rounds", 0) or 0),
                    "last_seen": float(t.get("last_seen", time.time()) or time.time()),
                    "last_updated": float(t.get("last_updated", time.time()) or time.time()),
                }
            )
        return clean

    def import_backup(
        self,
        universal: dict,
        chat_history: dict | None,
        mode: str = "merge",
        limits: tuple[int, int] | None = None,
        manual: dict | None = None,
    ) -> dict:
        """从备份导入。mode=merge 合并同名特征，mode=replace 直接覆盖。

        manual 是 1.2.0 起的手动记忆表；老备份没有 manual_traits.json，
        传 None 即可，行为跟以前一致。
        """
        imported_sessions = 0
        imported_traits = 0

        # 先导手动记忆：后面导学习特征时要拿它做去重基准
        imported_manual = 0
        for session_id, traits in (manual or {}).items():
            clean = self._clean_traits(traits)
            if not clean:
                continue
            bucket = self.manual.setdefault(session_id, [])
            existing = {t.get("content") for t in bucket}
            for t in clean:
                if t["content"] in existing or len(bucket) >= MANUAL_MAX_PER_SESSION:
                    continue
                bucket.append(t)
                existing.add(t["content"])
                imported_manual += 1

        for session_id, traits in (universal or {}).items():
            clean = self._clean_traits(traits)
            if not clean:
                continue
            if mode == "merge":
                existing = self.universal.get(session_id, [])
                by_content = {t["content"]: t for t in existing}
                for t in clean:
                    prev = by_content.get(t["content"])
                    if prev:
                        prev["proficiency"] = max(
                            int(prev.get("proficiency", 0)), t["proficiency"]
                        )
                        prev["confirmed_rounds"] = max(
                            int(prev.get("confirmed_rounds", 0)), t["confirmed_rounds"]
                        )
                    else:
                        existing.append(t)
                        by_content[t["content"]] = t
                self.universal[session_id] = existing
            else:
                self.universal[session_id] = clean
            imported_sessions += 1
            imported_traits += len(clean)

        if chat_history:
            for session_id, msgs in chat_history.items():
                if isinstance(msgs, list) and msgs:
                    self.chat_history.setdefault(session_id, []).extend(
                        m for m in msgs if isinstance(m, dict)
                    )

        # 两表同名时以手动为准，学习那条丢掉，否则面板和注入里会出现重复内容
        for session_id, traits in self.manual.items():
            names = {t.get("content") for t in traits}
            learned = self.universal.get(session_id)
            if not learned or not names:
                continue
            kept = [t for t in learned if t.get("content") not in names]
            if kept:
                self.universal[session_id] = kept
            else:
                self.universal.pop(session_id, None)

        if limits:
            for sid in (universal or {}):
                self.trim_traits(sid, limits)

        self._version += 1
        self._mark("universal", "chat_history", "manual")
        return {
            "sessions": imported_sessions,
            "traits": imported_traits,
            "manual": imported_manual,
        }

    # ── 统计 ────────────────────────────────────────────────────────────

    @staticmethod
    def _count_by_category(mapping: dict[str, list[Trait]]) -> tuple[int, int]:
        stable = situational = 0
        for traits in mapping.values():
            for t in traits:
                if t.get("category") == CATEGORY_SITUATIONAL:
                    situational += 1
                else:
                    stable += 1
        return stable, situational

    def stats(self) -> dict[str, Any]:
        learned_stable, learned_sit = self._count_by_category(self.universal)
        manual_stable, manual_sit = self._count_by_category(self.manual)
        learned_total = learned_stable + learned_sit
        manual_total = manual_stable + manual_sit
        return {
            "sessions": len(set(self.universal) | set(self.manual)),
            "traits": learned_total + manual_total,
            "learned_traits": learned_total,
            "manual_traits": manual_total,
            "stable": learned_stable + manual_stable,
            "situational": learned_sit + manual_sit,
            "manual_sessions": len(self.manual),
            "active_sessions": len(self.active),
            "pending_messages": sum(len(v) for v in self.chat_history.values()),
            "metrics": {
                "learn_calls": int(self.metrics.get("learn_calls", 0)),
                "learn_ok": int(self.metrics.get("learn_ok", 0)),
                "learn_fail": int(self.metrics.get("learn_fail", 0)),
                "messages_analyzed": int(self.metrics.get("messages_analyzed", 0)),
                "chars_analyzed": int(self.metrics.get("chars_analyzed", 0)),
                "elapsed_total": float(self.metrics.get("elapsed_total", 0.0)),
            },
        }

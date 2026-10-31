"""消耗统计：token 用量、人民币花费与「每群每日搜索配额」。

这个模块是「今日消耗」面板与搜索限流的**唯一数据来源**，被两条路径同时读写：
- 回复链路上的 search 工具：调用 :meth:`record_search` 占配额；
- 事后的钩子：调用 :meth:`record` 补记 token。

设计要点：
- **按天一个文件**，``YYYY-MM-DD.json``，放在 AstrBot data 顶层的 ``qunliao_token/``，
  而不是插件目录里 —— 换插件版本、重装插件都不会把历史账单带走。
- 只存数字，不存任何消息内容，文件被看到也不会泄露聊天。
- 内存累计 + 30 秒防抖落盘（与 ``storage.py`` 一致），异常一律吞掉并记日志：
  统计坏了最多是面板数字难看，绝不能影响正常回复。

关于金额有两种口径，别混用：
- 日文件里的 ``cost`` 字段：**每次 record 时按当时所处的峰谷价累加**，是这一天的
  精确账单。全天的 token 是分时段产生的，跨过高峰边界的调用各按各的价算。
- :meth:`UsageTracker.cost_of`：拿一整天混合后的 token 总量，按**此刻**的价格粗算，
  只适合「如果现在都按当前价算」这种对照展示，和历史账单不一定相等。
"""

from __future__ import annotations

import asyncio
import copy
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypedDict

from astrbot.api import logger

# 计价一律按北京时间判定峰谷。容器时区是 UTC，绝不能依赖 time.localtime()。
BEIJING_TZ = timezone(timedelta(hours=8))

# 高峰时段（北京时间，周一至周五）：09:00-12:00、14:00-18:00
PEAK_RANGES = ((9, 12), (14, 18))

# 会产生 token 的来源；search 只计次数不计 token（搜索本身由服务商计费）
TOKEN_SOURCES = ("learn", "judge", "bot")
ALL_SOURCES = ("learn", "judge", "bot", "search")

DAY_FILE_FORMAT = "%Y-%m-%d"
SAVE_DELAY = 30.0
# 单次 snapshot 最多回看的天数，防止面板传个离谱的数字把磁盘扫一遍
SNAPSHOT_MAX_DAYS = 60


class TokenCounts(TypedDict):
    """一份 token 三段式计数。"""

    input_other: int
    input_cached: int
    output: int


class SourceCounts(TypedDict, total=False):
    """按来源的计数。search 只有 calls，其余三项是 token。"""

    calls: int
    input_other: int
    input_cached: int
    output: int


class SessionCounts(TypedDict, total=False):
    """按会话的计数。search_calls 是每日搜索配额的唯一依据。"""

    calls: int
    search_calls: int
    input_other: int
    input_cached: int
    output: int


class DayDoc(TypedDict, total=False):
    """单个日文件的完整结构。"""

    date: str
    calls: int
    search_calls: int
    cost: float
    tokens: TokenCounts
    by_source: dict[str, SourceCounts]
    by_session: dict[str, SessionCounts]


# ── 纯工具函数（不依赖实例状态，方便单测）──────────────────────────────────


def now_beijing() -> datetime:
    """当前北京时间。全模块唯一的取时入口。"""
    return datetime.now(BEIJING_TZ)


def today_key() -> str:
    """今天的日期键，形如 ``2026-10-31``。"""
    return now_beijing().strftime(DAY_FILE_FORMAT)


def _empty_tokens() -> TokenCounts:
    return {"input_other": 0, "input_cached": 0, "output": 0}


def _empty_source(name: str) -> SourceCounts:
    bucket: SourceCounts = {"calls": 0}
    if name != "search":
        bucket.update(_empty_tokens())  # type: ignore[typeddict-item]
    return bucket


def _empty_session() -> SessionCounts:
    return {
        "calls": 0,
        "search_calls": 0,
        "input_other": 0,
        "input_cached": 0,
        "output": 0,
    }


def _empty_doc(day: str) -> DayDoc:
    return {
        "date": day,
        "calls": 0,
        "search_calls": 0,
        "cost": 0.0,
        "tokens": _empty_tokens(),
        "by_source": {name: _empty_source(name) for name in ALL_SOURCES},
        "by_session": {},
    }


def _as_int(value: Any) -> int:
    """任何东西转非负整数，失败给 0 —— 统计字段不值得为类型问题抛异常。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _as_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0


def _raw_field(usage: Any, name: str) -> Any:
    """从 usage 对象里取一个字段，同时兼容属性、dict 和 .get() 三种形态。"""
    if isinstance(usage, dict):
        return usage.get(name)
    value = getattr(usage, name, None)
    if value is not None:
        return value
    getter = getattr(usage, "get", None)
    if callable(getter):
        try:
            return getter(name)
        except Exception:
            return None
    return None


def _pick(usage: Any, names: tuple[str, ...]) -> int | None:
    """按顺序找第一个存在的字段名，返回非负整数；全都没有返回 None。"""
    for name in names:
        value = _raw_field(usage, name)
        if value is None:
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        return number if number > 0 else 0
    return None


def extract_tokens(usage: Any) -> tuple[int, int, int]:
    """把各种形态的 usage 归一成 ``(input_other, input_cached, output)``。

    容错三件事：usage 为 None、字段缺失、字段是字符串或负数。
    另外兼容 AstrBot 原生 usage 的字段名；如果上游只给了输入总量，
    就按「输入总量减去命中缓存的量」倒推未命中部分。
    """
    if usage is None:
        return 0, 0, 0

    cached = _pick(usage, ("input_cached", "prompt_cache_hit_tokens", "cached_tokens"))
    other = _pick(usage, ("input_other", "prompt_cache_miss_tokens"))
    output = _pick(usage, ("output", "completion_tokens", "output_tokens"))

    if other is None:
        total_input = _pick(usage, ("prompt_tokens", "input_tokens"))
        if total_input is not None:
            other = max(0, total_input - (cached or 0))

    return other or 0, cached or 0, output or 0


def _ensure_writable(path: Path) -> bool:
    """真写一个探针文件来判断可写（Windows 上 os.access 不可靠）。"""
    probe = path / ".write_probe"
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe.write_text("", encoding="utf-8")
    except OSError:
        return False
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
    return True


def resolve_dir(plugin_data_dir: str | Path) -> Path:
    """决定统计文件放哪。

    首选 AstrBot data 顶层的 ``qunliao_token/``（插件目录外的相邻位置），
    取不到或不可写就退回插件自己的 ``token_stats/``。任何异常都不许冒出去，
    最坏情况返回一个可能写不进去的路径，由写盘处记日志。
    """
    try:
        plugin_dir = Path(plugin_data_dir)
    except (TypeError, ValueError):
        plugin_dir = Path(".")
    try:
        candidates = [plugin_dir.parent.parent / "qunliao_token", plugin_dir / "token_stats"]
    except Exception:
        candidates = [plugin_dir / "token_stats"]

    for candidate in candidates:
        if _ensure_writable(candidate):
            return candidate
    return candidates[-1]


def _prune_dir(directory: Path, keep_days: int, day: str) -> None:
    """删掉超过保留天数的日文件。只认 ``YYYY-MM-DD.json``，别的文件一概不动。"""
    try:
        cutoff = datetime.strptime(day, DAY_FILE_FORMAT).date() - timedelta(days=keep_days - 1)
    except ValueError:
        return
    try:
        files = list(directory.glob("*.json"))
    except OSError:
        return
    for file in files:
        try:
            file_day = datetime.strptime(file.stem, DAY_FILE_FORMAT).date()
        except ValueError:
            continue
        if file_day >= cutoff:
            continue
        try:
            file.unlink(missing_ok=True)
        except OSError as e:
            logger.warning(f"[群聊记忆] 删除过期消耗文件 {file.name} 失败: {e}")


class UsageTracker:
    """今日消耗统计 + 每会话每日搜索配额。

    线程安全说明：计数走 ``threading.RLock``（record/record_search 可能在事件循环
    线程里被同步调用），写盘在锁外做，靠版本号判断这期间有没有新数据。
    精度按「够看」来设计，不做银行级账目。
    """

    def __init__(self, data_dir: Path, settings) -> None:
        self._settings = settings
        self.dir = resolve_dir(data_dir)

        self._lock = threading.RLock()
        self._flush_lock = asyncio.Lock()
        self._save_task: asyncio.Task | None = None
        self._version = 0
        self._dirty = False

        self._day = today_key()
        self._data: DayDoc = self._load_day(self._day)
        # 已经清理过旧文件的日期；跨天后置空，让新一天首次写盘时再清一遍
        self._pruned_day: str | None = None

        try:
            _prune_dir(self.dir, self._keep_days(), self._day)
            self._pruned_day = self._day
        except Exception as e:  # 清理失败只是磁盘多留几天文件
            logger.warning(f"[群聊记忆] 清理过期消耗文件失败: {e}")

    # ── 配置读取 ────────────────────────────────────────────────────────

    def _cfg(self, key: str, default: Any) -> Any:
        """读配置。settings 未来可能换实现，这里对任何异常都回退默认值。"""
        try:
            value = self._settings.get(key)
        except Exception:
            return default
        return default if value is None else value

    def _cfg_float(self, key: str, default: float) -> float:
        try:
            return float(self._cfg(key, default))
        except (TypeError, ValueError):
            return default

    def _keep_days(self) -> int:
        try:
            days = int(self._cfg("token_stats_keep_days", 7))
        except (TypeError, ValueError):
            days = 7
        return days if days >= 1 else 1

    def peak_multiplier(self, at: datetime | None = None) -> float:
        """给定时刻的峰谷倍率。关闭峰谷计价或不在高峰时段时返回 1.0。"""
        if not bool(self._cfg("token_price_peak_enabled", True)):
            return 1.0
        moment = at or now_beijing()
        if moment.weekday() >= 5:  # 周六周日全天空闲价
            return 1.0
        hour = moment.hour
        if not any(start <= hour < end for start, end in PEAK_RANGES):
            return 1.0
        multiplier = self._cfg_float("token_price_peak_multiplier", 2.0)
        return multiplier if multiplier > 0 else 1.0

    def _tokens_cost(self, input_other: int, input_cached: int, output: int, multiplier: float) -> float:
        """按当前配置牌价折算金额（元）。倍率由调用方决定用哪个时段。"""
        price_cached = self._cfg_float("token_price_input_cached", 0.05)
        price_miss = self._cfg_float("token_price_input_miss", 1.5)
        price_output = self._cfg_float("token_price_output", 4.5)
        amount = (
            input_cached / 1e6 * price_cached
            + input_other / 1e6 * price_miss
            + output / 1e6 * price_output
        )
        return amount * multiplier

    # ── 日文件读写 ──────────────────────────────────────────────────────

    def _day_path(self, day: str) -> Path:
        return self.dir / f"{day}.json"

    def _normalize(self, raw: Any, day: str) -> DayDoc:
        """把磁盘上的任意内容整理成完整结构，缺字段补 0，脏数据不抛异常。"""
        doc = _empty_doc(day)
        if not isinstance(raw, dict):
            return doc

        doc["calls"] = _as_int(raw.get("calls"))
        doc["search_calls"] = _as_int(raw.get("search_calls"))

        raw_tokens = raw.get("tokens")
        if isinstance(raw_tokens, dict):
            doc["tokens"] = {
                "input_other": _as_int(raw_tokens.get("input_other")),
                "input_cached": _as_int(raw_tokens.get("input_cached")),
                "output": _as_int(raw_tokens.get("output")),
            }

        raw_sources = raw.get("by_source")
        if isinstance(raw_sources, dict):
            for name, counts in raw_sources.items():
                if not isinstance(name, str) or not isinstance(counts, dict):
                    continue
                bucket = _empty_source(name)
                bucket["calls"] = _as_int(counts.get("calls"))
                if name != "search":
                    for field in ("input_other", "input_cached", "output"):
                        bucket[field] = _as_int(counts.get(field))  # type: ignore[literal-required]
                doc["by_source"][name] = bucket

        raw_sessions = raw.get("by_session")
        if isinstance(raw_sessions, dict):
            for session_id, counts in raw_sessions.items():
                if not isinstance(session_id, str) or not isinstance(counts, dict):
                    continue
                session = _empty_session()
                for field in ("calls", "search_calls", "input_other", "input_cached", "output"):
                    session[field] = _as_int(counts.get(field))  # type: ignore[literal-required]
                doc["by_session"][session_id] = session

        # 老文件或手改文件可能没有 cost；按此刻牌价粗算一个，仅供展示
        if "cost" in raw:
            doc["cost"] = _as_float(raw.get("cost"))
        else:
            doc["cost"] = self.cost_of(doc)
        return doc

    def _load_day(self, day: str) -> DayDoc:
        path = self._day_path(day)
        if not path.exists():
            return _empty_doc(day)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.error(f"[群聊记忆] 加载消耗统计 {path.name} 失败: {e}")
            return _empty_doc(day)
        return self._normalize(raw, day)

    def _serialize(self, doc: DayDoc) -> dict:
        """落盘前的最后整形：金额固定 4 位小数。"""
        payload = copy.deepcopy(dict(doc))
        payload["cost"] = round(_as_float(payload.get("cost")), 4)
        return payload

    def _write(self, day: str, doc: DayDoc) -> bool:
        """同步写一个日文件。失败只记日志，返回 False 让调用方保留脏标记。"""
        path = self._day_path(day)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self._serialize(doc), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return True
        except (OSError, TypeError, ValueError) as e:
            logger.error(f"[群聊记忆] 保存消耗统计 {path.name} 失败: {e}")
            return False

    def _mark_dirty(self) -> None:
        """打脏标记并排一次 30 秒后的合并落盘。需在持锁状态下调用。"""
        self._dirty = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 同步上下文没有事件循环，等显式 flush()
            return
        if self._save_task is None or self._save_task.done():
            self._save_task = loop.create_task(self._delayed_save())

    async def _delayed_save(self) -> None:
        await asyncio.sleep(SAVE_DELAY)
        await self.flush()

    def _ensure_day_locked(self) -> None:
        """跨天时把旧的一天写盘并换到新文件。需在持锁状态下调用。"""
        day = today_key()
        if day == self._day:
            return
        if self._dirty:
            # 同步短写：跨天发生在上报路径上，不能等异步任务
            if self._write(self._day, self._data):
                self._dirty = False
        self._day = day
        self._data = self._load_day(day)
        self._pruned_day = None  # 新一天首次落盘时顺手清理旧文件

    # ── 记录 ────────────────────────────────────────────────────────────

    def record(self, source: str, usage, session_id: str = "") -> None:
        """记一次 LLM 调用的 token。

        ``source`` 取 ``learn`` / ``judge`` / ``bot``；认不出来的来源一律并入 bot，
        免得面板上冒出乱七八糟的键。``usage`` 允许是 None 或缺字段 —— 这种情况下
        仍然计一次调用数，只是 token 记 0。
        """
        try:
            input_other, input_cached, output = extract_tokens(usage)
            # 金额在下单这一刻就地折算：跨过高峰边界的两笔调用各按各的价，
            # 比事后再拿总量乘一个倍率准得多。
            cost = self._tokens_cost(
                input_other, input_cached, output, self.peak_multiplier()
            )
            name = source if source in TOKEN_SOURCES else "bot"

            with self._lock:
                self._ensure_day_locked()
                data = self._data
                data["calls"] = int(data.get("calls") or 0) + 1
                tokens = data["tokens"]
                tokens["input_other"] += input_other
                tokens["input_cached"] += input_cached
                tokens["output"] += output
                data["cost"] = float(data.get("cost") or 0.0) + cost

                bucket = data["by_source"].get(name)
                if bucket is None:
                    bucket = _empty_source(name)
                    data["by_source"][name] = bucket
                bucket["calls"] = int(bucket.get("calls") or 0) + 1
                if name != "search":
                    bucket["input_other"] = int(bucket.get("input_other") or 0) + input_other
                    bucket["input_cached"] = int(bucket.get("input_cached") or 0) + input_cached
                    bucket["output"] = int(bucket.get("output") or 0) + output

                sid = str(session_id or "")
                if sid:
                    session = data["by_session"].get(sid)
                    if session is None:
                        session = _empty_session()
                        data["by_session"][sid] = session
                    session["calls"] = int(session.get("calls") or 0) + 1
                    session["input_other"] = int(session.get("input_other") or 0) + input_other
                    session["input_cached"] = int(session.get("input_cached") or 0) + input_cached
                    session["output"] = int(session.get("output") or 0) + output

                self._version += 1
                self._mark_dirty()
        except Exception as e:  # 统计坏了不能影响正常回复
            logger.error(f"[群聊记忆] 记录 token 消耗失败: {e}")

    def record_search(self, session_id: str) -> int:
        """记一次搜索调用，返回该会话今日累计次数（含本次）。

        返回值直接决定配额：调用方通常先问 :meth:`search_quota_left`，
        再用这里的返回值做「本次是否被允许」的后置校验。
        """
        sid = str(session_id or "")
        try:
            with self._lock:
                self._ensure_day_locked()
                data = self._data
                data["search_calls"] = int(data.get("search_calls") or 0) + 1
                bucket = data["by_source"].get("search")
                if bucket is None:
                    bucket = _empty_source("search")
                    data["by_source"]["search"] = bucket
                bucket["calls"] = int(bucket.get("calls") or 0) + 1

                session = data["by_session"].get(sid)
                if session is None:
                    session = _empty_session()
                    data["by_session"][sid] = session
                used = int(session.get("search_calls") or 0) + 1
                session["search_calls"] = used

                self._version += 1
                self._mark_dirty()
                return used
        except Exception as e:
            logger.error(f"[群聊记忆] 记录搜索调用失败: {e}")
            return 0

    # ── 查询 ────────────────────────────────────────────────────────────

    def search_used(self, session_id: str) -> int:
        """该会话今日已用搜索次数。"""
        sid = str(session_id or "")
        try:
            with self._lock:
                self._ensure_day_locked()
                session = self._data["by_session"].get(sid) or {}
                return _as_int(session.get("search_calls"))
        except Exception as e:
            logger.error(f"[群聊记忆] 读取搜索次数失败: {e}")
            return 0

    def search_quota_left(self, session_id: str) -> int:
        """该会话今日剩余搜索次数，不会小于 0。"""
        try:
            limit = int(self._cfg("web_search_daily_limit", 20))
        except (TypeError, ValueError):
            limit = 20
        return max(0, limit - self.search_used(session_id))

    def today(self) -> dict:
        """今日整份数据（面板展示用），返回副本，外部随便改。"""
        with self._lock:
            self._ensure_day_locked()
            doc = copy.deepcopy(dict(self._data))
        doc["cost"] = round(_as_float(doc.get("cost")), 4)
        return doc

    def snapshot(self, days: int = 7) -> list[dict]:
        """近 N 天序列，按日期升序，缺失的日期补 0，方便前端直接画折线。"""
        try:
            count = int(days)
        except (TypeError, ValueError):
            count = 7
        count = max(1, min(count, SNAPSHOT_MAX_DAYS))

        with self._lock:
            self._ensure_day_locked()
            current_key = self._day
            current_doc = copy.deepcopy(dict(self._data))

        try:
            anchor = datetime.strptime(current_key, DAY_FILE_FORMAT).date()
        except ValueError:
            anchor = now_beijing().date()

        series: list[dict] = []
        for offset in range(count - 1, -1, -1):
            day = (anchor - timedelta(days=offset)).strftime(DAY_FILE_FORMAT)
            doc = current_doc if day == current_key else self._load_day(day)
            tokens = doc.get("tokens") or _empty_tokens()
            series.append(
                {
                    "date": day,
                    # 这里用的是逐笔累加的真实账单，不是 cost_of 的估算
                    "cost": round(_as_float(doc.get("cost")), 4),
                    "tokens": dict(tokens),
                    "search_calls": _as_int(doc.get("search_calls")),
                    "calls": _as_int(doc.get("calls")),
                }
            )
        return series

    def cost_of(self, day: dict) -> float:
        """把某一天的 token 总量按**此刻**的峰谷价粗算成人民币。

        与日文件里 ``cost`` 字段的区别：``cost`` 是每次调用时就地按当时时段累加的
        真实账单；这里是把一整天混在一起的 token 乘上当前倍率的估算，只用于
        「按现价算会是多少」的对照展示。
        """
        try:
            tokens = (day or {}).get("tokens") or {}
            input_other = _as_int(tokens.get("input_other"))
            input_cached = _as_int(tokens.get("input_cached"))
            output = _as_int(tokens.get("output"))
            return round(
                self._tokens_cost(input_other, input_cached, output, self.peak_multiplier()),
                6,
            )
        except Exception:
            return 0.0

    def summary(self) -> dict:
        """给 api.py 用的汇总视图。"""
        doc = self.today()
        used_by_session = {
            sid: _as_int(counts.get("search_calls"))
            for sid, counts in (doc.get("by_session") or {}).items()
        }
        return {
            "today": {
                "cost": round(_as_float(doc.get("cost")), 4),
                "tokens": dict(doc.get("tokens") or _empty_tokens()),
                "search_calls": _as_int(doc.get("search_calls")),
                "calls": _as_int(doc.get("calls")),
            },
            "days": self.snapshot(),
            "search_used_by_session": used_by_session,
        }

    # ── 落盘 ────────────────────────────────────────────────────────────

    async def flush(self) -> None:
        """强制写盘。写失败保留脏标记，下次再试。"""
        async with self._flush_lock:
            with self._lock:
                need_prune = self._pruned_day != self._day
                if not self._dirty and not need_prune:
                    return
                day = self._day
                doc = copy.deepcopy(dict(self._data))
                version = self._version
                dirty = self._dirty
                keep_days = self._keep_days()

            if dirty:
                ok = await asyncio.to_thread(self._write, day, doc)  # type: ignore[arg-type]
                if ok:
                    with self._lock:
                        # 写盘期间又有新数据进来时不清脏标记，留给下一轮
                        if self._version == version:
                            self._dirty = False

            if need_prune:
                await asyncio.to_thread(self._prune_safe, day, keep_days)

    def _prune_safe(self, day: str, keep_days: int) -> None:
        """清理过期文件，任何异常都不外抛。"""
        try:
            _prune_dir(self.dir, keep_days, day)
        except Exception as e:
            logger.warning(f"[群聊记忆] 清理过期消耗文件失败: {e}")
            return
        with self._lock:
            # 清理的是「截至 day 这一天」的，跨天后会自动重置
            self._pruned_day = day

"""配置读取层。

设计要点：
- AstrBot 的 ``AstrBotConfig`` 在 WebUI 里改配置后是实时可读的，所以这里**不缓存**配置值，
  每次 ``get`` 都现读，天然支持热更新。
- 面板上允许热调的几个运行参数写进 ``runtime.json``，优先级高于插件配置；
  这样调试时不必为了改一个阈值去重启整个 AstrBot，也不会污染正式配置。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from astrbot.api import logger

# 代码里的兜底默认值。schema 缺失、老配置迁移、字段被删时都能安全回退。
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "inject_enabled": True,
    "analysis_interval_seconds": 21600,
    "min_history_for_analysis": 10,
    "max_messages_per_learn": 80,
    "max_stable_inject": 5,
    # 手动记忆的注入名额，和 max_stable_inject 各算各的：
    # 否则手工加满 5 条后，6 小时一次的学习成果一条都进不了注入。
    "max_manual_inject": 3,
    "enable_situational_inject": True,
    "max_situational_inject": 2,
    "situational_similarity_threshold": 0.4,
    "enable_cross_group": False,
    "max_global_styles": 3,
    "enable_emb_selection": True,
    "embedding_provider_id": "",
    "analysis_provider_id": "",
    "history_limit_per_session": 200,
    # 场景化表达匹配：先按触发词命中，再退回包含判断，最后才考虑向量
    "enable_trigger_matching": True,
    # 向量请求跑在回复链路上，给个硬超时，避免 embedding 服务慢时拖住回复
    "embedding_timeout_seconds": 2.0,
    # 特征数量上限：防止长期运行后 prompt 跟着特征一起膨胀
    "max_stable_traits": 40,
    "max_situational_traits": 20,
    "session_whitelist": [],
    "session_blacklist": [],
    "blocked_users": [],
    "debug_mode": False,
    "startup_learn_delay_seconds": 600,
    # ── 风格强度与人格让位 ──────────────────────────────────────────────
    # 0 = 完全不注入风格（纯人格）；100 = 全力体现。分档见 injector.scale_quota
    "style_strength": 100,
    # 主动插话默认压到 60 —— 默认阈值 100 下，主动回复永远不会推翻人格
    "active_style_strength": 60,
    # 强度【严格大于】这个值才允许让位。默认 100 配合默认强度 100 等于「开箱不让位」，
    # 保持与 1.2.0 一致：满额注入风格，但冲突时人格优先。想让风格赢就把它调低。
    "style_override_threshold": 100,
    # 让位时把声明同时追加到 system_prompt。效果更硬，但该轮 DeepSeek 前缀缓存必失效
    "style_override_deep": False,
    # ── 回复字数上限 ────────────────────────────────────────────────────
    # 0 = 不限制。被动默认 0 是为了不改变 1.2.0 的现有行为
    "reply_max_chars": 0,
    "active_reply_max_chars": 80,
    # 硬截断默认关：会切坏 markdown、链接和表情占位符，比啰嗦更难看
    "reply_max_chars_hard": False,
    # ── 主动回复 ────────────────────────────────────────────────────────
    "active_reply_enabled": False,
    # probability / rule / llm_judge；后两者更容易触发，靠冷却和日限压制
    "active_reply_mode": "probability",
    "active_reply_chance": 0.15,
    "active_reply_cooldown_seconds": 1800,
    "active_reply_daily_limit": 8,
    "active_reply_quiet_hours": "01:00-08:00",
    "active_reply_min_messages": 3,
    "active_reply_judge_stack": 8,
    "active_reply_backoff_limit": 3,
    "active_reply_keywords": [],
    "active_reply_guidance": (
        "这是一条由你主动发起的群聊回复，没有人向你提问。\n"
        "不要使用「有什么可以帮你」「我在」「已收到」这类服务式或召唤式开头；\n"
        "不要提及自己是主动发言或任何机制；\n"
        "像普通群友一样简短接话；话题不适合接就少说或不说。"
    ),
    "active_reply_provider_id": "",
    # ── 联网搜索 ────────────────────────────────────────────────────────
    # inherit = 完全交给 AstrBot（插件不计数、不限额）；plugin = 插件挂工具并限流；off = 全摘
    "web_search_mode": "plugin",
    "web_search_provider": "deepseek",
    "web_search_api_key": "",
    # 插件 key 为空时自动读 AstrBot 全局同家 key，让 key 只存一处
    "web_search_reuse_global_key": True,
    "web_search_max_results": 5,
    "web_search_timeout": 8,
    # 开了每条结果带整页正文，一次可能几万 token，成本翻十倍
    "web_search_need_content": False,
    # 每群每天的搜索次数上限，跨群独立计算，按日期天然归零
    "web_search_daily_limit": 20,
    "web_search_max_per_turn": 2,
    "web_search_hint": "",
    # ── 管理员 ──────────────────────────────────────────────────────────
    "admin_extra_users": [],
    "admin_tool_enabled": False,
    # ── 计价（元 / 百万 token，DeepSeek 官方牌价）──────────────────────
    "token_price_input_cached": 0.05,
    "token_price_input_miss": 1.5,
    "token_price_output": 4.5,
    "token_price_peak_multiplier": 2.0,
    "token_price_peak_enabled": True,
    "token_stats_keep_days": 7,
}

# 允许在面板热调、且会写进 runtime.json 的字段。
RUNTIME_KEYS = {
    "enabled",
    "inject_enabled",
    "analysis_interval_seconds",
    "min_history_for_analysis",
    "max_messages_per_learn",
    "max_stable_inject",
    "max_manual_inject",
    "enable_situational_inject",
    "max_situational_inject",
    "situational_similarity_threshold",
    "enable_cross_group",
    "max_global_styles",
    "enable_trigger_matching",
    "embedding_timeout_seconds",
    "max_stable_traits",
    "max_situational_traits",
    "debug_mode",
    # 风格强度与人格让位
    "style_strength",
    "active_style_strength",
    "style_override_threshold",
    "style_override_deep",
    # 字数上限
    "reply_max_chars",
    "active_reply_max_chars",
    "reply_max_chars_hard",
    # 主动回复
    "active_reply_enabled",
    "active_reply_mode",
    "active_reply_chance",
    "active_reply_cooldown_seconds",
    "active_reply_daily_limit",
    "active_reply_quiet_hours",
    "active_reply_min_messages",
    "active_reply_judge_stack",
    "active_reply_backoff_limit",
    "active_reply_keywords",
    "active_reply_guidance",
    "active_reply_provider_id",
    # 联网搜索
    "web_search_mode",
    "web_search_provider",
    "web_search_api_key",
    "web_search_reuse_global_key",
    "web_search_max_results",
    "web_search_timeout",
    "web_search_need_content",
    "web_search_daily_limit",
    "web_search_max_per_turn",
    "web_search_hint",
    # 管理员
    "admin_extra_users",
    "admin_tool_enabled",
    # 计价
    "token_price_input_cached",
    "token_price_input_miss",
    "token_price_output",
    "token_price_peak_multiplier",
    "token_price_peak_enabled",
    "token_stats_keep_days",
}

_BOOL_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, bool)}
_INT_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, int) and not isinstance(v, bool)}
_FLOAT_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, float)}
_LIST_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, list)}
_STR_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, str)}


class Settings:
    """配置读取 + 运行期覆盖。"""

    def __init__(self, config: Any, runtime_path: Path):
        self._config = config
        self._runtime_path = Path(runtime_path)
        self._runtime: dict[str, Any] = self._load_runtime()

    # ── 运行期覆盖 ──────────────────────────────────────────────────────

    def _load_runtime(self) -> dict[str, Any]:
        try:
            if self._runtime_path.exists():
                data = json.loads(self._runtime_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return {k: v for k, v in data.items() if k in RUNTIME_KEYS}
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"[群聊记忆] 读取 runtime.json 失败，忽略: {e}")
        return {}

    async def set_runtime(self, key: str, value: Any) -> Any:
        """热调一个参数，返回规范化后的值。"""
        if key not in RUNTIME_KEYS:
            raise KeyError(f"不支持热调的配置项: {key}")
        value = self._coerce(key, value)
        self._runtime[key] = value
        try:
            self._runtime_path.parent.mkdir(parents=True, exist_ok=True)
            self._runtime_path.write_text(
                json.dumps(self._runtime, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            logger.error(f"[群聊记忆] 保存 runtime.json 失败: {e}")
        return value

    def clear_runtime(self) -> None:
        self._runtime.clear()
        try:
            self._runtime_path.unlink(missing_ok=True)
        except OSError:
            pass

    # ── 读取 ────────────────────────────────────────────────────────────

    def get(self, key: str) -> Any:
        if key in self._runtime:
            return self._runtime[key]
        return self._coerce(key, self._raw_config(key))

    def _raw_config(self, key: str) -> Any:
        try:
            # AstrBotConfig 是 dict 子类，get 可能返回 None 而不是默认值
            value = self._config.get(key)
        except Exception:
            value = None
        return DEFAULTS[key] if value is None else value

    def _coerce(self, key: str, value: Any) -> Any:
        default = DEFAULTS.get(key)
        try:
            if key in _BOOL_KEYS:
                if isinstance(value, str):
                    return value.strip().lower() in {"1", "true", "yes", "on", "是"}
                return bool(value)
            if key in _INT_KEYS:
                return max(0, int(value))
            if key in _FLOAT_KEYS:
                return float(value)
            if key in _LIST_KEYS:
                if isinstance(value, str):
                    return [x.strip() for x in value.split(",") if x.strip()]
                return list(value or [])
            if key in _STR_KEYS:
                return "" if value is None else str(value)
        except (TypeError, ValueError):
            return default
        return value

    def snapshot(self) -> dict[str, Any]:
        """当前全部生效配置（面板展示用）。"""
        data = {k: self.get(k) for k in DEFAULTS}
        data["_runtime_overrides"] = {k: v for k, v in self._runtime.items()}
        return data

    def is_debug(self) -> bool:
        return bool(self.get("debug_mode"))

"""联网搜索工具：把搜索工具挂到 LLM 请求上，并做每群每日配额。

设计要点：
- 工具名与 AstrBot 内置同名，靠 ``ToolSet.add_tool`` 的同名覆盖语义接管实现；
  插件模式关掉时把内置工具摘掉，两种模式不会同时挂两份。
- 配额两层：单轮上限挡住「一次请求里反复搜」，每日上限挡住「一个群整天刷」。
  只有真正拿到非空结果才扣次数，请求失败不冤枉用户。
- 所有网络异常都在这里消化成空列表，工具永远返回 ``ToolExecResult`` 而不是抛异常，
  否则一次超时会把整轮回复打断。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import aiohttp

from astrbot.api import logger
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

try:  # 新版本 AstrBot 直接给出内置工具名表，优先用它的
    from astrbot.core.tools.web_search_tools import (
        WEB_SEARCH_TOOL_NAMES as BUILTIN_SEARCH_TOOL_NAMES,
    )
except Exception:  # noqa: BLE001 - 旧版本没有这个模块，退回手抄的常量
    BUILTIN_SEARCH_TOOL_NAMES = [
        "web_search_baidu",
        "web_search_tavily",
        "tavily_extract_web_page",
        "web_search_bocha",
        "web_search_brave",
        "web_search_firecrawl",
        "firecrawl_extract_web_page",
        "web_search_exa",
        "exa_get_contents",
        "web_search_anysearch",
        "web_search_serpapi",
        "image_search_serpapi",
        "finance_search_serpapi",
        "scholar_search_serpapi",
        "web_search_deepseek",
        "deepseek_fetch_web_page",
    ]

PROVIDERS = ("deepseek", "tavily", "bocha", "brave")

# 插件挂的工具名必须和内置一致，才能覆盖掉内置实现
PROVIDER_TOOL_NAMES = {
    "deepseek": "web_search_deepseek",
    "tavily": "web_search_tavily",
    "bocha": "web_search_bocha",
    "brave": "web_search_brave",
}

# AstrBot 全局配置里各家的 key 字段，值是 list（WebUI 里可能填了多行）
GLOBAL_KEY_FIELDS = {
    "deepseek": "websearch_deepseek_key",
    "tavily": "websearch_tavily_key",
    "bocha": "websearch_bocha_key",
    "brave": "websearch_brave_key",
}

DEEPSEEK_URL = "https://api.deepseek.com/v1/tools/search"
TAVILY_URL = "https://api.tavily.com/search"
BOCHA_URL = "https://api.bochaai.com/v1/web-search"
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"

# 单条结果的正文/摘要长度上限：不截断的话 5 条结果就能吃掉几千 token
SNIPPET_MAX = 1200
CONTENT_MAX = 2000

DAILY_LIMIT_TEXT = (
    "今日联网搜索次数已用完（{used}/{limit}）。请直接依据已有知识回答；"
    "如果这确实是需要最新信息的问题，明确告诉对方「这个我查不了最新的」。"
)
TURN_LIMIT_TEXT = "本轮搜索次数已达上限，请直接用已有结果或已有知识回答。"
EMPTY_TEXT = "没有搜到相关结果，请换一个关键词或直接依据已有知识回答。"
EMPTY_QUERY_TEXT = "搜索关键词为空，请补充一个 query 再搜。"

_DESCRIPTIONS = {
    "deepseek": (
        "Search the web using DeepSeek and return results with source references. "
        "Only use this when the answer needs real-time or verifiable information."
    ),
    "tavily": (
        "Search the web using Tavily and return results with source references. "
        "Only use this when the answer needs real-time or verifiable information."
    ),
    "bocha": (
        "Search the web using Bocha and return results with source references. "
        "Only use this when the answer needs real-time or verifiable information."
    ),
    "brave": (
        "Search the web using Brave and return results with source references. "
        "Only use this when the answer needs real-time or verifiable information."
    ),
}

_SEARCH_PARAMETERS = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "description": "The search query, in the same language as the user's question.",
        }
    },
    "required": ["query"],
}


# ── key 解析 ────────────────────────────────────────────────────────────


def resolve_key(plugin, provider: str) -> str:
    """插件 key 优先，其次复用 AstrBot 全局同家 key；都没有返回空串。"""
    try:
        key = str(plugin.settings.get("web_search_api_key") or "").strip()
        if key:
            return key
        if not plugin.settings.get("web_search_reuse_global_key"):
            return ""
    except Exception as e:  # noqa: BLE001 - 配置读取失败不该影响插件启动
        logger.warning(f"[群聊记忆] 读取搜索 key 失败: {e}")
        return ""
    return _global_key(plugin, provider)


def _global_key(plugin, provider: str) -> str:
    """从全局配置 provider_settings 里取 key（list，取第一个非空）。"""
    field = GLOBAL_KEY_FIELDS.get(provider)
    if not field:
        return ""
    config = _global_config(plugin)
    if config is None:
        return ""
    return _pick_key(config, field)


def _global_config(plugin) -> dict | None:
    """取全局配置。读全局 key 只需要一个 umo，拿现有会话列表里的任意一个即可。"""
    context = getattr(plugin, "context", None)
    if context is None:
        return None
    for umo in _umo_candidates(plugin):
        try:
            return context.get_config(umo=umo)
        except Exception:  # noqa: BLE001 - 换个 umo 再试
            continue
    try:
        return context.get_config()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[群聊记忆] 读取 AstrBot 全局配置失败，无法复用 key: {e}")
        return None


def _umo_candidates(plugin) -> list[str]:
    """凑几个真实存在的会话 umo，用于向全局配置要一份快照。"""
    candidates: list[str] = []
    try:
        history = getattr(getattr(plugin, "storage", None), "chat_history", None)
        if isinstance(history, dict):
            candidates.extend(str(k) for k in history if k)
    except Exception:  # noqa: BLE001
        pass
    try:
        whitelist = plugin.settings.get("session_whitelist") or []
        candidates.extend(str(x) for x in whitelist if x)
    except Exception:  # noqa: BLE001
        pass
    if not candidates:
        candidates.append("default:FriendMessage:0")
    return candidates


def _pick_key(config: Any, field: str) -> str:
    try:
        if not isinstance(config, dict):
            return ""
        provider_settings = _clean_dict(config).get("provider_settings")
        if not isinstance(provider_settings, dict):
            return ""
        value = _clean_dict(provider_settings).get(field)
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, str) and item.strip():
                    return item.strip()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[群聊记忆] 解析全局搜索 key 失败: {e}")
    return ""


def _clean_dict(data: dict) -> dict:
    """配置首层偶尔带 BOM 或空格的脏键（老版本迁移留下的），统一规整一次。"""
    return {str(k).lstrip("\ufeff").strip(): v for k, v in data.items()}


# ── 各 provider 的 HTTP 实现 ─────────────────────────────────────────────


async def search_deepseek(
    query: str,
    key: str,
    *,
    count: int,
    need_content: bool,
    timeout: float,
    ori_question: str = "",
) -> list[dict]:
    payload: dict[str, Any] = {
        "query": query,
        "count": count,
        "need_content": bool(need_content),
    }
    # ori_question 是可选参数，服务端限制了 1-128 字符
    if ori_question:
        payload["ori_question"] = ori_question[:128]

    data = await _request_json(
        "POST",
        DEEPSEEK_URL,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        payload=payload,
        timeout=timeout,
    )
    results: list[dict] = []
    for item in _as_list((data or {}).get("results") if isinstance(data, dict) else None):
        if not isinstance(item, dict):
            continue
        snippet = str(item.get("snippet") or item.get("summary") or "").strip()
        if need_content:
            content = str(item.get("content") or "").strip()
            if content:
                snippet = f"{snippet}\n{content[:CONTENT_MAX]}".strip()
        results.append(
            {
                "title": str(item.get("title") or "").strip(),
                "url": str(item.get("url") or "").strip(),
                "snippet": snippet[:SNIPPET_MAX + CONTENT_MAX],
            }
        )
    return results


async def search_tavily(
    query: str, key: str, *, count: int, timeout: float
) -> list[dict]:
    data = await _request_json(
        "POST",
        TAVILY_URL,
        headers={"Content-Type": "application/json"},
        payload={"api_key": key, "query": query, "max_results": count},
        timeout=timeout,
    )
    results: list[dict] = []
    for item in _as_list((data or {}).get("results") if isinstance(data, dict) else None):
        if not isinstance(item, dict):
            continue
        results.append(
            {
                "title": str(item.get("title") or "").strip(),
                "url": str(item.get("url") or "").strip(),
                "snippet": str(item.get("content") or "").strip()[:SNIPPET_MAX],
            }
        )
    return results


async def search_bocha(
    query: str, key: str, *, count: int, timeout: float
) -> list[dict]:
    data = await _request_json(
        "POST",
        BOCHA_URL,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        payload={"query": query, "count": count, "summary": True},
        timeout=timeout,
    )
    pages = None
    if isinstance(data, dict):
        pages = ((data.get("data") or {}).get("webPages") or {}).get("value")
    results: list[dict] = []
    for item in _as_list(pages):
        if not isinstance(item, dict):
            continue
        results.append(
            {
                "title": str(item.get("name") or "").strip(),
                "url": str(item.get("url") or "").strip(),
                "snippet": str(
                    item.get("snippet") or item.get("summary") or ""
                ).strip()[:SNIPPET_MAX],
            }
        )
    return results


async def search_brave(
    query: str, key: str, *, count: int, timeout: float
) -> list[dict]:
    data = await _request_json(
        "GET",
        BRAVE_URL,
        headers={
            "X-Subscription-Token": key,
            "Accept": "application/json",
        },
        params={"q": query, "count": count},
        timeout=timeout,
    )
    items = (data or {}).get("web", {}).get("results") if isinstance(data, dict) else None
    results: list[dict] = []
    for item in _as_list(items):
        if not isinstance(item, dict):
            continue
        results.append(
            {
                "title": str(item.get("title") or "").strip(),
                "url": str(item.get("url") or "").strip(),
                "snippet": str(item.get("description") or "").strip()[:SNIPPET_MAX],
            }
        )
    return results


async def _request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    timeout: float,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> Any:
    """发一次请求并解析 JSON。任何失败都返回 None，由调用方当成空结果处理。"""
    try:
        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.request(
                method,
                url,
                headers=headers,
                params=params,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:200]
                    logger.warning(f"[群聊记忆] 搜索接口返回 {resp.status}: {body}")
                    return None
                return await resp.json(content_type=None)
    except asyncio.TimeoutError:
        logger.warning(f"[群聊记忆] 搜索请求超时（{timeout}s）: {url}")
    except Exception as e:  # noqa: BLE001 - 网络/解析异常一律降级成空结果
        logger.warning(f"[群聊记忆] 搜索请求失败: {e}")
    return None


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


# ── 工具类 ──────────────────────────────────────────────────────────────


class _SearchTool(FunctionTool[AstrAgentContext]):
    """provider 无关的公共实现：两层配额 + 结果格式化。"""

    _provider: str = ""
    _tool_name: str = ""

    def __init__(
        self,
        plugin,
        session_id: str,
        *,
        api_key: str,
        max_results: int,
        timeout: float,
        need_content: bool,
        max_per_turn: int,
        daily_limit: int,
    ):
        try:
            super().__init__(
                name=self._tool_name,
                description=_DESCRIPTIONS[self._provider],
                parameters=dict(_SEARCH_PARAMETERS),
            )
        except TypeError:  # 老版本 FunctionTool 不是 pydantic 模型，属性得自己塞
            super().__init__()
            self.name = self._tool_name
            self.description = _DESCRIPTIONS[self._provider]
            self.parameters = dict(_SEARCH_PARAMETERS)

        self._plugin = plugin
        self._session_id = session_id
        self._api_key = api_key
        self._max_results = max_results
        self._timeout = timeout
        self._need_content = need_content
        self._max_per_turn = max_per_turn
        self._daily_limit = daily_limit
        # 工具实例随请求创建，天然是「单轮」作用域，不必额外清洗
        self._turn_calls = 0

    async def call(self, context, **kwargs) -> ToolExecResult:
        """返回字符串即可——``ToolExecResult`` 只是 ``str | CallToolResult`` 的类型别名，
        不是可调用类型，AstrBot 内置工具也都是直接返回 str。"""
        query = str(kwargs.get("query") or "").strip()
        if not query:
            return EMPTY_QUERY_TEXT

        # 第一层：单轮上限
        if self._max_per_turn > 0 and self._turn_calls >= self._max_per_turn:
            return TURN_LIMIT_TEXT

        # 第二层：每日配额（按群独立）
        used, limit = self._usage()
        if limit > 0 and used >= limit:
            return DAILY_LIMIT_TEXT.format(used=used, limit=limit)

        # 发了请求就算一次尝试，失败也占单轮名额，避免模型连环重试
        self._turn_calls += 1
        try:
            results = await self._search(query)
        except Exception as e:  # noqa: BLE001 - 工具异常不能冒出调用链
            logger.warning(f"[群聊记忆] 搜索工具异常: {e}")
            return f"搜索失败（{type(e).__name__}），请直接依据已有知识回答。"

        if not results:
            return EMPTY_TEXT

        self._record()
        return _format_results(results[: self._max_results])

    async def _search(self, query: str) -> list[dict]:
        raise NotImplementedError

    def _usage(self) -> tuple[int, int]:
        """返回 (今日已用, 上限)。usage 缺失时按 0 处理，配额失效不影响正常搜索。"""
        limit = int(self._daily_limit)
        try:
            usage = getattr(self._plugin, "usage", None)
            if usage is None:
                return 0, limit
            return int(usage.search_used(self._session_id) or 0), limit
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[群聊记忆] 读取搜索配额失败: {e}")
            return 0, limit

    def _record(self) -> None:
        try:
            usage = getattr(self._plugin, "usage", None)
            if usage is not None:
                usage.record_search(self._session_id)
        except Exception as e:  # noqa: BLE001 - 计数失败不值得打断回复
            logger.warning(f"[群聊记忆] 记录搜索次数失败: {e}")


class _DeepSeekSearchTool(_SearchTool):
    _provider = "deepseek"
    _tool_name = "web_search_deepseek"

    async def _search(self, query: str) -> list[dict]:
        return await search_deepseek(
            query,
            self._api_key,
            count=self._max_results,
            need_content=self._need_content,
            timeout=self._timeout,
        )


class _TavilySearchTool(_SearchTool):
    _provider = "tavily"
    _tool_name = "web_search_tavily"

    async def _search(self, query: str) -> list[dict]:
        return await search_tavily(
            query, self._api_key, count=self._max_results, timeout=self._timeout
        )


class _BochaSearchTool(_SearchTool):
    _provider = "bocha"
    _tool_name = "web_search_bocha"

    async def _search(self, query: str) -> list[dict]:
        return await search_bocha(
            query, self._api_key, count=self._max_results, timeout=self._timeout
        )


class _BraveSearchTool(_SearchTool):
    _provider = "brave"
    _tool_name = "web_search_brave"

    async def _search(self, query: str) -> list[dict]:
        return await search_brave(
            query, self._api_key, count=self._max_results, timeout=self._timeout
        )


_TOOL_CLASSES = {
    "deepseek": _DeepSeekSearchTool,
    "tavily": _TavilySearchTool,
    "bocha": _BochaSearchTool,
    "brave": _BraveSearchTool,
}


def _format_results(results: list[dict]) -> str:
    """转成模型看得懂的 JSON，index 是给人看的引用编号。"""
    ref = uuid.uuid4().hex[:4]
    payload = [
        {
            "title": item.get("title") or "",
            "url": item.get("url") or "",
            "snippet": item.get("snippet") or "",
            "index": f"{ref}.{i}",
        }
        for i, item in enumerate(results, start=1)
    ]
    return json.dumps({"results": payload}, ensure_ascii=False)


# ── 工厂 ────────────────────────────────────────────────────────────────


def build_search_tool(plugin, session_id: str, provider: str | None = None) -> object | None:
    """按配置造一个搜索工具实例。配额判断不在这里，交给 call() 和调用方。"""
    try:
        name = str(provider or plugin.settings.get("web_search_provider") or "").strip().lower()
        tool_cls = _TOOL_CLASSES.get(name)
        if tool_cls is None:
            logger.warning(f"[群聊记忆] 不支持的搜索提供方 {name!r}，跳过挂载")
            return None

        key = resolve_key(plugin, name)
        if not key:
            logger.warning("[群聊记忆] 搜索 key 未配置，plugin 模式跳过挂载")
            return None

        return tool_cls(
            plugin,
            session_id,
            api_key=key,
            max_results=max(1, int(plugin.settings.get("web_search_max_results"))),
            timeout=max(1.0, float(plugin.settings.get("web_search_timeout"))),
            need_content=bool(plugin.settings.get("web_search_need_content")),
            max_per_turn=int(plugin.settings.get("web_search_max_per_turn")),
            daily_limit=int(plugin.settings.get("web_search_daily_limit")),
        )
    except Exception as e:  # noqa: BLE001 - 挂不上就退化成不搜索，不能挡住回复
        logger.warning(f"[群聊记忆] 构建搜索工具失败: {e}")
        return None

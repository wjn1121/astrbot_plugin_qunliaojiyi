"""插件 Web API：面板数据、管理操作、注入预览、备份导入。

拆成独立模块的原因：main.py 只负责装配，接口逻辑集中在这里，
以后加接口或改返回结构不会动到 Star 类。
"""

from __future__ import annotations

import json
import re
import time

from quart import jsonify, request

from astrbot.api import logger

from .constants import PLUGIN_VERSION
from .core.storage import MANUAL_MAX_PER_SESSION
from .utils import display_name


def register(plugin) -> None:
    """把全部接口注册到 AstrBot context。"""
    routes = (
        (f"/{plugin.plugin_name}/status", api_status, ["GET"], "群聊记忆状态"),
        (f"/{plugin.plugin_name}/manage", api_manage, ["POST"], "群聊记忆管理"),
        (f"/{plugin.plugin_name}/preview", api_preview, ["POST"], "注入效果预览"),
        (f"/{plugin.plugin_name}/import", api_import, ["POST"], "导入旧版备份"),
        (f"/{plugin.plugin_name}/export", api_export, ["GET"], "导出全部数据"),
    )
    for route, func, methods, desc in routes:
        try:
            plugin.context.register_web_api(route, _bind(plugin, func), methods, desc)
        except Exception as e:  # noqa: BLE001 - 老版本 AstrBot 可能没有该接口
            logger.warning(f"[群聊记忆] 注册 Web API {route} 失败: {e}")


def _bind(plugin, func):
    """把 plugin 实例绑到无参 handler 上（quart 调用时不传 request）。"""

    async def handler():
        return await func(plugin)

    return handler


# ── 序列化 ──────────────────────────────────────────────────────────────


def _serialize_session(plugin, session_id: str, msgs) -> dict:
    storage = plugin.storage
    learned_stable, learned_sit = storage.learned_split(session_id)
    manual = storage.manual_of(session_id)
    override = storage.session_override(session_id)
    injector = plugin.manager.injector
    # 面板要告诉用户「哪几条真的会进注入」，否则改熟练度全靠猜
    selected = sorted(injector.selected_stable_contents(session_id))

    # 主动回复状态与搜索配额都可能因为数据损坏拿不到，单独兜住——
    # 一个会话的附加信息坏掉不该让整个列表 500
    try:
        active_status = plugin.active.status(session_id)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[群聊记忆] 读取主动回复状态失败 {session_id}: {e}")
        active_status = {}
    try:
        search_used = plugin.usage.search_used(session_id)
    except Exception:  # noqa: BLE001
        search_used = 0

    return {
        "session_id": session_id,
        "display_name": display_name(session_id),
        "stable": learned_stable,
        "situational": learned_sit,
        "manual": manual,
        "history": msgs[-30:],
        "history_count": len(msgs),
        "learn_state": storage.learn_state.get(session_id, {}),
        "override": override,
        "learn_enabled": override.get("learn", True),
        "inject_enabled": override.get("inject", True),
        "active_enabled": bool(override.get("active", True)),
        "search_allowed": storage.search_allowed(session_id),
        "search_used": search_used,
        "search_limit": int(plugin.settings.get("web_search_daily_limit")),
        "active_status": active_status,
        "selected": selected,
        "limits": injector.limits(),
    }


def _valid_session_id(session_id: str) -> bool:
    """粗校验 unified_msg_origin：至少三段且每段非空。

    真校验不了平台实例是否真实存在（那是运行时信息），所以面板侧还要靠
    「前缀下拉 + 未知会话二次确认」兜住最常见的填错场景。
    """
    parts = (session_id or "").split(":")
    return len(parts) >= 3 and all(p.strip() for p in parts[:3])


def _matches(session: dict, query: str) -> bool:
    if not query:
        return True
    haystack = [
        session.get("session_id", ""),
        session.get("display_name", ""),
    ]
    for t in (
        session.get("stable", []) + session.get("situational", []) + session.get("manual", [])
    ):
        haystack.append(str(t.get("content") or ""))
        haystack.append(str(t.get("context") or ""))
        haystack.extend(str(x) for x in (t.get("triggers") or []))
    return any(query in str(x).lower() for x in haystack)


# ── GET /status ─────────────────────────────────────────────────────────


async def api_status(plugin):
    query = (request.args.get("q") or "").strip().lower()
    try:
        page = max(1, int(request.args.get("page") or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(request.args.get("page_size") or 20)
    except (TypeError, ValueError):
        page_size = 20
    page_size = max(5, min(100, page_size))
    sort = (request.args.get("sort") or "traits").strip()

    # 学习产出、手动记忆、待学习缓冲任一有数据就列出来：
    # 手动加了记忆但群里还没说过话的会话也必须出现在面板里。
    known = set(plugin.storage.universal) | set(plugin.storage.manual)
    # 新建会话时的平台实例候选：从已有 session_id 抽第一段。
    # 手填平台实例是最容易出错的地方（填成 aiocqhttp 而实际是 bot1 就会造出僵尸会话）。
    platforms = sorted(
        {sid.split(":", 1)[0] for sid in known | set(plugin.storage.chat_history) if ":" in sid}
    )
    sessions = [
        _serialize_session(plugin, session_id, plugin.storage.chat_history.get(session_id, []))
        for session_id in known
    ]
    # 只有待学习消息、还没学到特征的会话也列出来，方便调试
    for session_id, msgs in plugin.storage.chat_history.items():
        if session_id in known or not msgs:
            continue
        sessions.append(_serialize_session(plugin, session_id, msgs))

    sessions = [s for s in sessions if _matches(s, query)]
    if sort == "recent":
        sessions.sort(key=lambda s: s["learn_state"].get("last_learn_at", 0), reverse=True)
    elif sort == "name":
        sessions.sort(key=lambda s: s["display_name"])
    else:
        sessions.sort(
            key=lambda s: (
                # 手动记忆也要计入，否则只加了一条手工记忆的新群会沉到最后一页，
                # 用户刷新后找不到，直接当成没加上。
                len(s["stable"]) + len(s["situational"]) + len(s["manual"]),
                s["history_count"],
            ),
            reverse=True,
        )

    total = len(sessions)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    start = (page - 1) * page_size

    return jsonify(
        {
            "success": True,
            "data": {
                "version": PLUGIN_VERSION,
                "stats": plugin.storage.stats(),
                "manager": plugin.manager.status(),
                "settings": plugin.settings.snapshot(),
                "sessions": sessions[start : start + page_size],
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": total_pages,
                "query": query,
                "sort": sort,
                "platforms": platforms,
                "known_ids": sorted(known | set(plugin.storage.chat_history)),
                "inject_limits": plugin.manager.injector.limits(),
                "manual_max": MANUAL_MAX_PER_SESSION,
                "usage": _safe_usage(plugin),
                "active_enabled": bool(plugin.settings.get("active_reply_enabled")),
                "search": _search_summary(plugin),
                "style": _style_summary(plugin),
            },
        }
    )


def _safe_usage(plugin) -> dict:
    try:
        return plugin.usage.summary()
    except Exception as e:  # noqa: BLE001 - 统计坏了面板也要能打开
        logger.warning(f"[群聊记忆] 读取消耗统计失败: {e}")
        return {"today": {}, "days": [], "search_used_by_session": {}}


def _search_summary(plugin) -> dict:
    """搜索结果面板要显示「当前到底能不能搜」，省得用户配了 key 还以为没生效。"""
    try:
        provider = str(plugin.settings.get("web_search_provider") or "").strip().lower()
        key = ""
        try:
            from .core.search import resolve_key

            key = resolve_key(plugin, provider)
        except Exception:  # noqa: BLE001
            key = ""
        return {
            "mode": str(plugin.settings.get("web_search_mode") or "plugin"),
            "provider": provider,
            "daily_limit": int(plugin.settings.get("web_search_daily_limit")),
            "max_per_turn": int(plugin.settings.get("web_search_max_per_turn")),
            "need_content": bool(plugin.settings.get("web_search_need_content")),
            "key_configured": bool(key),
        }
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[群聊记忆] 读取搜索配置失败: {e}")
        return {}


def _style_summary(plugin) -> dict:
    injector = plugin.manager.injector
    try:
        strength = injector.strength_of(active=False)
        active_strength = injector.strength_of(active=True)
        return {
            "strength": strength,
            "active_strength": active_strength,
            "override_threshold": int(plugin.settings.get("style_override_threshold")),
            "override_probability": injector.override_probability(strength),
            "active_override_probability": injector.override_probability(active_strength),
        }
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[群聊记忆] 读取风格强度失败: {e}")
        return {}


# ── POST /manage ────────────────────────────────────────────────────────


async def api_manage(plugin):
    body = await request.get_json(silent=True) or {}
    action = str(body.get("action") or "")
    session_id = str(body.get("session_id") or "")

    try:
        if action == "learn":
            return jsonify({"success": True, "data": await plugin.manager.learn_now(session_id)})
        if action == "add_trait":
            if not _valid_session_id(session_id):
                return jsonify(
                    {"success": False, "message": "会话 ID 格式应为「平台:GroupMessage:群号」"}
                )
            trait, error = await plugin.storage.add_manual_trait(
                session_id,
                str(body.get("content") or ""),
                category=str(body.get("category") or "stable"),
                context=str(body.get("context") or ""),
                triggers=body.get("triggers"),
                proficiency=body.get("proficiency"),
            )
            if trait is None:
                return jsonify({"success": False, "message": error})
            await plugin.storage.flush()
            return jsonify({"success": True, "data": trait, "message": "已添加"})
        if action == "add_traits_batch":
            if not _valid_session_id(session_id):
                return jsonify(
                    {"success": False, "message": "会话 ID 格式应为「平台:GroupMessage:群号」"}
                )
            # 批量粘贴：一行一条「内容|场景|触发词1,触发词2」，场景与触发词可省略
            category = str(body.get("category") or "stable")
            proficiency = body.get("proficiency")
            added: list[str] = []
            failed: list[dict] = []
            for raw in str(body.get("text") or "").splitlines():
                line = raw.strip()
                if not line:
                    continue
                parts = [p.strip() for p in line.split("|")]
                triggers = (
                    [x for x in re.split(r"[,，\s]+", parts[2]) if x]
                    if len(parts) > 2 and parts[2]
                    else []
                )
                trait, error = await plugin.storage.add_manual_trait(
                    session_id,
                    parts[0] if parts else "",
                    category=category,
                    context=parts[1] if len(parts) > 1 else "",
                    triggers=triggers,
                    proficiency=proficiency,
                )
                if trait is None:
                    failed.append({"line": line[:40], "reason": error})
                else:
                    added.append(str(trait.get("content") or ""))
            await plugin.storage.flush()
            return jsonify({"success": True, "data": {"added": added, "failed": failed}})
        if action == "delete_trait":
            ok = await plugin.storage.delete_trait(
                session_id,
                str(body.get("content") or ""),
                source=str(body.get("source") or "") or None,
            )
            return jsonify({"success": ok, "message": "已删除" if ok else "未找到该特征"})
        if action == "clear_session":
            scope = str(body.get("scope") or "all")
            await plugin.storage.clear_session(session_id, scope=scope)
            label = {
                "learned": "学习产出",
                "manual": "手动记忆",
                "history": "待学习消息",
                "active": "主动回复状态",
            }.get(scope, "全部记忆")
            return jsonify({"success": True, "message": f"已清空该会话的{label}"})
        if action == "clear_history":
            await plugin.storage.clear_history(session_id)
            return jsonify({"success": True, "message": "已清空待学习记录"})
        if action == "clear_all":
            scope = str(body.get("scope") or "all")
            result = await plugin.storage.clear_all(scope=scope)
            parts = []
            if result.get("learned"):
                parts.append(f"{result['learned']} 个会话的学习产出")
            if result.get("manual"):
                parts.append(f"{result['manual']} 个会话的手动记忆")
            if result.get("history"):
                parts.append(f"{result['history']} 个会话的待学习消息")
            if result.get("active"):
                parts.append(f"{result['active']} 个会话的主动回复状态")
            return jsonify(
                {
                    "success": True,
                    "data": result,
                    "message": ("已清空 " + "、".join(parts)) if parts else "没有可清空的数据",
                }
            )
        if action == "set_session_override":
            kwargs: dict = {"learn": body.get("learn"), "inject": body.get("inject")}
            # search 是三态。优先认字符串形式（search_mode），因为面板请求要穿一层宿主桥接，
            # 传 JSON null 有被过滤掉的风险，一旦丢键就变成「不改」，用户选「跟随全局」会静默失效。
            if "search_mode" in body:
                mode = str(body.get("search_mode") or "").strip().lower()
                kwargs["search"] = None if mode == "inherit" else (mode == "on")
            elif "search" in body:
                raw = body.get("search")
                kwargs["search"] = None if raw is None else bool(raw)
            override = await plugin.storage.set_override(session_id, **kwargs)
            if body.get("active") is not None:
                plugin.active.set_enabled(session_id, bool(body["active"]))
            return jsonify({"success": True, "data": {"session_id": session_id, "override": override}})
        if action == "set_active":
            return jsonify(
                {
                    "success": True,
                    "data": plugin.active.set_enabled(session_id, bool(body.get("enabled"))),
                }
            )
        if action == "clear_active":
            removed = plugin.storage.clear_active(session_id)
            return jsonify(
                {
                    "success": True,
                    "message": "已重置本群主动回复状态" if removed else "本来就没有状态记录",
                }
            )
        if action == "usage_detail":
            days = body.get("days")
            try:
                days = int(days) if days is not None else 7
            except (TypeError, ValueError):
                days = 7
            return jsonify({"success": True, "data": plugin.usage.snapshot(days=max(1, min(31, days)))})
        if action == "clear_override":
            removed = await plugin.storage.clear_override(session_id)
            return jsonify(
                {"success": True, "message": "已恢复默认" if removed else "本来就没有单独设置"}
            )
        if action == "update_trait":
            updated, error = await plugin.storage.update_trait(
                session_id,
                str(body.get("content") or ""),
                new_content=body.get("new_content"),
                context=body.get("context"),
                triggers=body.get("triggers"),
                proficiency=body.get("proficiency"),
                category=body.get("category"),
                source=str(body.get("source") or "") or None,
            )
            if updated is None:
                return jsonify({"success": False, "message": error or "保存失败"})
            await plugin.storage.flush()
            return jsonify({"success": True, "data": updated})
        if action == "set_setting":
            key = str(body.get("key") or "")
            value = await plugin.settings.set_runtime(key, body.get("value"))
            return jsonify({"success": True, "data": {key: value}})
        if action == "reset_runtime":
            plugin.settings.clear_runtime()
            return jsonify({"success": True, "message": "已恢复为配置文件中的取值"})
        return jsonify({"success": False, "message": f"未知操作: {action}"})
    except Exception as e:  # noqa: BLE001
        logger.error(f"[群聊记忆] 面板操作失败 {action}: {e}")
        return jsonify({"success": False, "message": str(e)})


# ── POST /preview ───────────────────────────────────────────────────────


async def api_preview(plugin):
    body = await request.get_json(silent=True) or {}
    session_id = str(body.get("session_id") or "")
    message = str(body.get("message") or "")
    # 主动档位也能预览：插话用的强度和被动不一样，只看被动会误判效果
    active = bool(body.get("active"))
    injector = plugin.manager.injector
    try:
        strength = injector.strength_of(active=active)
        text = await injector.build_injection_text(
            session_id, message, active=active
        )
        return jsonify(
            {
                "success": True,
                "data": {
                    "text": text or "",
                    "would_inject": bool(text),
                    "should_inject": injector.should_inject(session_id),
                    "active": active,
                    "strength": strength,
                    "override_probability": injector.override_probability(strength),
                    "length_rule": injector.build_length_rule(active) or "",
                    "limits": injector.limits(strength),
                },
            }
        )
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "message": str(e)})


# ── POST /import ────────────────────────────────────────────────────────


async def api_import(plugin):
    body = await request.get_json(silent=True) or {}
    mode = str(body.get("mode") or "merge")
    backup_root = plugin.plugin_dir / "backup"
    try:
        # 支持指定子目录；不指定就取名字最大的那个（通常是最新导出的）
        wanted = str(body.get("dir") or "").strip()
        if wanted:
            backup_dir = backup_root / wanted
        else:
            candidates = (
                sorted(
                    (p for p in backup_root.iterdir() if (p / "universal.json").is_file()),
                    key=lambda p: p.name,
                    reverse=True,
                )
                if backup_root.is_dir()
                else []
            )
            backup_dir = candidates[0] if candidates else None

        if backup_dir is None or not (backup_dir / "universal.json").is_file():
            return jsonify(
                {
                    "success": False,
                    "message": "没有找到可导入的备份，请把 universal.json 放到 backup/<目录>/ 下",
                }
            )

        universal = json.loads((backup_dir / "universal.json").read_text(encoding="utf-8"))
        history_path = backup_dir / "chat_history.json"
        chat_history = (
            json.loads(history_path.read_text(encoding="utf-8")) if history_path.is_file() else {}
        )
        # 1.2.0 起手动记忆单独一个文件；老备份没有它，按空处理即可
        manual_path = backup_dir / "manual_traits.json"
        manual = (
            json.loads(manual_path.read_text(encoding="utf-8")) if manual_path.is_file() else {}
        )
        result = plugin.storage.import_backup(
            universal,
            chat_history,
            mode=mode,
            manual=manual,
            limits=(
                int(plugin.settings.get("max_stable_traits")),
                int(plugin.settings.get("max_situational_traits")),
            ),
        )
        await plugin.storage.flush()
        return jsonify({"success": True, "data": {**result, "dir": backup_dir.name}})
    except Exception as e:  # noqa: BLE001
        logger.error(f"[群聊记忆] 导入备份失败: {e}")
        return jsonify({"success": False, "message": str(e)})


# ── GET /export ─────────────────────────────────────────────────────────


async def api_export(plugin):
    """导出全部数据，供面板下载为 JSON 备份。"""
    return jsonify(
        {
            "success": True,
            "data": {
                "version": PLUGIN_VERSION,
                "exported_at": time.time(),
                "universal": plugin.storage.universal,
                "manual_traits": plugin.storage.manual,
                "chat_history": plugin.storage.chat_history,
                "learn_state": plugin.storage.learn_state,
                "overrides": plugin.storage.overrides,
                "active_state": plugin.storage.active,
                "metrics": plugin.storage.metrics,
            },
        }
    )

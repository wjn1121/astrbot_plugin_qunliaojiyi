"""插件 Web API：面板数据、管理操作、注入预览、备份导入。

拆成独立模块的原因：main.py 只负责装配，接口逻辑集中在这里，
以后加接口或改返回结构不会动到 Star 类。
"""

from __future__ import annotations

import json
import time

from quart import jsonify, request

from astrbot.api import logger

from .constants import PLUGIN_VERSION
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


def _serialize_session(plugin, session_id: str, traits, msgs) -> dict:
    stable, situational = plugin.storage.split(session_id)
    override = plugin.storage.session_override(session_id)
    return {
        "session_id": session_id,
        "display_name": display_name(session_id),
        "stable": stable,
        "situational": situational,
        "history": msgs[-30:],
        "history_count": len(msgs),
        "learn_state": plugin.storage.learn_state.get(session_id, {}),
        "override": override,
        "learn_enabled": override.get("learn", True),
        "inject_enabled": override.get("inject", True),
    }


def _matches(session: dict, query: str) -> bool:
    if not query:
        return True
    haystack = [
        session.get("session_id", ""),
        session.get("display_name", ""),
    ]
    for t in session.get("stable", []) + session.get("situational", []):
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

    sessions = []
    for session_id in plugin.storage.universal:
        sessions.append(
            _serialize_session(
                plugin,
                session_id,
                plugin.storage.traits_of(session_id),
                plugin.storage.chat_history.get(session_id, []),
            )
        )
    # 只有待学习消息、还没学到特征的会话也列出来，方便调试
    for session_id, msgs in plugin.storage.chat_history.items():
        if session_id in plugin.storage.universal or not msgs:
            continue
        sessions.append(_serialize_session(plugin, session_id, [], msgs))

    sessions = [s for s in sessions if _matches(s, query)]
    if sort == "recent":
        sessions.sort(key=lambda s: s["learn_state"].get("last_learn_at", 0), reverse=True)
    elif sort == "name":
        sessions.sort(key=lambda s: s["display_name"])
    else:
        sessions.sort(
            key=lambda s: (len(s["stable"]) + len(s["situational"]), s["history_count"]),
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
            },
        }
    )


# ── POST /manage ────────────────────────────────────────────────────────


async def api_manage(plugin):
    body = await request.get_json(silent=True) or {}
    action = str(body.get("action") or "")
    session_id = str(body.get("session_id") or "")

    try:
        if action == "learn":
            return jsonify({"success": True, "data": await plugin.manager.learn_now(session_id)})
        if action == "delete_trait":
            ok = await plugin.storage.delete_trait(session_id, str(body.get("content") or ""))
            return jsonify({"success": ok, "message": "已删除" if ok else "未找到该特征"})
        if action == "clear_session":
            await plugin.storage.clear_session(session_id)
            return jsonify({"success": True, "message": "已清空该会话"})
        if action == "clear_history":
            await plugin.storage.clear_history(session_id)
            return jsonify({"success": True, "message": "已清空待学习记录"})
        if action == "clear_all":
            count = await plugin.storage.clear_all()
            return jsonify({"success": True, "message": f"已清空 {count} 个会话"})
        if action == "set_session_override":
            override = await plugin.storage.set_override(
                session_id,
                learn=body.get("learn"),
                inject=body.get("inject"),
            )
            return jsonify({"success": True, "data": {"session_id": session_id, "override": override}})
        if action == "clear_override":
            removed = await plugin.storage.clear_override(session_id)
            return jsonify(
                {"success": True, "message": "已恢复默认" if removed else "本来就没有单独设置"}
            )
        if action == "update_trait":
            old_content = str(body.get("content") or "")
            updated = await plugin.storage.update_trait(
                session_id,
                old_content,
                new_content=body.get("new_content"),
                context=body.get("context"),
                triggers=body.get("triggers"),
            )
            if updated is None:
                return jsonify({"success": False, "message": "未找到该特征"})
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
    try:
        text = await plugin.manager.injector.build_injection_text(session_id, message)
        return jsonify(
            {
                "success": True,
                "data": {
                    "text": text or "",
                    "would_inject": bool(text),
                    "should_inject": plugin.manager.injector.should_inject(session_id),
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
        result = plugin.storage.import_backup(
            universal,
            chat_history,
            mode=mode,
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
                "chat_history": plugin.storage.chat_history,
                "learn_state": plugin.storage.learn_state,
                "overrides": plugin.storage.overrides,
                "metrics": plugin.storage.metrics,
            },
        }
    )

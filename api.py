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
from .core.storage import MANUAL_MAX_PER_SESSION, trait_score
from .utils import display_name


def register(plugin) -> None:
    """把全部接口注册到 AstrBot context。"""
    routes = (
        (f"/{plugin.plugin_name}/status", api_status, ["GET"], "群聊记忆状态"),
        (f"/{plugin.plugin_name}/manage", api_manage, ["POST"], "群聊记忆管理"),
        (f"/{plugin.plugin_name}/preview", api_preview, ["POST"], "注入效果预览"),
        (f"/{plugin.plugin_name}/import", api_import, ["POST"], "导入旧版备份"),
        (f"/{plugin.plugin_name}/export", api_export, ["GET"], "导出全部数据"),
        (f"/{plugin.plugin_name}/traits", api_traits, ["GET"], "特征库与待审列表"),
        (f"/{plugin.plugin_name}/review", api_review, ["POST"], "特征审核"),
        (f"/{plugin.plugin_name}/persona", api_persona, ["GET"], "群人格档案"),
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
        # 面板里置顶的群/私聊排最前面，纯展示顺序偏好
        "pinned": storage.is_pinned(session_id),
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
        "trait_stats": storage.trait_stats(session_id),
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
    # 置顶的永远在最前：先按上面的主序排好，再按置顶顺序做一次稳定排序，
    # 这样同为主序时置顶之间的相对顺序也固定，翻页不会跳来跳去。
    pinned_ids = plugin.storage.pinned_sessions()
    pinned_rank = {sid: i for i, sid in enumerate(pinned_ids)}
    unpinned_rank = len(pinned_ids) + 10**6
    sessions.sort(key=lambda s: pinned_rank.get(s["session_id"], unpinned_rank))

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
                # 置顶的会话按用户设定顺序排好，前端直接照着渲染下拉与分组
                "pinned_ids": pinned_ids,
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
        return {"today": {}, "days": [], "hours": [], "current_hour": 0, "search_used_by_session": {}}


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


# ── 特征库与审核（1.4.0）────────────────────────────────────────────────


async def api_traits(plugin):
    """特征库列表。

    不带 session_id 时跨会话汇总，除了平铺的 traits 还给一份按会话分好的
    groups——面板的学习库是按「群聊 / 私聊」分开看的，平铺列表里同一个群的
    条目会散落各处，找不到是哪来的。
    """
    storage = plugin.storage
    session_id = str(request.args.get("session_id") or "").strip()
    status_filter = str(request.args.get("status") or "").strip()
    now = time.time()

    sessions = [session_id] if session_id else list(storage.universal.keys())
    rows = []
    groups = []
    totals = {"pending": 0, "active": 0, "archived": 0, "rejected": 0, "manual": 0, "total": 0}
    for sid in sessions:
        all_traits = storage.universal.get(sid, [])
        counts = {"pending": 0, "active": 0, "archived": 0, "rejected": 0}
        for trait in all_traits:
            counts[trait.get("status", "active")] = counts.get(trait.get("status", "active"), 0) + 1
        manual_count = len(storage.manual.get(sid) or [])
        for key in ("pending", "active", "archived", "rejected"):
            totals[key] += counts.get(key, 0)
        totals["manual"] += manual_count
        totals["total"] += len(all_traits)

        group_rows = []
        for trait in all_traits:
            status = trait.get("status", "active")
            if status_filter and status != status_filter:
                continue
            group_rows.append(
                {
                    **trait,
                    "status": status,
                    "session_id": sid,
                    "effective_score": round(trait_score(trait, now), 1),
                }
            )
        # 组内按有效权重排，钉住的最前——和注入时的取舍顺序一致
        group_rows.sort(
            key=lambda x: (1 if x.get("pinned") else 0, x.get("effective_score", 0)),
            reverse=True,
        )
        rows.extend(group_rows)
        # 分组本身始终返回（哪怕过滤后为空），否则切状态时整个群会「消失」，
        # 用户会以为数据被删了。空组由前端显示成「这个筛选下没有条目」。
        groups.append(
            {
                "session_id": sid,
                "display_name": display_name(sid),
                "pinned": storage.is_pinned(sid),
                "counts": counts,
                "total": len(all_traits),
                "manual": manual_count,
                "traits": group_rows[:200],
            }
        )

    rows.sort(key=lambda x: x.get("effective_score", 0), reverse=True)
    pinned_ids = storage.pinned_sessions()
    pinned_rank = {sid: i for i, sid in enumerate(pinned_ids)}
    unpinned = len(pinned_ids) + 10**6
    # 置顶群优先；其余按「有生效条目 → 条数多」排，空群沉底
    groups.sort(
        key=lambda g: (
            pinned_rank.get(g["session_id"], unpinned),
            0 if g["counts"].get("pending") else 1,
            -g["total"],
        )
    )
    return jsonify(
        {
            "success": True,
            "data": {
                "traits": rows[:500],
                "groups": groups,
                "pending_total": len(storage.pending_traits()),
                "stats": storage.trait_stats(session_id) if session_id else totals,
            },
        }
    )


async def api_review(plugin):
    """人工审核：批准 / 否决 / 归档 / 恢复 / 钉住 / 批量批准。"""
    body = await request.get_json(silent=True) or {}
    action = str(body.get("action") or "")
    session_id = str(body.get("session_id") or "").strip()
    content = str(body.get("content") or "").strip()
    storage = plugin.storage

    status_map = {
        "approve": "active",
        "reject": "rejected",
        "archive": "archived",
        "restore": "active",
    }

    if action == "approve_all":
        pending = storage.pending_traits(session_id or None)
        done = 0
        touched: set[str] = set()
        for item in pending:
            if storage.set_trait_status(item["session_id"], item["content"], "active"):
                done += 1
                touched.add(item["session_id"])
        await storage.flush()
        # 批准就是「确认这条属于这个群」，人格档案要跟着长出来
        for sid in touched:
            plugin.manager.learner.refresh_persona(sid)
        return jsonify({"success": True, "data": {"approved": done}, "message": f"已批准 {done} 条"})

    if not session_id or not content:
        return jsonify({"success": False, "message": "缺少 session_id 或 content"})

    if action in ("pin", "unpin"):
        ok = storage.set_pinned(session_id, content, action == "pin")
    elif action in status_map:
        ok = storage.set_trait_status(session_id, content, status_map[action])
    else:
        return jsonify({"success": False, "message": f"未知操作: {action}"})

    if not ok:
        return jsonify({"success": False, "message": "没找到这条特征"})
    await storage.flush()
    # 审核改变了「已生效」的集合，人格档案的自动段要同步（钉住只影响排序，也一并刷）
    plugin.manager.learner.refresh_persona(session_id)
    return jsonify({"success": True, "message": "已更新"})


async def api_persona(plugin):
    """群人格档案。

    不带 session_id 返回概览（哪些群已经生成了档案）；带 session_id 返回单份
    全文。第一次打开某个群时会顺手生成一份，省得用户还得先点「重新生成」。
    """
    store = getattr(plugin, "persona", None)
    if store is None:
        return jsonify({"success": False, "message": "人格档案模块未初始化"})

    storage = plugin.storage
    session_id = str(request.args.get("session_id") or "").strip()

    if session_id:
        traits = storage.universal.get(session_id, [])
        if not store.exists(session_id) and traits:
            store.rebuild(session_id, traits, force=True)
        data = store.read(session_id)
        active = [t for t in traits if t.get("status", "active") == "active"]
        data.update(
            {
                "trait_total": len(traits),
                "trait_active": len(active),
                "pinned": storage.is_pinned(session_id),
            }
        )
        return jsonify({"success": True, "data": data})

    pinned_ids = storage.pinned_sessions()
    rank = {sid: i for i, sid in enumerate(pinned_ids)}
    unpinned = len(pinned_ids) + 10**6
    rows = []
    for sid, traits in storage.universal.items():
        active = sum(1 for t in traits if t.get("status", "active") == "active")
        rows.append(
            {
                "session_id": sid,
                "display_name": display_name(sid),
                "pinned": storage.is_pinned(sid),
                "exists": store.exists(sid),
                "updated_at": store.updated_at(sid),
                "trait_total": len(traits),
                "trait_active": active,
            }
        )
    rows.sort(key=lambda r: (rank.get(r["session_id"], unpinned), -r["trait_active"]))
    return jsonify(
        {
            "success": True,
            "data": {
                "sessions": rows,
                "dir": str(store.dir),
                "count": len(rows),
            },
        }
    )


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
            source = str(body.get("source") or "") or None
            ok = await plugin.storage.delete_trait(
                session_id,
                str(body.get("content") or ""),
                source=source,
            )
            if ok and source != "manual":
                plugin.manager.learner.refresh_persona(session_id)
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
        if action == "set_pin":
            # 置顶的是会话（群号 / QQ 号），不是某条特征；特征级的 pinned 走 /review
            changed = await plugin.storage.set_pin(session_id, bool(body.get("pinned")))
            if changed:
                await plugin.storage.flush()
            pinned = bool(body.get("pinned"))
            return jsonify(
                {
                    "success": True,
                    "data": {"session_id": session_id, "pinned": pinned},
                    "message": (
                        "已置顶，刷新后排在列表最前"
                        if pinned
                        else "已取消置顶"
                    )
                    if changed
                    else "本来就是这个状态",
                }
            )
        if action == "save_persona":
            # 只改人工段：自动段由学习/审核驱动，用户保存补充不该覆盖它
            if not _valid_session_id(session_id):
                return jsonify(
                    {"success": False, "message": "会话 ID 格式应为「平台:GroupMessage:群号」"}
                )
            result = plugin.persona.save_manual(session_id, str(body.get("manual") or ""))
            return jsonify(
                {
                    "success": bool(result.get("saved")),
                    "data": result,
                    "message": "人工补充已保存" if result.get("saved") else "保存失败，请看日志",
                }
            )
        if action == "rebuild_persona":
            # 不传 session_id 就整库重建，用于第一次升级后一次性生成
            targets = [session_id] if session_id else list(plugin.storage.universal.keys())
            rebuilt = 0
            for sid in targets:
                result = plugin.persona.rebuild(
                    sid, plugin.storage.universal.get(sid, []), force=True
                )
                if result.get("changed"):
                    rebuilt += 1
            return jsonify(
                {
                    "success": True,
                    "data": {"rebuilt": rebuilt, "total": len(targets)},
                    "message": f"已重新生成 {rebuilt} 份人格档案",
                }
            )
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
            if str(body.get("source") or "") != "manual":
                plugin.manager.learner.refresh_persona(session_id)
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
                "pins": plugin.storage.pinned_sessions(),
                "metrics": plugin.storage.metrics,
            },
        }
    )

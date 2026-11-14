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
from .core.learn_gate import is_private_session, learn_enabled as learn_gate_enabled
from .core.facts import (
    KIND_LABEL,
    PRUNE_KEEP,
    STATUS_ACTIVE,
    STATUS_LABEL,
    TIER_LABEL,
    TIER_ORDER,
    tier_of as TIER_OF,
)
from .core.settings import ACTIVE_PROFILES
from .core.storage import MANUAL_MAX_PER_SESSION, trait_score
from .core.tone import clean_text, split_segments
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
        (f"/{plugin.plugin_name}/bond", api_bond, ["GET", "POST"], "关系状态"),
        (f"/{plugin.plugin_name}/tone", api_tone, ["POST"], "输出层预览"),
        (f"/{plugin.plugin_name}/facts", api_facts, ["GET", "POST"], "事实记忆"),
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
                # 折叠分组（关系状态 / 输出层）的当前值。扁平快照里没有它们，
                # 但面板要用这些值当调试开关的初始状态
                "groups": plugin.settings.groups_snapshot(),
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
                # 主动回复的三档预设。前端拿它渲染「一键切换」那排按钮，
                # 只有服务端这一份定义，避免两边写死两套数值对不上
                "active_profiles": ACTIVE_PROFILES,
                # 概览页要显示「主人是谁 / 学不学私聊」——这两件事影响四个模块，
                # 却藏在配置里，用户回到面板第一眼就该看见
                "owners": sorted(plugin.settings.owners()),
                "learn_group_enabled": bool(plugin.settings.get("learn_group_enabled")),
                "learn_private_enabled": bool(plugin.settings.get("learn_private_enabled")),
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


def _last_msg_at(storage, session_id: str) -> float:
    msgs = storage.chat_history.get(session_id) or []
    return max((float(m.get("time") or 0) for m in msgs), default=0.0)


def _inactive_days(storage, session_id: str) -> float | None:
    """多久没新消息（天）。没记录返回 None，别和「刚聊过」混成一个 0。"""
    last = _last_msg_at(storage, session_id)
    if not last:
        return None
    return round(max(0.0, (time.time() - last) / 86400.0), 1)


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

    # 「学过的」和「聊过天的」都要列出来。
    #
    # 只列学过的会有一个死角：私聊默认不学，于是它连列表都进不去——
    # 用户永远没法在面板上给某个私聊点「开始学习」。
    # 并上 chat_history 之后，聊过天的私聊会以「0 条」出现，正好能开。
    learned_ids = set(storage.universal.keys())
    history_ids = set(storage.chat_history.keys())
    known_ids = learned_ids | history_ids
    sessions = [session_id] if session_id else list(known_ids)
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
                # 群聊 / 私聊在前端分成两个列表，判定只用这一处
                "is_private": is_private_session(sid),
                # 这个会话现在学不学（含全局开关 + 主人私聊例外）
                "learn_enabled": learn_gate_enabled(plugin.settings, storage, sid),
                # 三态：None = 跟随全局默认，True/False = 用户手动设过
                "learn_override": storage.learn_override(sid),
                "counts": counts,
                "total": len(all_traits),
                "manual": manual_count,
                # 学过 / 只聊过（还没学）。前端用它决定 0 条的空组要不要显示：
                # 群聊列表里过滤掉纯空壳，私聊列表里保留（那是开启学习的入口）
                "learned": sid in learned_ids,
                "has_history": sid in history_ids,
                # 最后一条消息的时间：面板用它标「可能已经不在了」。
                # 插件拿不到平台的群列表，只能靠「多久没人说话」推测
                "last_message_at": _last_msg_at(storage, sid),
                "inactive_days": _inactive_days(storage, sid),
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
                # 学习库拆成群聊 / 私聊两条，前端按这个开关显示「默认开 / 默认关」
                "learn_group_enabled": bool(plugin.settings.get("learn_group_enabled")),
                "learn_private_enabled": bool(plugin.settings.get("learn_private_enabled")),
                "owners": sorted(plugin.settings.owners()),
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

    # ── 删除与清理：置顶的一律不能删 ──
    #
    # 用户的规则很明确：「置顶后一律默认不能删除」。把置顶当保护标记用，
    # 比再加一个「锁定」开关好——用户不会为了防误删去学一套新概念。
    def _trait_item(sid: str, text: str):
        for item in list(storage.universal.get(sid, [])) + list(
            storage.manual.get(sid, [])
        ):
            if str(item.get("content")) == text:
                return item
        return None

    if action in ("delete_trait", "drop_session", "purge_inactive"):
        if action == "purge_inactive":
            days = max(1, int(body.get("days") or 30))
            targets = storage.inactive_sessions(days)
            dropped: list[str] = []
            kept: list[str] = []
            for sid in targets:
                if storage.is_pinned(sid):
                    kept.append(display_name(sid))
                    continue
                if await storage.clear_session(sid, scope="all"):
                    dropped.append(display_name(sid))
            await storage.flush()
            if not dropped:
                return jsonify(
                    {
                        "success": True,
                        "message": f"没有可清理的（{len(kept)} 个置顶的已跳过）",
                        "data": {"dropped": [], "kept": kept},
                    }
                )
            return jsonify(
                {
                    "success": True,
                    "message": f"已清理 {len(dropped)} 个 {days} 天没消息的会话"
                    + (f"，跳过 {len(kept)} 个置顶的" if kept else ""),
                    "data": {"dropped": dropped, "kept": kept},
                }
            )

        if not session_id:
            return jsonify({"success": False, "message": "缺少 session_id"})
        if storage.is_pinned(session_id):
            return jsonify(
                {
                    "success": False,
                    "message": "这个会话已置顶，置顶的一律不能删。先取消置顶再来。",
                }
            )
        if action == "drop_session":
            # 置顶的一律不能删——前端已经把按钮藏了，这里再挡一次：
            # 浏览器可能开着旧页面，接口也可能被直接调
            if storage.is_pinned(session_id):
                return jsonify(
                    {
                        "success": False,
                        "message": "这个会话已置顶，置顶的一律不能删。先取消置顶再来。",
                    }
                )
            await storage.clear_session(session_id, scope="all")
            await storage.flush()
            return jsonify(
                {"success": True, "message": f"已删除 {display_name(session_id)} 的全部学习数据"}
            )

        content = str(body.get("content") or "")
        if not content:
            return jsonify({"success": False, "message": "缺少要删除的内容"})
        item = _trait_item(session_id, content)
        if item is None:
            return jsonify({"success": False, "message": "没找到这条"})
        if item.get("pinned"):
            return jsonify(
                {"success": False, "message": "这条已钉住，钉住的一律不能删。先取消钉住再来。"}
            )
        ok = await storage.delete_trait(session_id, content)
        await storage.flush()
        if ok:
            try:
                plugin.manager.learner.refresh_persona(session_id)
            except Exception:  # noqa: BLE001
                pass
        return jsonify({"success": ok, "message": "已删除" if ok else "没找到这条"})

    # 学习开关是「会话级」的，没有 content——必须放在下面那条校验之前，
    # 否则会被「缺少 content」挡掉
    if action in ("learn_on", "learn_off"):
        if not session_id:
            return jsonify({"success": False, "message": "缺少 session_id"})
        await storage.set_override(session_id, learn=(action == "learn_on"))
        await storage.flush()
        return jsonify(
            {
                "success": True,
                "message": "已开始学习这个会话" if action == "learn_on" else "已停止学习这个会话",
            }
        )

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
        if action == "apply_active_profile":
            # 一键切「安静 / 平衡 / 活跃」。这几个参数是互相咬合的，
            # 单独改哪个都看不出效果，所以整档一起写
            profile_id = str(body.get("profile") or "")
            preset = next(
                (p for p in ACTIVE_PROFILES if p["id"] == profile_id), None
            )
            if preset is None:
                return jsonify({"success": False, "message": f"不认识的预设: {profile_id}"})
            applied: dict[str, Any] = {}
            for key, value in preset["values"].items():
                applied[key] = await plugin.settings.set_runtime(key, value)
            return jsonify(
                {
                    "success": True,
                    "message": f"已切到「{preset['label']}」：{preset['desc']}",
                    "data": applied,
                }
            )
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
    sender_id = str(body.get("sender_id") or "")
    injector = plugin.manager.injector
    try:
        strength = injector.strength_of(active=active)
        text = await injector.build_injection_text(
            session_id, message, active=active
        )
        # 关系段是 hooks 里单独挂的，不走 injector。预览不带上它，
        # 用户看到的就和实际注入不是一回事了
        relation = ""
        if sender_id:
            try:
                relation = (
                    plugin.bond.build_text(
                        sender_id,
                        is_group=":GroupMessage:" in session_id,
                    )
                    or ""
                )
            except Exception as e:  # noqa: BLE001 - 预览里少一段不该算失败
                logger.debug(f"[群聊记忆] 预览关系段失败: {e}")
        length_rule = injector.build_length_rule(active) or ""
        combined = "\n\n".join(p for p in (text or "", relation, length_rule) if p)
        return jsonify(
            {
                "success": True,
                "data": {
                    "text": text or "",
                    "relation": relation,
                    "length_rule": length_rule,
                    "combined": combined,
                    "would_inject": bool(combined),
                    "should_inject": injector.should_inject(session_id),
                    "active": active,
                    "strength": strength,
                    "override_probability": injector.override_probability(strength),
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


# ── GET/POST /bond ──────────────────────────────────────────────────────


def _bond_rows(plugin) -> list[dict]:
    """把 bonds.json 全部转成面板能直接渲染的行。

    **一行 = 一个人**（按 QQ 号唯一），不再是「某个群里的某个人」。
    ``last_umo`` 只用来判「最近在哪聊的」和决定阶段封顶，不参与识别。

    排序：主人 → 手动置顶 → 最近聊过。主人不写进数据、按配置实时判，
    这样改了 owner_ids 立刻生效，也不在库里留会过期的身份。

    单条算不出来就跳过它，不让一条脏数据把整页拖挂。
    """
    store = plugin.bond
    rows: list[dict] = []
    for key, raw in list(plugin.storage.bonds.items()):
        if not isinstance(raw, dict):
            continue
        sender_id = str(key).strip()
        # 纯 QQ 号才认。带竖线的是没迁干净的旧键，跳过等下次迁移
        if not sender_id or "|" in sender_id:
            continue
        last_umo = str(raw.get("last_umo") or "")
        is_group = ":GroupMessage:" in last_umo
        try:
            info = store.summary(sender_id, is_group=is_group)
            inject_text = store.build_text(sender_id, is_group=is_group) or ""
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[群聊记忆] 面板跳过一条关系记录 {key}: {e}")
            continue
        rows.append(
            {
                "key": sender_id,
                "session": last_umo,
                "session_label": display_name(last_umo) if last_umo else "—",
                "sender_id": sender_id,
                "sender_name": str(raw.get("sender_name") or ""),
                "is_group": is_group,
                "last_talk_text": store.format_since(info.get("last_talk", 0)),
                "inject_text": inject_text,
                **info,
            }
        )
    rows.sort(
        key=lambda r: (
            1 if r.get("is_owner") else 0,
            1 if r.get("pinned") else 0,
            r.get("last_talk") or 0,
        ),
        reverse=True,
    )
    return rows


async def api_bond(plugin):
    """关系状态：GET 看全部，POST 改称呼 / 重置 / 删除。

    面板改完立刻 flush，不等 5 秒防抖——用户点完按钮就该落盘，
    否则马上刷新会看到旧值。
    """
    if (request.method or "").upper() == "GET":
        cfg = plugin.settings.group("bond")
        rows = _bond_rows(plugin)
        groups = {
            "total": len(rows),
            "familiar_or_more": sum(
                1 for r in rows if r.get("stage") in ("familiar", "close", "intimate")
            ),
            "with_nickname": sum(1 for r in rows if r.get("nickname")),
            "pinned": sum(1 for r in rows if r.get("pinned")),
            # 负好感和「久没联系」这两类，面板上要能一眼看出有几个
            "hostile": sum(1 for r in rows if r.get("stage") == "hostile"),
            "idle": sum(1 for r in rows if (r.get("idle_days") or 0) >= 7),
        }
        return jsonify(
            {
                "success": True,
                "data": {
                    "enabled": bool(cfg.get("enabled")),
                    "settings": cfg,
                    "items": rows,
                    "groups": groups,
                },
            }
        )

    body = await request.get_json(silent=True) or {}
    action = str(body.get("action") or "")
    # 关系现在按 QQ 号唯一，key 就是 QQ 号本身，不再拆 umo|sender
    key = str(body.get("key") or "").strip()
    try:
        if action == "set_nickname":
            # 手动设称呼（支持多个，逗号分隔）。留空等于清除
            if not key:
                return jsonify({"success": False, "message": "记录键不对"})
            saved = plugin.bond.set_nickname(key, str(body.get("nickname") or ""))
            await plugin.storage.flush()
            names = [x.strip() for x in re.split(r"[,，、;；/\s]+", saved) if x.strip()]
            return jsonify(
                {
                    "success": True,
                    "data": {"nickname": saved, "names": names},
                    "message": (
                        f"已设为 {'、'.join(names)}" if names else "称呼已清除，回落到平台昵称"
                    ),
                }
            )

        if action == "clear_nickname":
            if not key or "|" in key:
                return jsonify({"success": False, "message": "记录键不对"})
            plugin.bond.set_nickname(key, "")
            await plugin.storage.flush()
            return jsonify({"success": True, "message": "称呼已清除"})

        if action in ("pin", "unpin"):
            if not key or "|" in key:
                return jsonify({"success": False, "message": "记录键不对"})
            pinned = plugin.bond.set_pinned(key, action == "pin")
            await plugin.storage.flush()
            return jsonify(
                {
                    "success": True,
                    "pinned": pinned,
                    "message": "已置顶" if pinned else "已取消置顶",
                }
            )

        if action == "set_intimacy":
            # 手动改数值。只改亲密度，不动轮次和认识天数——
            # 「聊过多少轮」是历史事实，人工改数值不该顺手伪造它
            try:
                value = float(body.get("value"))
            except (TypeError, ValueError):
                return jsonify({"success": False, "message": "数值要填个数字"})
            if not key or "|" in key:
                return jsonify({"success": False, "message": "记录键不对"})
            info = plugin.bond.set_intimacy(key, value)
            if not info:
                return jsonify({"success": False, "message": "这条关系不存在"})
            await plugin.storage.flush()
            return jsonify(
                {
                    "success": True,
                    "intimacy": info.get("intimacy"),
                    "stage": info.get("stage"),
                    "stage_label": info.get("stage_label"),
                    "message": f"已设为 {info.get('intimacy')} 点 · {info.get('stage_label')}",
                }
            )

        if action == "reset":
            raw = plugin.storage.bond_of(key)
            if not isinstance(raw, dict) or not raw:
                return jsonify({"success": False, "message": "这条关系不存在"})
            record = dict(raw)
            # 只归零「熟度」，first_talk 保留：重置的是亲密进度，
            # 「认识多久」是事实，不该被一次点击抹掉
            record["turns"] = 0
            record["intimacy"] = 0
            record["stage"] = "stranger"
            plugin.storage.set_bond(key, record)
            await plugin.storage.flush()
            return jsonify({"success": True, "message": "亲密度和轮次已归零"})

        if action == "delete":
            if not plugin.storage.delete_bond(key):
                return jsonify({"success": False, "message": "这条关系不存在"})
            await plugin.storage.flush()
            return jsonify({"success": True, "message": "已删除这条关系"})

        if action == "clear_all":
            count = len(plugin.storage.bonds)
            plugin.storage.bonds.clear()
            plugin.storage._mark("bonds")
            await plugin.storage.flush()
            return jsonify({"success": True, "message": f"已清空 {count} 条关系记录"})

        if action == "clear_session":
            # 关系已经按 QQ 号全局统一，清某个会话不再能代表「忘掉这些人」
            return jsonify(
                {
                    "success": False,
                    "message": "关系已按 QQ 号统一，请在关系列表里逐条删除或用「清空全部」",
                }
            )

        return jsonify({"success": False, "message": f"不认识的操作：{action}"})
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[群聊记忆] 关系面板操作失败 {action}: {e}")
        return jsonify({"success": False, "message": str(e)})


# ── POST /tone ──────────────────────────────────────────────────────────


async def api_tone(plugin):
    """输出层预览：把一段回复丢进去，看清理和拆条的实际结果。

    纯计算，不写任何东西。面板上的开关可以临时覆盖配置，
    这样能在不改配置的情况下逐项对比「开了会怎样」。
    """
    body = await request.get_json(silent=True) or {}
    text = str(body.get("text") or "")
    if not text.strip():
        return jsonify({"success": False, "message": "先粘一段回复进来"})

    try:
        cfg = dict(plugin.settings.group("tone"))
        for field in ("clean", "squeeze", "markdown", "segment"):
            if field in body:
                cfg[field] = bool(body[field])
        for field in ("segment_chars", "segment_parts"):
            if field in body:
                try:
                    cfg[field] = int(body[field])
                except (TypeError, ValueError):
                    pass

        cleaned = clean_text(text, cfg)
        if cfg.get("segment"):
            parts = split_segments(
                cleaned,
                int(cfg.get("segment_chars") or 60),
                int(cfg.get("segment_parts") or 4),
            )
        else:
            parts = [cleaned]
        # 含代码或链接时 split_segments 不拆，这里标出来，
        # 免得用户以为「开了拆条却没生效」是坏了
        blocked = (
            cfg.get("segment")
            and len(parts) == 1
            and any(t in cleaned for t in ("```", "`", "http://", "https://"))
        )
        return jsonify(
            {
                "success": True,
                "data": {
                    "original": text,
                    "cleaned": cleaned,
                    "segments": [
                        {"text": p, "chars": len(p)} for p in parts if p.strip()
                    ],
                    "segment_blocked": bool(blocked),
                    "chars_before": len(text),
                    "chars_after": len(cleaned),
                    "chars_saved": len(text) - len(cleaned),
                    "config": cfg,
                },
            }
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[群聊记忆] 输出层预览失败: {e}")
        return jsonify({"success": False, "message": str(e)})


# ── GET/POST /facts ─────────────────────────────────────────────────────


# 事实条目的排序：已否决/已归档沉底 → 钉住的浮顶 → 按档位 → 按最近提到。
#
# 为什么钉住排在档位前面：钉住是用户亲手按的，是「我就要这条在最上面」；
# 档位是算法猜的。用户的意图优先于算法，这是全插件统一的口径
# （学习库的置顶分组、关系面板的置顶也是这么排的）。
_FACT_TIER_RANK = {"core": 0, "deep": 1, "shallow": 2}


def _fact_rank(row: dict):
    status = str(row.get("status_key") or "")
    dead = 1 if status in ("rejected", "archived") else 0
    return (
        dead,
        0 if row.get("pinned") else 1,
        _FACT_TIER_RANK.get(str(row.get("tier") or "shallow"), 2),
        -float(row.get("last_seen") or 0),
    )


def _profile_of(plugin, sender_id: str) -> dict:
    """面板用的画像快照。画像模块没装/坏了都返回空结构，不炸整页。"""
    try:
        store = getattr(plugin, "profile", None)
        if store is None:
            return {}
        return store.snapshot(sender_id)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[群聊记忆] 读取画像失败 {sender_id}: {e}")
        return {}


def _fact_buckets(plugin) -> list[dict]:
    """把 facts.json 摊平，供面板渲染。**一个人一行**。

    1.7.x 起事实按 QQ 号唯一存（不再按群分账），所以这里一个人只有一行，
    「最近在哪个群」只是展示信息——从关系记录里的 ``last_umo`` 取，
    取不到就留空，不影响任何逻辑。

    空桶不显示：事实被清空后键还在，列表里留一行「0 条」没有意义。
    """
    rows: list[dict] = []
    for key, items in list(plugin.storage.facts.items()):
        if not isinstance(items, list) or not items:
            continue
        # 迁移之后不该再有带竖线的键；万一有（迁移失败），跳过而不是崩
        if "|" in str(key):
            logger.debug(f"[群聊记忆] 面板跳过未迁移的事实桶 {key}")
            continue
        sender_id = str(key).strip()
        if not sender_id:
            continue
        try:
            info = plugin.facts.summary("", sender_id)
        except Exception as e:  # noqa: BLE001 - 一个桶坏了不该拖垮整页
            logger.debug(f"[群聊记忆] 面板跳过事实桶 {key}: {e}")
            continue
        bond = {}
        # 档位用**不封顶**的口径（``stage_now`` 默认），和「能存多少条」一致。
        # 关系面板那边是「当前场合的亲疏」（群聊封顶在熟悉），这里要的是
        # 「跟他整体多熟」——不然会出现「阶段显示陌生、上限却是老友档」的矛盾。
        stage = ""
        try:
            record = plugin.bond.record(sender_id)
            umo = str(record.get("last_umo") or "")
            bond = plugin.bond.summary(sender_id, is_group=":GroupMessage:" in umo)
            stage = str(plugin.bond.stage_now(sender_id) or "")
        except Exception:  # noqa: BLE001
            umo = ""
            bond = {}
        # 群聊口径的档位在事实面板里没用（存储是全局的），统一换成不封顶的
        if stage:
            bond["stage"] = stage
        is_owner = bool(bond.get("is_owner"))
        try:
            # 主人也会返回值（单独清理的保留量）。「一键清理跳过主人」这个判断
            # 在 ``prune_all`` 里做，不靠 0 来暗号——用 0 表示「不清理」的话，
            # 面板就没法显示「主人单独清理后留几条」
            keep_target = plugin.facts.keep_target_of(sender_id, stage=stage or None)
        except Exception:  # noqa: BLE001
            keep_target = 0
        rows.append(
            {
                "key": sender_id,
                "session": umo,
                "session_label": display_name(umo) if umo else "—",
                "sender_id": sender_id,
                "sender_name": str(bond.get("nickname") or ""),
                "is_owner": is_owner,
                # 人级别的置顶（面板展示顺序用，不进注入）
                "pinned": plugin.storage.is_fact_pinned(sender_id),
                "stage": stage,
                "stage_label": str(bond.get("stage_label") or ""),
                # 一键清理后这个人会剩几条。0 = 不清理（主人）
                "keep_target": keep_target,
                "intimacy": bond.get("intimacy", 0),
                "total": info.get("total", 0),
                # 「还记着几条」= 除被否决以外的。一键清理把条目标成 rejected，
                # 它们还在文件里留档，但不算「记着」——统计和上限都要用它，
                # 否则清完一遍显示「还是 12 条」，看起来像没生效
                "alive": max(0, info.get("total", 0) - info.get("rejected", 0)),
                "rejected": info.get("rejected", 0),
                "active": info.get("active", 0),
                "pending": info.get("pending", 0),
                "suspect": info.get("suspect", 0),
                "archived": info.get("archived", 0),
                "limit": info.get("limit", 0),
                "core_quota": plugin.facts.core_quota_of(sender_id, stage=stage or None),
                "core_count": sum(
                    1
                    for i in (info.get("items") or [])
                    if str(i.get("tier") or "") == "core"
                ),
                # 深层条数：卡片头上跟「核心 x/N」并排显示。
                # 只有核心一个数字时，用户看不出中间那层到底有没有东西
                "deep_count": sum(
                    1
                    for i in (info.get("items") or [])
                    if TIER_OF(i) == "deep"
                    and str(i.get("status") or "") not in ("rejected", "archived")
                ),
                # 个人画像：一段「这个人是谁」，字数上限按好感档位。
                # 面板要能看、能手改，所以连 quota 一起给
                "profile": _profile_of(plugin, sender_id),
                "items": sorted(
                    [
                    {
                        "id": str(i.get("id") or ""),
                        "text": str(i.get("text") or ""),
                        "kind": KIND_LABEL.get(str(i.get("kind") or ""), "事实"),
                        "status": STATUS_LABEL.get(str(i.get("status") or ""), "?"),
                        # 面板按状态筛选要用原始键：中文标签会随文案改动而失效
                        "status_key": str(i.get("status") or ""),
                        "active": str(i.get("status") or "") == STATUS_ACTIVE,
                        "importance": i.get("importance", 3),
                        "created": i.get("created", 0),
                        "last_seen": i.get("last_seen", 0),
                        "pinned": bool(i.get("pinned")),
                        # 三档：核心 / 深层 / 浅层。老数据里的 normal 由
                        # tier_of 归一成浅层，面板不需要认识这个历史包袱
                        "tier": TIER_OF(i),
                        "tier_label": TIER_LABEL.get(TIER_OF(i), "浅层"),
                        "tier_manual": bool(i.get("tier_manual")),
                        # 熟练度（0~100）：分档的依据，也是面板上那个百分比
                        "prof": plugin.facts.proficiency(i),
                        "seen": i.get("seen", 1),
                        # 同一件事的另一种说法（模糊合并时的留档）
                        "variants": list(i.get("variants") or []),
                        # 这条还剩多少「记得」。浅层天数（默认 15）归零，面板上直接看得到
                        "keep": round(
                            plugin.facts.keep_ratio(
                                max(0.0, (time.time() - float(i.get("last_seen") or 0)) / 86400.0)
                            ),
                            2,
                        ),
                    }
                        for i in info.get("items") or []
                    ],
                    key=_fact_rank,
                ),
            }
        )
    # 排序：置顶的人按用户设定的先后排最前，其余按条数多少。
    # 置顶只影响展示顺序——和事实的注入权重、档位都无关
    order = {qq: i for i, qq in enumerate(plugin.storage.pinned_facts())}
    rows.sort(
        key=lambda r: (
            0 if r["key"] in order else 1,
            order.get(r["key"], 0),
            -r["total"],
            -len(r["items"]),
        )
    )
    return rows


async def api_facts(plugin):
    """事实记忆：GET 看全部，POST 删除 / 手动添加 / 清空某个人的。"""
    if (request.method or "").upper() == "GET":
        cfg = plugin.settings.group("facts")
        # 打开面板时把全库档位按熟练度重算一遍：老数据（两档时代的 normal）
        # 不重算就全显示成浅层。只算内存、有变化才写盘，几千条也就几毫秒
        try:
            plugin.facts.refresh_all()
        except Exception as e:  # noqa: BLE001 - 算档位失败不该让整页 500
            logger.debug(f"[群聊记忆] 刷新记忆档位失败（可忽略）: {e}")
        rows = _fact_buckets(plugin)
        return jsonify(
            {
                "success": True,
                "data": {
                    "enabled": bool(cfg.get("enabled")),
                    "settings": cfg,
                    "items": rows,
                    "stats": {
                        # 「人」不是「桶」：按 QQ 号唯一之后一人一行
                        "people": len(rows),
                        "facts": sum(r["total"] for r in rows),
                        "active": sum(r["active"] for r in rows),
                        "suspect": sum(r["suspect"] for r in rows),
                        "archived": sum(r["archived"] for r in rows),
                        # 「还记着多少」不含已否决：清理过的条目留档但不算数
                        "alive": sum(r["alive"] for r in rows),
                        # 一键清理后会剩多少（**不含主人**——一键清理本来就跳过主人）。
                        # 面板上直接写「现在 39 条 → 清理后 18 条」，点之前就知道代价
                        "keep_after": sum(
                            r["alive"] if r["is_owner"]
                            else (min(r["alive"], r["keep_target"]) if r["keep_target"] else r["alive"])
                            for r in rows
                        ),
                        "owner_facts": sum(r["alive"] for r in rows if r["is_owner"]),
                        # 三档各有多少条。面板顶部要显示「核心 3 · 深层 12 · 浅层 27」
                        "tiers": {
                            t: sum(
                                1
                                for r in rows
                                for i in r["items"]
                                if i["tier"] == t
                                and i["status_key"] not in ("rejected", "archived")
                            )
                            for t in TIER_ORDER
                        },
                        # 主人单独清理后会剩多少（没设主人就没有这个数）
                        "owner_after": sum(
                            min(r["alive"], r["keep_target"]) if r["keep_target"] else r["alive"]
                            for r in rows
                            if r["is_owner"]
                        ),
                        "has_owner": any(r["is_owner"] for r in rows),
                        "owners": sorted(plugin.settings.owners()),
                    },
                },
            }
        )

    body = await request.get_json(silent=True) or {}
    action = str(body.get("action") or "")
    key = str(body.get("key") or "")
    try:
        # 1.7.x 起事实按 QQ 号唯一存，键就是 QQ 号。还兼容一下旧的「会话|用户」，
        # 免得用户浏览器开着一份旧页面，点删除时报「记录键不对」。
        umo, sep, sender_id = key.partition("|")
        if not sep:
            sender_id = key.strip()
            umo = ""
        if action in ("delete", "add", "clear") and not sender_id:
            return jsonify({"success": False, "message": "记录键不对"})

        if action == "delete":
            fact_id = str(body.get("id") or "")
            if not fact_id:
                return jsonify({"success": False, "message": "缺少条目编号"})
            removed = plugin.facts.forget(umo, sender_id, fact_id)
            await plugin.storage.flush()
            return jsonify(
                {"success": bool(removed), "message": "已删除" if removed else "没找到这条"}
            )

        if action == "add":
            record = plugin.facts.remember(umo, sender_id, str(body.get("text") or ""))
            await plugin.storage.flush()
            if record is None:
                return jsonify(
                    {"success": False, "message": "没记上：可能太长，或含敏感信息"}
                )
            return jsonify({"success": True, "message": f"已记下：{record.get('text')}"})

        if action == "clear":
            removed = plugin.facts.forget(umo, sender_id, "")
            await plugin.storage.flush()
            return jsonify({"success": True, "message": f"已清掉 {removed} 条"})

        if action == "set_tier":
            # 手动升降级。``dir``：1 升一级 / -1 降一级 / 0 交回自动。
            # 老面板发的是 ``core: true/false``，这里兼容一下——
            # 用户浏览器可能还开着一份缓存页面，不该点了没反应。
            fact_id = str(body.get("id") or "")
            if not fact_id:
                return jsonify({"success": False, "message": "缺少条目编号"})
            if "dir" in body:
                direction = int(body.get("dir") or 0)
            elif "core" in body:
                direction = 1 if body.get("core") else 0
            else:
                direction = 0
            rec = plugin.facts.set_tier(sender_id, fact_id, direction)
            if rec is None:
                return jsonify({"success": False, "message": "没找到这条记忆"})
            await plugin.storage.flush()
            label = TIER_LABEL.get(TIER_OF(rec), "浅层")
            return jsonify(
                {
                    "success": True,
                    "message": (
                        f"已设为「{label}」记忆" if direction else f"已交回自动判定（{label}）"
                    ),
                    "data": {
                        "id": fact_id,
                        "tier": TIER_OF(rec),
                        "tier_label": label,
                        "manual": bool(rec.get("tier_manual")),
                    },
                }
            )

        if action == "pin_user":
            # 人物级别的置顶：只影响面板顺序，和学习库的置顶同一套
            changed = await plugin.storage.set_fact_pin(sender_id, bool(body.get("pinned")))
            if changed:
                await plugin.storage.flush()
            return jsonify(
                {
                    "success": True,
                    "data": {"key": sender_id, "pinned": bool(body.get("pinned"))},
                    "message": "已置顶，排在列表最前" if body.get("pinned") else "已取消置顶",
                }
            )

        if action == "pin":
            # 事实条目的钉住 / 取消。钉住的不淘汰、不归档，注入加分
            fact_id = str(body.get("id") or "")
            if not fact_id:
                return jsonify({"success": False, "message": "缺少条目编号"})
            rec = plugin.facts.set_pinned(sender_id, fact_id, bool(body.get("pinned")))
            if rec is None:
                return jsonify({"success": False, "message": "没找到这条记忆"})
            await plugin.storage.flush()
            return jsonify(
                {
                    "success": True,
                    "message": "已钉住，注入时优先，且不会被自动清掉"
                    if rec.get("pinned")
                    else "已取消钉住",
                    "data": {"id": fact_id, "pinned": bool(rec.get("pinned"))},
                }
            )

        if action == "set_profile_chars":
            # 单人画像字数上限。0 = 恢复档位默认
            store = getattr(plugin, "profile", None)
            if store is None:
                return jsonify({"success": False, "message": "画像模块未加载"})
            try:
                want = int(body.get("chars") or 0)
            except (TypeError, ValueError):
                return jsonify({"success": False, "message": "字数要填数字"})
            value = store.set_chars(sender_id, want)
            await plugin.storage.flush()
            snap = store.snapshot(sender_id)
            return jsonify(
                {
                    "success": True,
                    "message": (
                        f"已设为 {value} 字（不填回 0 就恢复档位默认 {snap.get('stage_quota')} 字）"
                        if value
                        else f"已恢复档位默认 {snap.get('stage_quota')} 字"
                    ),
                    "data": snap,
                }
            )

        if action == "save_profile":
            # 人工补充段：用户亲手写的比模型总结的可靠，注入时排在最前，
            # 而且自动重写不会覆盖它
            store = getattr(plugin, "profile", None)
            if store is None:
                return jsonify({"success": False, "message": "画像模块未加载"})
            store.save_manual(sender_id, str(body.get("manual") or ""))
            await plugin.storage.flush()
            return jsonify(
                {
                    "success": True,
                    "message": "已保存人工补充（注入时排在最前）",
                    "data": store.snapshot(sender_id),
                }
            )

        if action == "rewrite_profile":
            # 手动重新生成。会发一次请求，所以按钮上写了「约 800 token」
            store = getattr(plugin, "profile", None)
            if store is None:
                return jsonify({"success": False, "message": "画像模块未加载"})
            quota = store.quota_of(sender_id)
            if quota <= 0:
                return jsonify(
                    {
                        "success": False,
                        "message": "这个人的画像名额是 0（不待见档，或画像功能关着）",
                    }
                )
            ok = await store.maybe_rewrite(sender_id, force=True)
            await plugin.storage.flush()
            if not ok:
                return jsonify(
                    {
                        "success": False,
                        "message": "没写成：事实少于 2 条，或今天的重写次数用完了",
                    }
                )
            return jsonify(
                {
                    "success": True,
                    "message": f"已重新生成（上限 {quota} 字）",
                    "data": store.snapshot(sender_id),
                }
            )

        if action == "merge":
            # 整理合并：把库里「同一件事的不同说法」并成一条。
            # 纯本地 2-gram 比对，不发请求、不花 token。
            dry = bool(body.get("dry_run"))
            res = plugin.facts.merge_duplicates(sender_id or None, dry_run=dry)
            if not dry:
                await plugin.storage.flush()
            if not res["merged"]:
                return jsonify(
                    {
                        "success": True,
                        "message": "没有发现可以合并的重复记忆",
                        "data": res,
                    }
                )
            return jsonify(
                {
                    "success": True,
                    "message": (
                        f"{'预计' if dry else '已'}合并 {res['merged']} 条重复记忆"
                        f"（涉及 {res['people']} 人，被合并的进了「已归档」）"
                    ),
                    "data": res,
                }
            )

        if action == "prune":
            # 单人清理：按这个人的档位瘦身到目标条数。
            # dry_run 只算不写——面板先弹一次「会清掉几条」再让用户确认。
            dry = bool(body.get("dry_run"))
            res = plugin.facts.prune(sender_id, dry_run=dry)
            if not dry:
                await plugin.storage.flush()
            if not res["removed"]:
                return jsonify(
                    {
                        "success": True,
                        "message": f"这个人现在 {res['before']} 条，没超过 {res['target']} 条的目标，不用清",
                        "data": res,
                    }
                )
            return jsonify(
                {
                    "success": True,
                    "message": (
                        f"{'预计' if dry else '已'}清掉 {res['removed']} 条："
                        f"{res['before']} → {res['after']} 条（目标 {res['target']} 条，"
                        "被清掉的在「已否决」里还能看到）"
                    ),
                    "data": res,
                }
            )

        if action == "prune_owner":
            # 只清主人。和一键清理分开：那个是日常维护（跳过主人），
            # 这个是用户主动说「主人这份也收一收」
            dry = bool(body.get("dry_run"))
            res = plugin.facts.prune_owner(dry_run=dry)
            if not dry:
                await plugin.storage.flush()
            if not res["removed"]:
                # 两种「没清」要分开说：一种是压根没主人 / 没记忆（该去配置），
                # 一种是记忆没超过保留量（什么都不用做）。只说前者会误导用户
                if not res.get("owner_count"):
                    return jsonify(
                        {
                            "success": False,
                            "message": "还没有配置主人 QQ，或者主人还没有记忆。"
                            "去「参数 → 全局 → 主人 QQ」填一下。",
                        }
                    )
                return jsonify(
                    {
                        "success": True,
                        "message": (
                            f"主人现在 {res.get('alive', 0)} 条，没超过保留量 "
                            f"{res['keep']} 条，不用清"
                        ),
                        "data": res,
                    }
                )
            first = res["rows"][0] if res["rows"] else {}
            return jsonify(
                {
                    "success": True,
                    "message": (
                        f"{'预计' if dry else '已'}清理主人记忆："
                        f"{first.get('before', 0)} → {first.get('after', 0)} 条"
                        f"（保留 {res['keep']} 条，清掉 {res['removed']} 条）"
                    ),
                    "data": res,
                }
            )

        if action == "prune_all":
            dry = bool(body.get("dry_run"))
            res = plugin.facts.prune_all(dry_run=dry)
            if not dry:
                await plugin.storage.flush()
            if not res["removed"] and not res["purged"]:
                return jsonify(
                    {
                        "success": True,
                        "message": f"没有需要清理的：{res['skipped_owner']} 个主人已跳过",
                        "data": res,
                    }
                )
            parts = [f"{'预计' if dry else '已'}整理 {res['people']} 人"]
            if res["removed"]:
                parts.append(f"清掉 {res['removed']} 条")
            if res["purged"]:
                parts.append(f"删除超期归档 {res['purged']} 条")
            if res["skipped_owner"]:
                parts.append(f"跳过主人 {res['skipped_owner']} 个")
            return jsonify(
                {"success": True, "message": " · ".join(parts), "data": res}
            )

        return jsonify({"success": False, "message": f"不认识的操作：{action}"})
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[群聊记忆] 事实面板操作失败 {action}: {e}")
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
                "bonds": plugin.storage.bonds,
                "facts": plugin.storage.facts,
                "metrics": plugin.storage.metrics,
            },
        }
    )

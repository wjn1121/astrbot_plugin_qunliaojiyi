const $ = (id) => document.getElementById(id);

// AstrBot 4.29 的 bridge 主名是 AstrBotPluginView，AstrBotPluginPage 是保留别名；
// 两种都兼容，避免以后改名导致面板静默失效。
const Bridge = window.AstrBotPluginView || window.AstrBotPluginPage;

function esc(text) {
  const div = document.createElement("div");
  div.textContent = text ?? "";
  return div.innerHTML;
}
function escAttr(text) {
  return esc(text).replace(/"/g, "&quot;");
}

let pendingConfirm = null;
let lastStatus = null;
// 列表视图状态：搜索 / 分页 / 排序，翻页与搜索都走服务端
const view = { q: "", page: 1, sort: "traits", traitStatus: "", traitQuery: "", personaQuery: "" };
// 展开过的会话，刷新后恢复，避免「加完记忆一刷新就收起」的割裂感
const openSessions = new Set();
// 预览按哪一档算：主动插话用的强度和被动不是一回事
let previewActive = false;
// 置顶的会话，顺序由后端给（先加的在前面），前端只用不改
let pinnedIds = [];

function showToast(message) {
  const toast = $("toast");
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { toast.hidden = true; }, 3200);
}

// 插件页面在 sandbox iframe 里，原生 confirm 会被拦截，这里自绘确认框
function askConfirm(text) {
  return new Promise((resolve) => {
    pendingConfirm = resolve;
    $("confirmText").textContent = text;
    $("confirmModal").hidden = false;
  });
}
function closeConfirm(result) {
  $("confirmModal").hidden = true;
  const resolve = pendingConfirm;
  pendingConfirm = null;
  if (resolve) resolve(result);
}

// bridge 会把响应解包一层，这里同时兼容 {success,data} 与已解包数据
function unwrap(payload) {
  if (payload && typeof payload === "object" && "success" in payload && "data" in payload) {
    return payload.data ?? {};
  }
  return payload || {};
}

function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function fmtDuration(sec) {
  if (sec === null || sec === undefined) return "—";
  if (sec < 60) return `${sec} 秒`;
  if (sec < 3600) return `${Math.floor(sec / 60)} 分钟`;
  return `${(sec / 3600).toFixed(1)} 小时`;
}
// 金额：小于一分钱也别显示成 0.00，否则会误以为统计坏了
function fmtMoney(value) {
  const v = Number(value) || 0;
  if (v === 0) return "0.00";
  if (v < 0.01) return v.toFixed(4);
  return v.toFixed(2);
}
function fmtCount(value) {
  const n = Number(value) || 0;
  if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

// 和后端 utils.display_name 保持一致，前端自己拼是为了 known_ids 里只有 id 的情况
function nameOf(sessionId) {
  const parts = String(sessionId || "").split(":");
  if (parts.length >= 3) {
    const kind = parts[1].toLowerCase().startsWith("group") ? "群" : "私聊";
    return `${kind} ${parts[2]}`;
  }
  return sessionId || "未知会话";
}

function parseTriggers(raw) {
  return String(raw || "")
    .split(/[,，\s]+/)
    .map((x) => x.trim())
    .filter(Boolean);
}

// ── 左侧标签页 ──────────────────────────────────────────────────────

const TAB_KEY = "qunliao.tab";
const TAB_TEXT = {
  overview: "概览",
  library: "学习库",
  persona: "人格档案",
  usage: "Token 消费",
  settings: "参数",
  preview: "注入预览",
  sessions: "会话与特征",
};

function switchTab(tab) {
  if (!TAB_TEXT[tab]) tab = "overview";
  document.querySelectorAll(".pane").forEach((pane) => {
    pane.classList.toggle("on", pane.dataset.pane === tab);
  });
  document.querySelectorAll("#nav .nav-item").forEach((btn) => {
    btn.classList.toggle("on", btn.dataset.tab === tab);
  });
  // 人格档案要读磁盘文件，没必要每次刷新都扫一遍，切过去时再加载
  if (tab === "persona") ensurePersona();
  try {
    // 在 iframe 里，sessionStorage 可能被宿主的沙箱限制，失败就当没记住
    localStorage.setItem(TAB_KEY, tab);
  } catch (err) {
    /* 忽略 */
  }
}

function restoreTab() {
  let saved = "";
  try {
    saved = localStorage.getItem(TAB_KEY) || "";
  } catch (err) {
    saved = "";
  }
  switchTab(TAB_TEXT[saved] ? saved : "overview");
}

// ── 置顶（群号 / QQ 号排在最前，可任意多个）──────────────────────────

function isPinned(sessionId) {
  return pinnedIds.includes(sessionId);
}

async function togglePin(sessionId) {
  const next = !isPinned(sessionId);
  const ok = await manageWithMessage(
    { action: "set_pin", session_id: sessionId, pinned: next },
    next ? "已置顶，排在列表最前" : "已取消置顶",
  );
  // 顺序与分页都在服务端算，置顶后必须重拉，否则当前页里看不到刚置顶的那条
  if (ok) await load();
}

// ── 加载 ────────────────────────────────────────────────────────────

async function load() {
  try {
    const data = unwrap(await Bridge.apiGet("status", {
      q: view.q,
      page: view.page,
      sort: view.sort,
      page_size: 20,
    }));
    view.page = data.page || 1;
    lastStatus = data;
    pinnedIds = data.pinned_ids || [];
    $("version").textContent = `v${data.version || "?"} · 从群聊里增量积累说话风格，按语境零网络注入`;
    renderStats(data.stats || {}, data.manager || {});
    renderUsage(data);
    renderSettings(data.settings || {});
    renderSessions(data.sessions || []);
    fillPreviewSessions(data);
    renderPager(data);
    // 预览会话下拉刚填好，「本群今日搜索」要按它重算
    renderUsageSearch(data);
    await loadTraits();
  } catch (err) {
    console.error("加载状态失败:", err);
    showToast("加载失败，请查看日志");
  }
}

// ── 学习库（1.4.0 累积链路）──────────────────────────────────────────

let traitGroups = [];
// 用户手动折叠 / 展开过的分组。记下来是因为默认只展开「有待审」的组，
// 十几个群全展开等于没分组，但用户主动点开的那个不该被每次刷新收回去。
const traitGroupState = new Map();

const TRAIT_STATUS_TEXT = {
  pending: "待审",
  active: "已生效",
  archived: "已归档",
  rejected: "已否决",
};

async function loadTraits() {
  try {
    const data = unwrap(await Bridge.apiGet("traits", { status: view.traitStatus || "" }));
    traitGroups = data.groups || [];
    const stats = data.stats || {};
    const items = [
      ["待审", data.pending_total ?? 0],
      ["生效", stats.active ?? 0],
      ["归档", stats.archived ?? 0],
      ["否决", stats.rejected ?? 0],
      ["手动记忆", stats.manual ?? 0],
    ];
    const box = $("traitStats");
    if (box) {
      box.innerHTML = items
        .map(([k, v]) => `<div class="stat"><span>${esc(k)}</span><b>${esc(v)}</b></div>`)
        .join("");
    }
    // 侧栏角标：有待审时提示，不切过去也知道有事要做
    const pending = Number(data.pending_total ?? 0);
    const badge = $("navPending");
    if (badge) {
      badge.hidden = !pending;
      badge.textContent = pending > 99 ? "99+" : String(pending);
    }
    renderTraits(traitGroups);
  } catch (err) {
    console.error("加载学习库失败:", err);
  }
}

// 学习库按会话分组：同一个群 / 私聊的条目聚在一起，置顶的排最前。
// 分组本身始终渲染（哪怕当前筛选下为空），否则切状态时整组会「消失」，
// 用户容易以为数据被删了。
function renderTraits(groups) {
  const box = $("traits");
  if (!box) return;
  const query = (view.traitQuery || "").trim().toLowerCase();

  const visible = groups
    .map((g) => {
      // 搜群号 / 群名命中时整组原样保留：搜群号的人想看这个群里的全部内容，
      // 而不是一个标题留下、条目被内容过滤清空的空壳。
      const bySession = query && `${g.session_id} ${g.display_name}`.toLowerCase().includes(query);
      return { ...g, traits: bySession ? g.traits || [] : filterTraits(g, query), bySession };
    })
    .filter((g) => !query || g.traits.length || g.bySession);

  const allBtn = $("approveAllBtn");
  if (allBtn) {
    const hasPending = visible.some((g) => (g.counts && g.counts.pending) > 0);
    allBtn.style.display = hasPending ? "" : "none";
  }

  // 搜索时给个命中数，否则「怎么只剩这几个群」会让人以为数据丢了
  const note = $("traitSearchNote");
  if (note) {
    if (query) {
      const hits = visible.filter((g) => g.traits.length).length;
      note.hidden = false;
      note.textContent = `搜索「${view.traitQuery.trim()}」：命中 ${hits} 个群 / 共 ${groups.length} 个群，命中群里的条目已全部展开。`;
    } else {
      note.hidden = true;
    }
  }

  if (!visible.length) {
    box.innerHTML = `<p class="hint">${
      groups.length ? "没有匹配的条目。" : "还没有学到任何内容，先在群里聊几句。"
    }</p>`;
    return;
  }
  box.innerHTML = visible.map(groupCard).join("");
}

function filterTraits(group, query) {
  const traits = group.traits || [];
  if (!query) return traits;
  return traits.filter((t) =>
    [t.content, t.context, ...(t.triggers || []), ...(t.alias || [])]
      .join(" ")
      .toLowerCase()
      .includes(query),
  );
}

function groupOpen(group) {
  // 搜索时全部展开：收着的话搜到了也看不见，等于没搜
  if (view.traitQuery) return true;
  if (traitGroupState.has(group.session_id)) return traitGroupState.get(group.session_id);
  // 默认只展开有待审条目的组
  return (group.counts && group.counts.pending) > 0 && (group.traits || []).length > 0;
}

function groupCard(group) {
  const sid = escAttr(group.session_id);
  const counts = group.counts || {};
  const traits = group.traits || [];
  const tags = [];
  if (counts.pending) tags.push(`<span class="group-tag pending">待审 ${esc(counts.pending)}</span>`);
  if (group.manual) tags.push(`<span class="group-count">手动 ${esc(group.manual)}</span>`);
  const body = traits.length
    ? `<div class="trait-body">${traits.map(traitItem).join("")}</div>`
    : '<div class="trait-body"><p class="hint">这个筛选下没有条目。</p></div>';
  return `<details class="trait-group" data-gsid="${sid}"${groupOpen(group) ? " open" : ""}>
    <summary>
      <b>${group.pinned ? "📌 " : ""}${esc(group.display_name)}</b>
      <span class="group-count">${esc(traits.length)} / ${esc(group.total ?? traits.length)} 条</span>
      ${tags.join("")}
      <button type="button" class="pin-btn${group.pinned ? " on" : ""}" data-pact="pin" data-sid="${sid}">${
        group.pinned ? "已置顶" : "置顶"
      }</button>
    </summary>
    ${body}
  </details>`;
}

function traitItem(t) {
  const status = t.status || "active";
  const sid = escAttr(t.session_id || "");
  const content = escAttr(t.content || "");
  const situation = t.context ? `当${esc(t.context)}时` : "任何场合都成立";
  const alias = (t.alias || []).length
    ? `<div class="trait-alias">同义说法：${(t.alias || []).map((x) => esc(x)).join(" / ")}</div>`
    : "";
  const evidence = (t.evidence || []).map((e) => esc(e.text || "")).filter(Boolean);
  const evidenceHtml = evidence.length
    ? `<details class="trait-evidence"><summary>证据 ${evidence.length} 条</summary><ul>${evidence
        .map((x) => `<li>${x}</li>`)
        .join("")}</ul></details>`
    : `<span class="hint">暂无原文证据</span>`;
  const btns = [];
  if (status === "pending") {
    btns.push(`<button class="ghost" data-tact="approve" data-sid="${sid}" data-content="${content}">批准</button>`);
    btns.push(`<button class="ghost danger" data-tact="reject" data-sid="${sid}" data-content="${content}">否决</button>`);
  } else if (status === "active") {
    btns.push(
      `<button class="ghost" data-tact="${t.pinned ? "unpin" : "pin"}" data-sid="${sid}" data-content="${content}">${t.pinned ? "取消钉住" : "钉住"}</button>`,
    );
    btns.push(`<button class="ghost danger" data-tact="reject" data-sid="${sid}" data-content="${content}">否决</button>`);
  } else {
    btns.push(`<button class="ghost" data-tact="restore" data-sid="${sid}" data-content="${content}">恢复</button>`);
  }
  return `<div class="trait-row">
    <div class="trait-head">
      <span class="trait-status st-${esc(status)}">${esc(TRAIT_STATUS_TEXT[status] || status)}</span>
      <span class="trait-situation">${situation}</span>
      ${t.pinned ? '<span class="trait-pin">钉住</span>' : ""}
      <span class="trait-weight">权重 ${esc(t.proficiency ?? 0)}<small>有效 ${esc(t.effective_score ?? 0)}</small></span>
    </div>
    <div class="trait-style">${esc(t.content || "")}</div>
    <div class="trait-meta">命中 ${esc(t.seen ?? 1)} 次${t.modified_by === "manual" ? " · 人工调整过" : ""}</div>
    ${alias}
    ${evidenceHtml}
    <div class="trait-btns">${btns.join("")}</div>
  </div>`;
}

async function reviewTrait(action, sessionId, content) {
  try {
    const payload = unwrap(
      await Bridge.apiPost("review", { action, session_id: sessionId, content }),
    );
    if (payload && payload.success === false) {
      showToast(payload.message || "操作失败");
      return;
    }
    showToast(payload.message || "已更新");
    await loadTraits();
  } catch (err) {
    console.error("审核失败:", err);
    showToast("操作失败，请查看日志");
  }
}

function renderPager(data) {
  const total = data.total ?? 0;
  const pages = data.total_pages ?? 1;
  const page = data.page ?? 1;
  $("pagerInfo").textContent = `共 ${total} 个会话 · 第 ${page}/${pages} 页`;
  $("prevPage").disabled = page <= 1;
  $("nextPage").disabled = page >= pages;
}

function renderStats(stats, manager) {
  const metrics = stats.metrics || {};
  const items = [
    ["会话数", stats.sessions ?? 0],
    ["风格特征", stats.traits ?? 0],
    ["学习 / 手动", `${stats.learned_traits ?? 0} / ${stats.manual_traits ?? 0}`],
    ["稳定 / 场景", `${stats.stable ?? 0} / ${stats.situational ?? 0}`],
    ["待学习消息", stats.pending_messages ?? 0],
    ["学习间隔", fmtDuration(manager.interval)],
    ["下次学习", manager.running ? fmtDuration(manager.next_run_in) : "已停止"],
    ["状态", manager.running ? "运行中" : "已停止"],
  ];
  $("stats").innerHTML = items
    .map(([k, v]) => `<div class="stat"><span>${esc(k)}</span><b>${esc(v)}</b></div>`)
    .join("");

  // 累计成本：让「这套机制到底花了多少」可见
  const calls = metrics.learn_calls ?? 0;
  const ok = metrics.learn_ok ?? 0;
  const fail = metrics.learn_fail ?? 0;
  const chars = metrics.chars_analyzed ?? 0;
  $("cost").innerHTML = [
    ["累计学习次数", calls],
    ["成功 / 失败", `${ok} / ${fail}`],
    ["累计分析消息", metrics.messages_analyzed ?? 0],
    ["累计分析字数", chars >= 10000 ? `${(chars / 10000).toFixed(1)} 万` : chars],
    ["累计耗时", fmtDuration(Math.round(metrics.elapsed_total ?? 0))],
  ]
    .map(([k, v]) => `<div class="stat"><span>${esc(k)}</span><b>${esc(v)}</b></div>`)
    .join("");

  const last = manager.last_run_result || {};
  $("schedule").textContent = last.sessions
    ? `上一轮：${last.sessions} 个会话，成功 ${last.ok}，失败 ${last.fail}`
    : last.skipped
      ? `上一轮：${last.skipped}`
      : "";
}

// ── 今日消耗 ────────────────────────────────────────────────────────

function renderUsage(data) {
  const usage = data.usage || {};
  const today = usage.today || {};
  const tokens = today.tokens || {};
  const cost = Number(today.cost) || 0;

  $("usageTop").innerHTML = `
    <div class="usage-hero"><span>今日金额</span><b>¥${esc(fmtMoney(cost))}</b></div>
    <div class="stat"><span>今日调用</span><b>${esc(today.calls ?? 0)}</b></div>
    <div class="stat"><span>今日搜索</span><b>${esc(today.search_calls ?? 0)}</b></div>`;

  $("usageTokens").innerHTML = [
    ["缓存命中输入", fmtCount(tokens.input_cached)],
    ["未命中输入", fmtCount(tokens.input_other)],
    ["输出", fmtCount(tokens.output)],
  ]
    .map(([k, v]) => `<div class="stat"><span>${esc(k)} token</span><b>${esc(v)}</b></div>`)
    .join("");

  renderHours(usage);
  renderBars(usage.days || []);
}

// ── 今日 24 小时分布 ─────────────────────────────────────────────────
//
// 仿 DeepSeek 用量页的形态：左侧金额刻度、中间 24 根柱子、底部时间轴。
// 柱子按最大值归一化，所以「今天只有一笔」时那根柱子也是满高——看的是分布
// 形状而不是绝对值，绝对值在上面「今日金额」里已经有了。

let hourMetric = "cost";

function renderHours(usage) {
  const box = $("hourChart");
  if (!box) return;
  const hours = usage.hours || [];
  const note = $("hourNote");
  if (!hours.length) {
    box.innerHTML = "";
    if (note) note.textContent = "还没有小时级数据。这个版本的插件开始记录后，每小时的分布会出现在这里。";
    return;
  }

  const byCalls = hourMetric === "calls";
  const values = hours.map((h) => (byCalls ? Number(h.calls) || 0 : Number(h.cost) || 0));
  const max = Math.max(...values, byCalls ? 1 : 0.0001);
  const nowHour = Number(usage.current_hour ?? -1);
  const total = values.reduce((a, b) => a + b, 0);

  const ticks = byCalls
    ? [Math.round(max), Math.round(max / 2), 0].map((v) => String(v))
    : [max, max / 2, 0].map((v) => `¥${fmtMoney(v)}`);

  const bars = hours
    .map((h, i) => {
      const value = values[i];
      const height = value > 0 ? Math.max(3, Math.round((value / max) * 100)) : 0;
      const tip = byCalls
        ? `${h.label} · ${h.calls} 次调用${h.search_calls ? ` · ${h.search_calls} 次搜索` : ""}`
        : `${h.label} · ¥${fmtMoney(h.cost)} · ${h.calls} 次调用`;
      const classes = ["hbar"];
      if (i === nowHour) classes.push("now");
      return `<div class="${classes.join(" ")}">
        <span class="hbar-tip">${esc(tip)}</span>
        <div class="hbar-col"><i style="height:${height}%"></i></div>
      </div>`;
    })
    .join("");

  // 只标 5 个时间点：24 个标签会糊成一片
  const axis = [0, 6, 12, 18, 23]
    .map((hour) => {
      const left = (hour / 23) * 100;
      const shift = hour === 0 ? "0" : hour === 23 ? "-100%" : "-50%";
      return `<span style="left:${left}%;transform:translateX(${shift})">${hours[hour].label}</span>`;
    })
    .join("");

  box.innerHTML = `
    <div class="hour-yaxis">${ticks.map((t) => `<span>${esc(t)}</span>`).join("")}</div>
    <div class="hour-body">
      <div class="hour-grid"><i></i><i></i><i></i></div>
      <div class="hour-bars">${bars}</div>
    </div>
    <div class="hour-xaxis">${axis}</div>`;

  if (note) {
    const peak = hours.reduce((best, h) => (Number(h[byCalls ? "calls" : "cost"]) > Number(best[byCalls ? "calls" : "cost"]) ? h : best), hours[0]);
    note.textContent = total > 0
      ? `峰值在 ${peak.label}（${byCalls ? `${peak.calls} 次` : `¥${fmtMoney(peak.cost)}`}），当前 ${String(nowHour).padStart(2, "0")}:00 这一小时已统计在内。`
      : "今天还没有产生调用。";
  }
}

// 近 7 天柱状图：纯 div 按最大值归一化，悬停看当天金额
function renderBars(days) {
  const box = $("usageBars");
  if (!days.length) {
    box.innerHTML = '<p class="hint">还没有消耗记录</p>';
    return;
  }
  const costs = days.map((d) => Number(d.cost) || 0);
  const max = Math.max(...costs, 0.0001);
  box.innerHTML = days
    .map((d, i) => {
      const cost = costs[i];
      const height = Math.max(2, Math.round((cost / max) * 100));
      const label = `${d.date} · ¥${fmtMoney(cost)} · ${d.calls ?? 0} 次调用`;
      return `<div class="bar${i === days.length - 1 ? " today" : ""}">
        <span class="bar-tip">${esc(label)}</span>
        <div class="bar-col"><i style="height:${height}%"></i></div>
        <em>${esc(String(d.date || "").slice(5))}</em>
      </div>`;
    })
    .join("");
}

// 「本群今日搜索 x/y」：优先用预览选中的会话，否则取搜得最多的那个群
function renderUsageSearch(data) {
  const el = $("usageSearch");
  if (!el) return;
  const search = data.search || {};
  const limit = search.daily_limit ?? "?";
  if (search.mode === "inherit") {
    el.textContent = "联网搜索当前跟随 AstrBot 全局设置：插件看不到搜了几次，也管不了每日上限。";
    return;
  }
  if (search.mode === "off") {
    el.textContent = "联网搜索已被插件摘掉，不会产生搜索消耗。";
    return;
  }
  const map = (data.usage || {}).search_used_by_session || {};
  const selected = $("previewSession").value;
  // 优先看当前选中的会话；没选就从今天搜得最多的群里挑一个
  const ranked = Object.keys(map).sort((a, b) => (map[b] || 0) - (map[a] || 0));
  const sid = selected || ranked[0] || "";
  const used = sid ? (map[sid] || 0) : 0;
  el.textContent = sid
    ? `本群今日搜索 ${used}/${limit}（${nameOf(sid)}）`
    : `今日还没有搜索记录，每个群每天上限 ${limit} 次。`;
}

// ── 设置（热调）────────────────────────────────────────────────────

// 六组新参数 + 一组原有的运行参数。hint 一律写清成本影响。
const SETTING_GROUPS = [
  {
    id: "base",
    title: "⚙️ 运行与学习",
    sub: "原有参数，改动同样即时生效",
    items: [
      { key: "enabled", label: "启用群聊记忆", type: "bool", default: true, hint: "总开关。关掉后不收集、不学习、不注入，完全静默。影响：token 归零。" },
      { key: "inject_enabled", label: "启用风格注入", type: "bool", default: true, hint: "关闭后不再注入风格，字数上限也一起失效（两者在同一个钩子里）。学习照常、仍有消耗。影响：token ↓（每条回复少几十字）。" },
      { key: "analysis_interval_seconds", label: "学习间隔（秒）", type: "int", min: 60, max: 604800, step: 60, default: 21600, hint: "默认 21600（6 小时）≈ 一天最多 4 次，这是「几乎零消耗」的核心。影响：token ↑（调小）。" },
      { key: "min_history_for_analysis", label: "触发学习的最少消息数", type: "int", min: 1, max: 1000, step: 1, default: 10, hint: "攒够这么多条新消息才送去提炼，不够直接跳过不发请求。影响：token ↓（调大）。" },
      { key: "max_messages_per_learn", label: "单次提炼最多消息数", type: "int", min: 1, max: 500, step: 10, default: 80, hint: "单次最多带多少条消息进 prompt。影响：token ↑（调大）。" },
      { key: "max_stable_inject", label: "最多注入学习风格", type: "int", min: 0, max: 20, step: 1, default: 5, hint: "每轮按熟练度取前 N 条学习风格注入。影响：token ↑（调大）。" },
      { key: "max_manual_inject", label: "最多注入手动记忆", type: "int", min: 0, max: 20, step: 1, default: 3, hint: "手动记忆单独占名额，和学习名额各算各的。设 0 = 只存不注入。" },
      { key: "max_stable_traits", label: "学习风格存储上限", type: "int", min: 0, max: 200, step: 5, default: 40, hint: "超出按熟练度淘汰。特征越多，每次学习的 prompt 越长。影响：token ↑（调大）。" },
      { key: "enable_situational_inject", label: "启用场景化表达注入", type: "bool", default: true, hint: "只在语境匹配时才注入，成本影响很小；关掉主要损失接梗能力。" },
      { key: "max_situational_inject", label: "最多注入场景化表达", type: "int", min: 0, max: 20, step: 1, default: 2, hint: "每轮最多带几条语境匹配的表达。影响：token ↑（调大）。" },
      { key: "max_situational_traits", label: "场景化表达存储上限", type: "int", min: 0, max: 100, step: 5, default: 20, hint: "同上，针对存储。设 0 = 不再保留这类特征。" },
      { key: "enable_trigger_matching", label: "启用触发词匹配", type: "bool", default: true, hint: "先做纯本地字符串匹配，命中就不发向量请求——省钱的关键开关。影响：token ↓（保持开启）。" },
      { key: "situational_similarity_threshold", label: "场景化相似度阈值", type: "float", min: 0, max: 1, step: 0.05, default: 0.4, hint: "向量匹配阈值，越低越容易注入。影响：token 轻微 ↑（调低）。" },
      { key: "embedding_timeout_seconds", label: "嵌入请求超时（秒）", type: "float", min: 0.5, max: 30, step: 0.5, default: 2, hint: "向量请求跑在回复链路上，超时降级为关键词匹配。影响：token 不变，只影响延迟。" },
      { key: "enable_cross_group", label: "启用跨群风格参考", type: "bool", default: false, hint: "把别群的稳定风格也注入进来。影响：token ↑，收益有限，默认关。" },
      { key: "max_global_styles", label: "最多注入全局风格", type: "int", min: 0, max: 20, step: 1, default: 3, hint: "跨群开启时最多注入几条别群风格。影响：token ↑（调大）。" },
      { key: "debug_mode", label: "调试日志", type: "bool", default: false, hint: "日志里输出完整注入内容和学习结果。只增加日志量，排查完关掉。" },
    ],
  },
  {
    id: "style",
    title: "🎭 风格与人格",
    sub: "调低＝省钱＋更像人设，调高＝更像群里的人",
    items: [
      {
        key: "style_strength", label: "被动回复风格强度", type: "slider", min: 0, max: 100, step: 5, default: 100,
        hint: "0 = 完全不注入（纯人格）；1-30 只留 1 条且措辞很弱；31-69 名额减半；70-99 名额四分之三；100 满额，且必定让位（100 时不再掷骰，每轮都按风格走）。强度和注入名额一起降。影响：token ↓（调低，每轮少带几十到一百多字）。",
      },
      {
        key: "active_style_strength", label: "主动插话风格强度", type: "slider", min: 0, max: 100, step: 5, default: 60,
        hint: "机器人自己插话时用这一档。默认 60 故意低于阈值 70，这样插话永远不推翻人格。影响：token ↓（通常比被动低）。",
      },
      {
        key: "style_override_threshold", label: "风格压过人格的起始强度", type: "slider", min: 0, max: 100, step: 5, default: 100,
        hint: "强度【严格大于】这个值才让位。默认 100 + 强度 100 → 开箱不让位，和 1.2.0 行为一致。想让群风格压过人设就调低它，例如 70 配强度 100 = 每轮都按风格走。只是指令层优先级，不是真删人格。影响：token 不变。",
      },
      {
        key: "style_override_deep", label: "深度覆盖人格（会击穿缓存）", type: "bool", warn: true, default: false,
        hint: "会让该轮缓存失效：每次命中都改 system prompt，DeepSeek 前缀缓存必然失效，输入价从 ¥0.05 跳到 ¥1.5/百万（30 倍）。默认关闭。",
      },
    ],
  },
  {
    id: "length",
    title: "📏 回复长度",
    sub: "「上限」是倾向不是硬限制",
    items: [
      { key: "reply_max_chars", label: "被动回复字数上限", type: "int", min: 0, max: 2000, step: 10, default: 0, hint: "0 = 不限制。设 80 实际常见 90-120 字，别当硬限制用。影响：token ↓（设置后回复变短）。" },
      { key: "active_reply_max_chars", label: "主动插话字数上限", type: "int", min: 0, max: 2000, step: 10, default: 80, hint: "插话本来就该路过接一句，三行字容易变成抢话。同样只是倾向。影响：token ↓。" },
      {
        key: "reply_max_chars_hard", label: "超限时硬截断", type: "bool", warn: true, default: false,
        hint: "会切坏 markdown：按句末标点硬切，表格、链接、代码块和表情占位符都可能被切残。不建议开。影响：token 不变。",
      },
    ],
  },
  {
    id: "active",
    title: "🗣 主动回复",
    sub: "唯一会凭空产生请求的功能，默认关闭",
    items: [
      { key: "active_reply_enabled", label: "启用主动回复", type: "bool", default: false, hint: "开启后机器人自己找时机接话。影响：token ↑（一天最多日限那么多次）。" },
      {
        key: "active_reply_mode", label: "触发方式", type: "select", default: "probability",
        options: [["probability", "概率掷骰（零额外消耗）"], ["rule", "关键词规则（零额外消耗）"], ["llm_judge", "让模型判定（有消耗）"]],
        hint: "probability 每个冷却窗口掷一次骰；rule 命中提问句式/关键词才接；llm_judge 攒够消息问一次模型，判定本身就有消耗。影响：llm_judge 的 token ↑↑。",
      },
      {
        key: "active_reply_chance", label: "每个窗口的参与概率", type: "slider", min: 0, max: 1, step: 0.05, default: 0.15,
        hint: "仅 probability 模式。0.15 ≈ 一天约 7 次，正好落在日限 8 以内。骰子按窗口掷，不按消息——否则群越热闹越容易被插话。",
      },
      { key: "active_reply_cooldown_seconds", label: "冷却时间（秒）", type: "int", min: 60, max: 86400, step: 60, default: 1800, hint: "两次评估至少隔多久，默认 1800 ≈ 一天 48 个窗口。影响：token ↓（调大）。" },
      { key: "active_reply_daily_limit", label: "每日上限（每群）", type: "int", min: 0, max: 50, step: 1, default: 8, hint: "每群每天最多主动说几次，自然日重置。默认 8 ≈ 1.2 万 token/天。20 以上有平台风控和被嫌吵的风险。" },
      { key: "active_reply_quiet_hours", label: "静默时段", type: "text", default: "01:00-08:00", hint: "格式 01:00-08:00，支持跨天（如 22:00-06:00）。这段时间零消耗。留空 = 不静默。" },
      { key: "active_reply_min_messages", label: "冷清阈值", type: "int", min: 0, max: 100, step: 1, default: 3, hint: "最近 10 分钟少于这么多条就不插话，避免对着空气说话。0 = 关闭检测。影响：token ↓。" },
      { key: "active_reply_judge_stack", label: "判定攒消息数", type: "int", min: 1, max: 100, step: 1, default: 8, hint: "仅 llm_judge：攒够多少条才问一次模型。默认 8，一天 200 条 ≈ 25 次判定 ≈ 1.5 万 token/天。影响：token ↑（调小）。" },
      { key: "active_reply_backoff_limit", label: "连击退避阈值", type: "int", min: 0, max: 20, step: 1, default: 3, hint: "连续这么多次插话没人接就把冷却放大，防止自嗨（设计上限 4 倍）。0 = 关闭退避。一旦有人说话立刻清零。影响：token ↓。" },
      { key: "active_reply_keywords", label: "额外触发词", type: "list", default: [], hint: "仅 rule 模式：逗号分隔，命中就接话。留空只用内置提问句式。影响：token 与命中次数成正比。" },
      {
        key: "active_reply_guidance", label: "语气引导", type: "textarea", default:
          "这是一条由你主动发起的群聊回复，没有人向你提问。\n不要使用「有什么可以帮你」「我在」「已收到」这类服务式或召唤式开头；\n不要提及自己是主动发言或任何机制；\n像普通群友一样简短接话；话题不适合接就少说或不说。",
        hint: "告诉模型这是主动发言、别用客服腔。留空则完全不注入这段。影响：token 轻微 ↑（多几十字）。",
      },
      { key: "active_reply_provider_id", label: "使用的 Provider ID", type: "text", default: "", hint: "判定和插话共用这个模型，留空用当前会话默认。换成便宜的 flash 可显著降低单次成本。" },
    ],
  },
  {
    id: "search",
    title: "🌐 联网搜索",
    sub: "每次搜索约多 1200 token",
    items: [
      {
        key: "web_search_mode", label: "接管模式", type: "select", default: "plugin",
        options: [["plugin", "插件接管（可按群限次，推荐）"], ["inherit", "完全跟随 AstrBot 全局"], ["off", "关闭搜索工具"]],
        hint: "plugin 能按群限次也能统计次数；inherit 完全交给 AstrBot，插件看不到用量；off 就算全局开着也把工具摘掉。影响：token ↑（每次搜索约 1200 token）。",
      },
      {
        key: "web_search_provider", label: "服务商", type: "select", default: "deepseek",
        options: [["deepseek", "DeepSeek（¥0.005/次，复用模型 key）"], ["tavily", "Tavily（免费 1000 次/月）"], ["bocha", "博查 Bocha"], ["brave", "Brave（需要梯子）"]],
        hint: "deepseek 最划算：¥0.005/次、复用模型 key、失败不计费。tavily/bocha 有月额度，20 次/群/天 × 10 群一个月就会烧穿。影响：直接决定每次搜索的钱。",
      },
      { key: "web_search_api_key", label: "API Key", type: "password", default: "", hint: "留空 + 下面复用全局 key 开着时会自动读 AstrBot 里同家的 key，多数情况不用填。填了会明文写进 runtime.json。影响：只影响能不能搜。" },
      { key: "web_search_reuse_global_key", label: "复用 AstrBot 全局 key", type: "bool", default: true, hint: "key 只存一处，插件目录不留明文。DeepSeek 的搜索 key 和模型 key 是同一个。影响：token 不变。" },
      { key: "web_search_daily_limit", label: "每日上限（每群）", type: "int", min: 0, max: 200, step: 1, default: 20, hint: "每群每天最多搜几次，跨群独立，第二天归零。20 次 ≈ ¥0.15/群/天。正常群一天真正需要实时信息不超过 5 次。影响：token ↑（调大）。" },
      { key: "web_search_max_per_turn", label: "单轮上限", type: "int", min: 0, max: 10, step: 1, default: 2, hint: "一轮最多搜几次，防止模型连环搜把当天配额烧完。0 = 不限（不建议）。影响：token ↑（调大）。" },
      { key: "web_search_max_results", label: "返回条数", type: "int", min: 1, max: 50, step: 1, default: 5, hint: "一次搜几条。5 条约 1200 token，调到 10 大约翻倍。影响：token ↑（调大）。" },
      {
        key: "web_search_need_content", label: "返回网页正文", type: "bool", warn: true, default: false,
        hint: "成本翻十倍：每条结果都带整页正文，5 条可能 3-5 万 token，一次搜索顶一天的学习消耗。默认的 summary 已有 600-800 字概要，够用。",
      },
      { key: "web_search_timeout", label: "超时（秒）", type: "int", min: 1, max: 60, step: 1, default: 8, hint: "超过就放弃搜索按失败处理（不阻塞、不计费）。调大会让群里等回复更久。影响：token 不变。" },
      { key: "web_search_hint", label: "搜索提示语", type: "textarea", default: "", hint: "留空完全不注入。填了会提示模型「需要实时信息先搜一下」，但可能搜得太勤把配额提前用完。影响：token 轻微 ↑，且可能显著增加搜索次数。" },
    ],
  },
  {
    id: "admin",
    title: "🛡 管理员",
    sub: "让管理员在群里直接改参数",
    items: [
      { key: "admin_extra_users", label: "额外管理员 QQ", type: "list", default: [], hint: "除 AstrBot 全局管理员外还能管理这个插件的人，逗号分隔。他们能在群里调参数、加记忆、查消耗。影响：token 不变。" },
      { key: "admin_tool_enabled", label: "允许对话调参", type: "bool", default: false, hint: "开启后管理员在群里说话就能改设置、加记忆。非管理员群里这个工具根本不挂给模型。影响：token 轻微 ↑（约 200 token 的工具定义）。" },
    ],
  },
  {
    id: "price",
    title: "💰 计价",
    sub: "只影响面板估的金额，不影响真实扣费",
    items: [
      { key: "token_price_input_cached", label: "缓存命中输入价（元/百万）", type: "float", min: 0, max: 1000, step: 0.01, default: 0.05, hint: "DeepSeek 官方牌价，调价了就改这里，不用等插件更新。" },
      { key: "token_price_input_miss", label: "未命中输入价（元/百万）", type: "float", min: 0, max: 1000, step: 0.01, default: 1.5, hint: "缓存没命中时的价格，是命中价的 30 倍——这就是插件坚持不改 system prompt 的原因。" },
      { key: "token_price_output", label: "输出价（元/百万）", type: "float", min: 0, max: 1000, step: 0.01, default: 4.5, hint: "每百万输出 token 的价格。" },
      { key: "token_price_peak_multiplier", label: "高峰倍率", type: "float", min: 1, max: 10, step: 0.1, default: 2, hint: "高峰时段（工作日 09:00-12:00、14:00-18:00 北京时间）按这个倍率算。DeepSeek 目前 2 倍。" },
      { key: "token_price_peak_enabled", label: "区分峰谷价", type: "bool", default: true, hint: "关掉就全按空闲价算。节假日不单独处理，会略微高估。" },
      { key: "token_stats_keep_days", label: "消耗记录保留天数", type: "int", min: 1, max: 60, step: 1, default: 7, hint: "按天存文件，超出的自动删。只存数字不存聊天内容，泄露也不含隐私。影响：只占磁盘。" },
    ],
  },
];

const STYLE_KEYS = ["style_strength", "active_style_strength", "style_override_threshold"];

// inherit 模式下这些参数由 AstrBot 接管，置灰免得改了没效果
const SEARCH_LOCKED_KEYS = [
  "web_search_api_key",
  "web_search_provider",
  "web_search_daily_limit",
  "web_search_max_per_turn",
  "web_search_need_content",
  "web_search_timeout",
];

function renderSettings(settings) {
  const overrides = settings._runtime_overrides || {};
  $("settings").innerHTML = SETTING_GROUPS.map((group) => {
    const rows = group.items.map((def) => settingRow(def, settings, overrides)).join("");
    const sub = group.sub ? `<small>${esc(group.sub)}</small>` : "";
    // 搜索组的联动提示挂在标题旁，选模式时立刻能看到
    const note = group.id === "search" ? '<span class="group-note" id="searchNote"></span>' : "";
    const footer = group.id === "style" ? '<p class="hint compute" id="styleNote"></p>' : "";
    return `<section class="set-group">
      <h3>${esc(group.title)}${sub}${note}</h3>
      <div class="set-grid">${rows}</div>
      ${footer}
    </section>`;
  }).join("");
  clearSettingMsg();
  updateStyleNote();
  applySearchMode(settings.web_search_mode || "plugin");
}

function settingRow(def, settings, overrides) {
  const raw = settings[def.key] === undefined || settings[def.key] === null
    ? def.default
    : settings[def.key];
  const hot = def.key in overrides ? '<i class="hot">已热调</i>' : "";
  const hint = def.hint
    ? `<p class="set-hint${def.warn ? " warn" : ""}">${esc(def.hint)}</p>`
    : "";
  const wide = def.type === "textarea" || def.type === "list" ? " wide" : "";
  const warn = def.warn ? " warn-row" : "";
  return `<label class="set-row${warn}${wide}" data-row="${def.key}">
    <span class="set-label">${esc(def.label)} ${hot}</span>
    <span class="set-control">${settingControl(def, raw)}</span>
    ${hint}
  </label>`;
}

function settingControl(def, value) {
  const key = def.key;
  if (def.type === "bool") {
    return `<input type="checkbox" data-key="${key}" data-kind="bool" ${value ? "checked" : ""} />`;
  }
  if (def.type === "select") {
    const opts = (def.options || [])
      .map(([v, label]) => `<option value="${escAttr(v)}" ${String(value) === String(v) ? "selected" : ""}>${esc(label)}</option>`)
      .join("");
    return `<select data-key="${key}" data-kind="str">${opts}</select>`;
  }
  if (def.type === "slider") {
    const min = def.min ?? 0;
    const max = def.max ?? 100;
    const step = def.step ?? 1;
    const v = Number(value);
    const safe = Number.isFinite(v) ? Math.min(max, Math.max(min, v)) : Number(def.default ?? min);
    return `<input type="range" data-key="${key}" data-kind="num" data-min="${min}" data-max="${max}"
      min="${min}" max="${max}" step="${step}" value="${safe}"
      style="--prof:${pct(safe, min, max)}%" /><b class="slider-val">${safe}</b>`;
  }
  if (def.type === "textarea") {
    return `<textarea rows="3" data-key="${key}" data-kind="str">${esc(value ?? "")}</textarea>`;
  }
  if (def.type === "list") {
    const text = Array.isArray(value) ? value.join(",") : (value ?? "");
    return `<input type="text" data-key="${key}" data-kind="list" value="${escAttr(text)}" placeholder="逗号分隔" />`;
  }
  const inputType = def.type === "text" ? "text" : def.type === "password" ? "password" : "number";
  const kind = def.type === "text" || def.type === "password" ? "str" : "num";
  const attrs = [`type="${inputType}"`, `data-key="${key}"`, `data-kind="${kind}"`, `value="${escAttr(value ?? "")}"`];
  if (def.min !== undefined) attrs.push(`min="${def.min}"`);
  if (def.max !== undefined) attrs.push(`max="${def.max}"`);
  if (def.step !== undefined) attrs.push(`step="${def.step}"`);
  return `<input ${attrs.join(" ")} />`;
}

function pct(value, min, max) {
  if (max <= min) return 0;
  return Math.round(((value - min) / (max - min)) * 100);
}

// 和 injector.override_probability 同一套算法：低于阈值永远是 0
function overridePct(strength, threshold) {
  if (strength <= 0 || strength <= threshold) return 0;
  return strength >= 100 ? 100 : strength;
}

function numFromInput(key, fallback) {
  const el = document.querySelector(`[data-key="${key}"]`);
  if (!el) return fallback;
  const n = Number(el.value);
  return Number.isFinite(n) ? n : fallback;
}

// 风格强度联动提示：滑块一动就重算，避免「调完不知道会怎样」
function updateStyleNote() {
  const el = $("styleNote");
  if (!el) return;
  const style = (lastStatus && lastStatus.style) || {};
  const s = numFromInput("style_strength", style.strength ?? 100);
  const a = numFromInput("active_style_strength", style.active_strength ?? 60);
  const t = numFromInput("style_override_threshold", style.override_threshold ?? 100);
  const p = overridePct(s, t);
  const ap = overridePct(a, t);

  const lines = [];
  if (s <= 0) {
    lines.push("当前：强度 0，完全不注入风格（纯人格）");
  } else {
    lines.push(
      p > 0
        ? `当前：强度 ${s} > 阈值 ${t}，约 ${p}% 的回复会让风格压过人格`
        : `当前：强度 ${s} ≤ 阈值 ${t}，人格优先，风格只作参考`,
    );
  }
  if (ap <= 0) {
    lines.push(`主动插话：强度 ${a} ≤ 阈值 ${t}，不会推翻人格（默认的安全设定）`);
  } else {
    lines.push(`主动插话：强度 ${a} > 阈值 ${t}，约 ${ap}% 的插话会让风格压过人格`);
  }
  el.innerHTML = lines.map(esc).join("<br>");
}

// 搜索模式联动：inherit 时锁掉插件侧参数并说明后果
function applySearchMode(mode) {
  const inherit = mode === "inherit";
  SEARCH_LOCKED_KEYS.forEach((key) => {
    const el = document.querySelector(`[data-key="${key}"]`);
    if (!el) return;
    el.disabled = inherit;
    const row = el.closest(".set-row");
    if (row) row.classList.toggle("locked", inherit);
  });

  const note = $("searchNote");
  if (!note) return;
  note.className = "group-note";
  const search = (lastStatus && lastStatus.search) || {};
  if (inherit) {
    note.textContent = "当前完全跟随 AstrBot 设置。插件不统计搜索次数、也无法限制每日上限。";
    note.classList.add("alert");
  } else if (mode === "off") {
    note.textContent = "所有搜索工具都被摘掉，就算 AstrBot 全局开着也搜不了。";
    note.classList.add("alert");
  } else if (search.key_configured === false) {
    note.textContent = "还没配 key，这个模式下搜不了。去 AstrBot 面板填 DeepSeek 搜索 key，或在下面填。DeepSeek 的搜索 key 和模型 key 是同一个。";
    note.classList.add("danger-text");
  } else {
    note.textContent = "插件接管搜索：能按群限次、能统计次数，key 已就绪。";
    note.classList.add("ok-text");
  }
}

function setSettingMsg(message) {
  const el = $("settingsMsg");
  if (!el) return;
  el.textContent = message || "";
}
function clearSettingMsg() {
  setSettingMsg("");
}

async function saveSetting(key, value) {
  try {
    const payload = unwrap(await Bridge.apiPost("manage", {
      action: "set_setting",
      key,
      value,
    }));
    if (payload && payload.success === false) {
      setSettingMsg(`${key} 保存失败：${payload.message || "未知原因"}`);
      showToast(payload.message || "保存失败");
      return;
    }
    clearSettingMsg();
    showToast(`已更新 ${key}`);
    await load();
  } catch (err) {
    console.error("保存设置失败:", err);
    setSettingMsg(`${key} 保存失败，请查看日志`);
    showToast("保存失败");
  }
}

function updateSlider(el) {
  const min = Number(el.min || 0);
  const max = Number(el.max || 100);
  const value = Number(el.value);
  el.style.setProperty("--prof", `${pct(value, min, max)}%`);
  const label = el.parentElement && el.parentElement.querySelector(".slider-val");
  if (label) label.textContent = String(value);
}

// 把控件里的原始输入转成后端要的类型，顺带做范围兜底
function readSettingValue(el) {
  const kind = el.dataset.kind || "str";
  if (kind === "bool") return { value: !!el.checked };
  if (kind === "list") {
    return { value: el.value.split(/[,，]/).map((x) => x.trim()).filter(Boolean) };
  }
  if (kind === "num") {
    let n = Number(el.value);
    if (el.value.trim() === "" || !Number.isFinite(n)) {
      return { error: "请填一个数字" };
    }
    const lo = el.min === "" ? null : Number(el.min);
    const hi = el.max === "" ? null : Number(el.max);
    let clamped = null;
    if (lo !== null && n < lo) { n = lo; clamped = lo; }
    if (hi !== null && n > hi) { n = hi; clamped = hi; }
    return { value: n, clamped };
  }
  return { value: el.value };
}

// ── 会话 ────────────────────────────────────────────────────────────

function renderSessions(sessions) {
  if (!sessions.length) {
    $("sessions").innerHTML = '<p class="empty">还没有收集到群聊消息</p>';
    return;
  }
  $("sessions").innerHTML = sessions.map(sessionCard).join("");
}

function sessionCard(s) {
  const state = s.learn_state || {};
  const stable = s.stable || [];
  const situational = s.situational || [];
  const manual = s.manual || [];
  const limits = s.limits || {};
  const learnedCount = stable.length + situational.length;

  const scale = state.last_message_count
    ? ` · ${state.last_message_count} 条 / ${state.last_chars ?? 0} 字 / ${state.last_elapsed ?? 0}s`
    : "";
  const stateLine = state.last_learn_at
    ? `上次学习 ${fmtTime(state.last_learn_at)} · ${state.last_ok ? "成功" : "失败"}${scale} · 累计 ${state.total_learns || 0} 次`
    : "尚未学习";

  const manualRows = manual.map((t) => traitRow(s, t, "manual")).join("");
  const stableRows = stable.map((t) => traitRow(s, t, "stable")).join("");
  const sitRows = situational.map((t) => traitRow(s, t, "situational")).join("");

  const history = (s.history || []).map(
    (m) => `<li><i>${esc(m.sender || "?")}</i>${esc(m.content || "")}</li>`,
  ).join("");
  // 学习历史时间线（最近 20 次）
  const timeline = (state.history || []).slice(-8).reverse().map((h) => {
    const tag = h.ok ? "成功" : "失败";
    const detail = h.ok
      ? `稳定 ${h.stable} · 场景 ${h.situational}`
      : esc(h.message || "未知原因");
    return `<li><i>${fmtTime(h.at)}</i>${tag} · ${detail}
      <i>${h.messages || 0} 条 / ${h.chars || 0} 字 / ${h.elapsed || 0}s</i></li>`;
  }).join("");

  const learnOn = s.learn_enabled !== false;
  const injectOn = s.inject_enabled !== false;
  const activeOn = s.active_enabled !== false;
  const sid = escAttr(s.session_id);
  const opened = openSessions.has(s.session_id) ? " open" : "";

  // 注入名额会按强度缩放，所以把满额也一并显示，免得以为配错了
  const scaled = limits.strength !== undefined && limits.strength < 100;
  const quota = scaled
    ? `注入名额：手动 ${limits.manual ?? "?"} 条 · 学习 ${limits.learned ?? "?"} 条（强度 ${limits.strength} 缩放，满额 ${limits.base_manual ?? "?"}/${limits.base_learned ?? "?"}）`
    : `注入名额：手动 ${limits.manual ?? "?"} 条 · 学习 ${limits.learned ?? "?"} 条`;

  const act = s.active_status || {};
  const rollIn = act.next_roll_in;
  const rollText = rollIn === null || rollIn === undefined || rollIn <= 0
    ? "待评估"
    : `下次评估 ${fmtDuration(rollIn)}后`;
  const activeLine = `今日 ${act.today_count ?? 0}/${act.daily_limit ?? "?"} · ${rollText}`;

  const searchAllowed = s.search_allowed;
  const searchValue = searchAllowed === true ? "on" : searchAllowed === false ? "off" : "";
  const searchText = searchAllowed === true
    ? "本群强制开启"
    : searchAllowed === false
      ? "本群强制关闭"
      : "跟随全局";
  const searchLine = `今日 ${s.search_used ?? 0}/${s.search_limit ?? "?"} · ${searchText}`;

  return `<details class="session" data-sid="${sid}"${opened}>
    <summary>
      <b>${s.pinned ? "📌 " : ""}${esc(s.display_name)}</b>
      <span>学习 ${learnedCount} · 手动 ${manual.length} · 待学习 ${s.history_count ?? 0}</span>
      <button type="button" class="pin-btn${s.pinned ? " on" : ""}" data-act="pin" data-sid="${sid}">${
        s.pinned ? "已置顶" : "置顶"
      }</button>
    </summary>
    <div class="body">
      <p class="hint">${esc(stateLine)}<br>${esc(quota)}</p>
      <div class="session-switches">
        <label><input type="checkbox" data-act="toggle_learn" data-sid="${sid}" ${learnOn ? "checked" : ""}/> 学习</label>
        <label><input type="checkbox" data-act="toggle_inject" data-sid="${sid}" ${injectOn ? "checked" : ""}/> 注入</label>
        <label><input type="checkbox" data-act="toggle_active" data-sid="${sid}" ${activeOn ? "checked" : ""}/> 主动回复</label>
        <span class="sw-note">${esc(activeLine)}</span>
      </div>
      <div class="session-switches">
        <label class="search-switch">联网搜索
          <select data-act="set_search" data-sid="${sid}">
            <option value="" ${searchValue === "" ? "selected" : ""}>跟随全局</option>
            <option value="on" ${searchValue === "on" ? "selected" : ""}>开启</option>
            <option value="off" ${searchValue === "off" ? "selected" : ""}>关闭</option>
          </select>
        </label>
        <span class="sw-note">${esc(searchLine)}</span>
      </div>
      <div class="session-actions">
        <button data-act="add_trait" data-sid="${sid}">+ 手动记忆</button>
        <button data-act="learn" data-sid="${sid}">立即学习</button>
        <button class="ghost" data-act="clear_history" data-sid="${sid}">清待学习记录</button>
        <button class="ghost danger" data-act="clear_learned" data-sid="${sid}">清学习产出</button>
        <button class="ghost danger" data-act="clear_manual" data-sid="${sid}">清手动记忆</button>
      </div>
      ${manual.length ? `<h3>✋ 手动记忆（${manual.length}）<small>不看聊天记录，学习也删不掉它</small></h3><ul class="sublist">${manualRows}</ul>` : ""}
      ${stable.length ? `<h3>稳定风格（${stable.length}）</h3><ul class="sublist">${stableRows}</ul>` : ""}
      ${situational.length ? `<h3>场景化表达（${situational.length}）</h3><ul class="sublist">${sitRows}</ul>` : ""}
      ${timeline ? `<h3>学习记录（最近 ${(state.history || []).length} 次）</h3><ul class="sublist">${timeline}</ul>` : ""}
      ${history ? `<h3>最近待学习消息（${s.history_count ?? 0}）</h3><ul class="sublist">${history}</ul>` : ""}
    </div>
  </details>`;
}

function traitRow(s, trait, kind) {
  const sessionId = s.session_id;
  const content = trait.content || "";
  const isManual = kind === "manual";
  const isSit = kind === "situational" || trait.category === "situational";
  const sourceAttr = isManual ? "manual" : "learned";
  const ctx = isSit && trait.context ? `<i>${esc(trait.context)}</i>` : "";
  const triggers =
    isSit && Array.isArray(trait.triggers) && trait.triggers.length
      ? `<i>触发：${esc(trait.triggers.join("、"))}</i>`
      : "";

  const prof = Math.max(0, Math.min(100, Number(trait.proficiency ?? 0)));
  const tags = [];
  if (isManual) tags.push('<b class="tag manual">手动</b>');
  // 场景化是按语境临时匹配的，没法提前判定入没入选，只标稳定风格
  if (!isSit) {
    const selected = (s.selected || []).includes(content);
    tags.push(selected
      ? '<b class="tag in">注入中</b>'
      : '<b class="tag out">未入选</b>');
  }
  const bar = `<span class="prof-bar" role="button" title="熟练度 ${prof}，点击修改"><i style="width:${prof}%"></i></span><em class="prof-num">${prof}</em>`;

  return `<li>${tags.join("")}${esc(content)}${ctx}${triggers}${bar}
    <button class="edit" data-act="edit_trait" data-sid="${escAttr(sessionId)}" data-content="${escAttr(content)}" data-source="${sourceAttr}" title="编辑">✎</button>
    <button class="del" data-act="delete_trait" data-sid="${escAttr(sessionId)}" data-content="${escAttr(content)}" data-source="${sourceAttr}" title="删除该条">×</button>
  </li>`;
}

// ── 预览会话选择器（可输入群号 / QQ 号搜索）──────────────────────────
//
// 原生 select 在两三个群时够用，几十个群就只能一行行翻。这里换成
// 「输入框 + 下拉列表」：键入群号、QQ 号或「群」字都能过滤，置顶的排在前面。

let previewOptions = [];   // [{ id, label, pinned }]
let previewSelectedId = "";

function previewDisplayText() {
  const current = previewOptions.find((o) => o.id === previewSelectedId);
  return current ? `${current.pinned ? "📌 " : ""}${current.label}` : "";
}

function syncPreviewInput() {
  $("previewSessionInput").value = previewDisplayText();
  // 其余逻辑（预览、今日搜索次数）都还读这个隐藏值，保持一致
  $("previewSession").value = previewSelectedId || "";
}

function fillPreviewSessions(data) {
  const ids = data.known_ids || [];
  const pinned = data.pinned_ids || pinnedIds;
  const rank = new Map(pinned.map((id, i) => [id, i]));
  previewOptions = [...ids]
    .sort((a, b) => (rank.get(a) ?? 1e6) - (rank.get(b) ?? 1e6))
    .map((id) => ({ id, label: nameOf(id), pinned: rank.has(id) }));

  // 刷新后要保住当前选中；那个会话没了就退回第一个
  if (!previewOptions.some((o) => o.id === previewSelectedId)) {
    previewSelectedId = previewOptions.length ? previewOptions[0].id : "";
  }
  syncPreviewInput();
  $("previewSessionInput").placeholder = previewOptions.length
    ? "输入群号 / QQ 号搜索会话"
    : "（还没有任何会话）";
}

function openPreviewMenu() {
  const menu = $("previewMenu");
  const typed = $("previewSessionInput").value.trim().toLowerCase();
  // 输入框里平时就写着选中项的名字，直接拿它当关键词会把列表过滤成一条，
  // 所以只有用户真的改了内容才当搜索词
  const keyword = typed && typed !== previewDisplayText().toLowerCase() ? typed : "";
  const hits = previewOptions.filter(
    (o) => !keyword || `${o.id} ${o.label}`.toLowerCase().includes(keyword),
  );

  menu.innerHTML = hits.length
    ? hits
        .slice(0, 200)
        .map(
          (o) => `<button type="button" class="picker-item${o.id === previewSelectedId ? " on" : ""}" data-id="${escAttr(o.id)}">
            <span>${o.pinned ? "📌 " : ""}${esc(o.label)}</span>
            <small>${esc(o.id)}</small>
          </button>`,
        )
        .join("")
    : '<p class="hint picker-empty">没有匹配的会话</p>';
  menu.hidden = false;
}

function closePreviewMenu() {
  $("previewMenu").hidden = true;
  // 关掉时把输入框还原成选中项的名字，避免留下半截搜索词
  syncPreviewInput();
}

function selectPreview(id) {
  previewSelectedId = id;
  $("previewMenu").hidden = true;
  syncPreviewInput();
  if (lastStatus) renderUsageSearch(lastStatus);
}

// ── 人格档案（按群生成的文件，自动段 + 人工段）──────────────────────

let personaList = [];        // 概览：[{session_id, display_name, pinned, exists, updated_at, ...}]
let personaCurrent = "";     // 当前查看的会话
let personaLoaded = false;
let personaFull = "";        // 当前文件的完整内容，用于复制/下载

async function ensurePersona(force) {
  if (personaLoaded && !force) return;
  await loadPersonaList();
}

async function loadPersonaList() {
  try {
    const data = unwrap(await Bridge.apiGet("persona", {}));
    personaList = data.sessions || [];
    personaLoaded = true;
    renderPersonaList();
    // 首次进来默认打开第一个群，省一次点击
    if (!personaCurrent || !personaList.some((s) => s.session_id === personaCurrent)) {
      const first = personaList[0];
      if (first) await openPersona(first.session_id);
      else renderPersonaEmpty();
    }
  } catch (err) {
    console.error("加载人格档案列表失败:", err);
    showToast("加载人格档案失败");
  }
}

function renderPersonaList() {
  const box = $("personaList");
  if (!box) return;
  const query = (view.personaQuery || "").trim().toLowerCase();
  const rows = personaList.filter(
    (s) => !query || `${s.session_id} ${s.display_name}`.toLowerCase().includes(query),
  );
  if (!rows.length) {
    box.innerHTML = `<p class="hint">${personaList.length ? "没有匹配的会话。" : "还没有学到东西，先在群里聊几句。"}</p>`;
    return;
  }
  box.innerHTML = rows
    .map((s) => {
      const on = s.session_id === personaCurrent ? " on" : "";
      const state = s.exists ? "已生成" : "未生成";
      return `<div class="persona-item${on}" data-sid="${escAttr(s.session_id)}">
        <span class="pi-name">${s.pinned ? "📌 " : ""}${esc(s.display_name)}</span>
        <span class="pi-meta">${esc(s.trait_active ?? 0)}/${esc(s.trait_total ?? 0)} 条生效 · ${state}</span>
        <button type="button" class="pin-btn${s.pinned ? " on" : ""}" data-pact="pin" data-sid="${escAttr(s.session_id)}">${
          s.pinned ? "已置顶" : "置顶"
        }</button>
      </div>`;
    })
    .join("");
}

function renderPersonaEmpty() {
  $("personaHead").innerHTML = '<p class="hint">还没有任何群的学习数据，人格档案会在学习出内容后自动生成。</p>';
  $("personaAuto").textContent = "";
  $("personaManual").value = "";
  $("personaPath").textContent = "";
  personaFull = "";
}

async function openPersona(sessionId) {
  personaCurrent = sessionId;
  renderPersonaList();
  try {
    const data = unwrap(await Bridge.apiGet("persona", { session_id: sessionId }));
    if (data && data.success === false) {
      showToast(data.message || "读取失败");
      return;
    }
    personaFull = data.full || "";
    const updated = data.updated_at ? fmtTime(data.updated_at) : "尚未生成";
    $("personaHead").innerHTML = `
      <b>${esc(data.display_name || nameOf(sessionId))}</b>
      <span class="hint">${esc(data.trait_active ?? 0)} 条生效特征 · 最后更新 ${esc(updated)}</span>`;
    $("personaAuto").textContent = data.auto || "（自动段还是空的：这个群还没有通过审核的特征）";
    $("personaManual").value = data.manual || "";
    $("personaPath").textContent = data.path ? `文件：${data.path}` : "";
    // 列表里的「已生成 / 未生成」要靠这次访问刷新
    const row = personaList.find((s) => s.session_id === sessionId);
    if (row && !row.exists) {
      row.exists = true;
      renderPersonaList();
    }
  } catch (err) {
    console.error("读取人格档案失败:", err);
    showToast("读取人格档案失败");
  }
}

async function savePersona() {
  if (!personaCurrent) {
    showToast("先选一个群");
    return;
  }
  const manual = $("personaManual").value;
  const payload = unwrap(
    await Bridge.apiPost("manage", {
      action: "save_persona",
      session_id: personaCurrent,
      manual,
    }),
  );
  if (payload && payload.success === false) {
    showToast(payload.message || "保存失败");
    return;
  }
  showToast("人工补充已保存");
  // 重新读一次，头部时间戳与全文同步刷新
  await openPersona(personaCurrent);
}

async function rebuildPersona(scope) {
  const label = scope === "all" ? "全部群" : "当前群";
  if (scope === "all" && !(await askConfirm("按学习库里已生效的特征，重新生成所有人格档案？人工补充的内容不受影响。"))) return;
  const payload = unwrap(
    await Bridge.apiPost("manage", {
      action: "rebuild_persona",
      session_id: scope === "all" ? "" : personaCurrent,
    }),
  );
  if (payload && payload.success === false) {
    showToast(payload.message || "生成失败");
    return;
  }
  showToast(`${label}已重新生成`);
  await loadPersonaList();
  if (scope !== "all" && personaCurrent) await openPersona(personaCurrent);
}

async function copyPersona() {
  if (!personaFull) {
    showToast("还没有可复制的内容");
    return;
  }
  try {
    await navigator.clipboard.writeText(personaFull);
    showToast("已复制全文");
  } catch (err) {
    // iframe 里没有剪贴板权限时降级成「选中文本」
    const area = document.createElement("textarea");
    area.value = personaFull;
    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    area.remove();
    showToast("已复制全文");
  }
}

function downloadPersona() {
  if (!personaFull) {
    showToast("还没有可下载的内容");
    return;
  }
  const blob = new Blob([personaFull], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${String(personaCurrent || "persona").replace(/[^0-9A-Za-z_-]+/g, "_")}.md`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ── 操作 ────────────────────────────────────────────────────────────

async function manage(body) {
  const payload = unwrap(await Bridge.apiPost("manage", body));
  if (payload && payload.success === false) {
    showToast(payload.message || "操作失败");
    return false;
  }
  return true;
}

async function manageWithMessage(body, fallback) {
  const payload = unwrap(await Bridge.apiPost("manage", body));
  if (payload && payload.success === false) {
    showToast(payload.message || "操作失败");
    return false;
  }
  showToast(payload.message || fallback);
  return true;
}

async function learn(sessionId) {
  showToast("正在学习，请稍候…");
  const payload = unwrap(await Bridge.apiPost("manage", {
    action: "learn",
    session_id: sessionId,
  }));
  showToast(payload.ok ? `学习完成：稳定 ${payload.stable} · 场景 ${payload.situational}` : (payload.reason || "学习失败"));
  await load();
}

async function preview() {
  const sessionId = $("previewSession").value;
  const message = $("previewMessage").value;
  if (!sessionId) { showToast("先选一个会话"); return; }
  const payload = unwrap(await Bridge.apiPost("preview", {
    session_id: sessionId,
    message,
    active: previewActive,
  }));
  if (payload && payload.success === false) {
    $("previewNote").textContent = `预览失败：${payload.message || "未知原因"}`;
    return;
  }
  const out = $("previewOut");
  out.textContent = payload.text || (payload.would_inject ? "" : "（该会话当前不会注入任何内容）");
  out.classList.toggle("empty", !payload.text);

  const parts = [];
  if (payload.strength !== undefined) {
    parts.push(`本次${previewActive ? "主动" : "被动"}强度 ${payload.strength}，让位概率 ${payload.override_probability ?? 0}%`);
  }
  if (payload.length_rule) parts.push("已带字数要求");
  $("previewNote").textContent = parts.join(" · ");
}

async function importBackup() {
  if (!(await askConfirm("把 backup/ 里的旧版备份合并进当前数据？同名特征会保留更高熟练度，手动记忆也会一起导入。"))) return;
  const payload = unwrap(await Bridge.apiPost("import", { mode: "merge" }));
  if (payload && payload.sessions !== undefined) {
    const manual = payload.manual ? `、手动 ${payload.manual} 条` : "";
    showToast(`导入完成：${payload.sessions} 个会话、${payload.traits} 条特征${manual}`);
    await load();
  } else {
    showToast("导入失败，请查看日志");
  }
}

async function exportData() {
  try {
    const data = unwrap(await Bridge.apiGet("export"));
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `qunliaojiyi-backup-${new Date().toISOString().slice(0, 10)}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    showToast("已导出数据（含手动记忆）");
  } catch (err) {
    console.error("导出失败:", err);
    showToast("导出失败，请查看日志");
  }
}

// ── 特征编辑 / 添加 ─────────────────────────────────────────────────

let pendingEdit = null;

function setProficiency(value) {
  const prof = Math.max(0, Math.min(100, Math.round(Number(value) || 0)));
  $("editProficiency").value = String(prof);
  $("editProfValue").textContent = String(prof);
  $("editProficiency").style.setProperty("--prof", `${prof}%`);
}

function openEdit(sessionId, trait, source) {
  pendingEdit = {
    sessionId,
    oldContent: trait.content || "",
    source: source || (trait.source === "manual" ? "manual" : "learned"),
    mode: "edit",
  };
  $("editTitle").textContent = "编辑特征";
  $("editContent").value = trait.content || "";
  $("editCategory").value = trait.category === "situational" ? "situational" : "stable";
  $("editContext").value = trait.context || "";
  $("editTriggers").value = Array.isArray(trait.triggers) ? trait.triggers.join(",") : "";
  setProficiency(trait.proficiency ?? 0);
  $("editHint").textContent = pendingEdit.source === "manual"
    ? "手动记忆存在独立的 manual_traits.json 里，自动学习不会覆盖或删除它。"
    : "学习特征会在下一轮学习时被整体重建，只有同名条目能保住熟练度累积。";
  $("editBatch").hidden = true;
  $("editModal").hidden = false;
  $("editContent").focus();
}

function openAdd(sessionId) {
  pendingEdit = { sessionId, oldContent: "", source: "manual", mode: "add" };
  const cap = (lastStatus && lastStatus.manual_max) || 50;
  const session = ((lastStatus && lastStatus.sessions) || []).find((s) => s.session_id === sessionId);
  const used = (session && session.manual ? session.manual.length : 0);
  $("editTitle").textContent = `添加手动记忆 · ${nameOf(sessionId)}`;
  $("editContent").value = "";
  $("editCategory").value = "stable";
  $("editContext").value = "";
  $("editTriggers").value = "";
  setProficiency(60);
  $("editHint").textContent = `该会话已有 ${used}/${cap} 条手动记忆。手动记忆不占学习的存储名额、不会被学习覆盖，但注入时和学习风格共用一份名额。`;
  $("editBatch").hidden = false;
  $("editModal").hidden = false;
  $("editContent").focus();
}

function closeEdit() {
  $("editModal").hidden = true;
  pendingEdit = null;
}

async function saveEdit() {
  if (!pendingEdit) return;
  const content = $("editContent").value.trim();
  if (!content) { showToast("内容不能为空"); return; }
  const triggers = parseTriggers($("editTriggers").value);
  const category = $("editCategory").value;
  const context = $("editContext").value.trim();
  const proficiency = Number($("editProficiency").value);
  const isAdd = pendingEdit.mode === "add";

  const body = isAdd
    ? {
        action: "add_trait",
        session_id: pendingEdit.sessionId,
        content,
        category,
        context,
        triggers,
        proficiency,
      }
    : {
        action: "update_trait",
        session_id: pendingEdit.sessionId,
        content: pendingEdit.oldContent,
        source: pendingEdit.source,
        new_content: content,
        category,
        context,
        triggers,
        proficiency,
      };

  const payload = unwrap(await Bridge.apiPost("manage", body));
  if (payload && payload.success === false) {
    showToast(payload.message || "保存失败");
    return;
  }
  closeEdit();
  showToast(isAdd ? "已添加手动记忆" : "已保存");
  await load();
}

// ── 批量添加 ────────────────────────────────────────────────────────

let batchSessionId = "";

function openBatch(sessionId) {
  if (!sessionId) { showToast("先选一个会话"); return; }
  batchSessionId = sessionId;
  $("batchText").value = "";
  $("batchCategory").value = $("editCategory").value || "stable";
  $("batchHint").textContent = `目标会话：${nameOf(sessionId)}`;
  $("editModal").hidden = true;
  $("batchModal").hidden = false;
  $("batchText").focus();
}

function closeBatch() {
  $("batchModal").hidden = true;
  batchSessionId = "";
}

async function saveBatch() {
  if (!batchSessionId) return;
  const text = $("batchText").value;
  if (!text.trim()) { showToast("内容不能为空"); return; }
  const payload = unwrap(await Bridge.apiPost("manage", {
    action: "add_traits_batch",
    session_id: batchSessionId,
    text,
    category: $("batchCategory").value,
    proficiency: 60,
  }));
  if (payload && payload.success === false) {
    showToast(payload.message || "添加失败");
    return;
  }
  const added = payload.added || [];
  const failed = payload.failed || [];
  closeBatch();
  if (failed.length) {
    showToast(`添加 ${added.length} 条，${failed.length} 条失败：${failed[0].reason}`);
  } else {
    showToast(`已添加 ${added.length} 条手动记忆`);
  }
  await load();
}

// ── 新建会话记忆 ────────────────────────────────────────────────────

function newSessionId() {
  const platform = $("newPlatform").value.trim();
  const kind = $("newKind").value;
  const target = $("newTarget").value.trim();
  if (!platform || !target) return "";
  return `${platform}:${kind}:${target}`;
}

function updateNewPreview() {
  const sid = newSessionId();
  $("newPreview").textContent = sid ? `拼出的会话 ID：${sid}` : "拼出的会话 ID：—";
}

function openNewSession() {
  const platforms = (lastStatus && lastStatus.platforms) || [];
  const select = $("newPlatform");
  if (platforms.length) {
    select.innerHTML = platforms
      .map((p) => `<option value="${escAttr(p)}">${esc(p)}</option>`)
      .join("");
  } else {
    select.innerHTML = '<option value="">（还没有已知平台，先让机器人在群里说句话）</option>';
  }
  $("newTarget").value = "";
  $("newKind").value = "GroupMessage";
  updateNewPreview();
  $("newModal").hidden = false;
  if (platforms.length) $("newTarget").focus();
}

function closeNewSession() {
  $("newModal").hidden = true;
}

async function submitNewSession() {
  const sid = newSessionId();
  if (!sid) { showToast("先选平台实例并填群号"); return; }
  const known = (lastStatus && lastStatus.known_ids) || [];
  if (!known.includes(sid)) {
    // 平台实例选错会造出一个永远不生效的僵尸会话，所以未知会话必须二次确认
    const ok = await askConfirm(`会话 ${sid} 目前还没出现过，确认要给它加记忆？填错平台实例的话这些记忆永远不会生效。`);
    if (!ok) return;
  }
  closeNewSession();
  openAdd(sid);
}

// ── 启动 ────────────────────────────────────────────────────────────

Bridge.ready().then(() => {
  restoreTab();
  $("nav").addEventListener("click", (e) => {
    const btn = e.target.closest(".nav-item");
    if (btn) switchTab(btn.dataset.tab);
  });
  $("refresh").addEventListener("click", load);
  $("importBtn").addEventListener("click", importBackup);
  $("exportBtn").addEventListener("click", exportData);
  $("newSessionBtn").addEventListener("click", openNewSession);
  $("previewBtn").addEventListener("click", preview);
  // 预览会话选择器：输入群号即过滤，回车选第一条，Esc 取消
  const previewInput = $("previewSessionInput");
  const previewMenu = $("previewMenu");
  if (previewInput && previewMenu) {
    previewInput.addEventListener("focus", openPreviewMenu);
    previewInput.addEventListener("input", openPreviewMenu);
    previewInput.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        closePreviewMenu();
        previewInput.blur();
      } else if (e.key === "Enter") {
        const first = previewMenu.querySelector(".picker-item");
        if (first) {
          e.preventDefault();
          selectPreview(first.dataset.id);
        }
      }
    });
    // 用 mousedown 而不是 click：click 之前 input 会先失焦，菜单已经关掉了
    previewMenu.addEventListener("mousedown", (e) => {
      const item = e.target.closest(".picker-item");
      if (!item) return;
      e.preventDefault();
      selectPreview(item.dataset.id);
    });
    previewInput.addEventListener("blur", () => {
      setTimeout(() => {
        if (!previewMenu.hidden) closePreviewMenu();
      }, 120);
    });
  }
  // 24 小时图切「金额 / 次数」：只换这一张图，不重新请求
  const hourSeg = $("hourMetric");
  if (hourSeg) {
    hourSeg.addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-metric]");
      if (!btn) return;
      hourMetric = btn.dataset.metric === "calls" ? "calls" : "cost";
      [...hourSeg.querySelectorAll("button")].forEach((b) => {
        b.classList.toggle("on", b === btn);
      });
      if (lastStatus) renderHours(lastStatus.usage || {});
    });
  }

  // 预览档位：被动 / 主动，主动插话用的强度是另一档
  $("previewMode").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-active]");
    if (!btn) return;
    previewActive = btn.dataset.active === "1";
    [...$("previewMode").querySelectorAll("button")].forEach((b) => {
      b.classList.toggle("on", b === btn);
    });
    $("previewNote").textContent = `已切到${previewActive ? "主动" : "被动"}档，点「预览」看实际会注入什么`;
  });
  $("editSave").addEventListener("click", saveEdit);
  $("editCancel").addEventListener("click", closeEdit);
  $("editBatch").addEventListener("click", () => openBatch(pendingEdit && pendingEdit.sessionId));
  $("editProficiency").addEventListener("input", (e) => setProficiency(e.target.value));
  $("editModal").addEventListener("click", (e) => {
    if (e.target === $("editModal")) closeEdit();
  });
  $("batchSave").addEventListener("click", saveBatch);
  $("batchCancel").addEventListener("click", closeBatch);
  $("batchModal").addEventListener("click", (e) => {
    if (e.target === $("batchModal")) closeBatch();
  });
  $("newCancel").addEventListener("click", closeNewSession);
  $("newNext").addEventListener("click", submitNewSession);
  ["newPlatform", "newKind", "newTarget"].forEach((id) => {
    $(id).addEventListener("input", updateNewPreview);
    $(id).addEventListener("change", updateNewPreview);
  });
  $("newModal").addEventListener("click", (e) => {
    if (e.target === $("newModal")) closeNewSession();
  });

  // 搜索：输入防抖 300ms，回到第一页
  let searchTimer = null;
  $("search").addEventListener("input", (e) => {
    const value = e.target.value;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      view.q = value.trim();
      view.page = 1;
      load();
    }, 300);
  });
  $("sort").addEventListener("change", (e) => {
    view.sort = e.target.value;
    view.page = 1;
    load();
  });
  $("prevPage").addEventListener("click", () => {
    if (view.page > 1) { view.page -= 1; load(); }
  });
  $("nextPage").addEventListener("click", () => {
    view.page += 1;
    load();
  });

  // 全局清空：三个范围分开，避免一次点击把手工加的东西也毁掉
  $("clearLearned").addEventListener("click", async () => {
    if (!(await askConfirm("清空所有会话的自动学习产出与学习记录？手动记忆和待学习消息都不动。"))) return;
    if (await manageWithMessage({ action: "clear_all", scope: "learned" }, "已清空学习产出")) {
      await load();
    }
  });
  $("clearManual").addEventListener("click", async () => {
    if (!(await askConfirm("清空所有手动添加的记忆？这些是人工写的，删掉无法恢复。"))) return;
    if (await manageWithMessage({ action: "clear_all", scope: "manual" }, "已清空手动记忆")) {
      await load();
    }
  });
  $("clearAll").addEventListener("click", async () => {
    if (!(await askConfirm("确认清空所有会话的学习产出与手动记忆？此操作不可恢复。"))) return;
    if (await manageWithMessage({ action: "clear_all", scope: "all" }, "已清空全部")) {
      await load();
    }
  });
  $("resetRuntime").addEventListener("click", async () => {
    if (await manage({ action: "reset_runtime" })) { showToast("已恢复配置值"); await load(); }
  });
  $("confirmOk").addEventListener("click", () => closeConfirm(true));
  $("confirmCancel").addEventListener("click", () => closeConfirm(false));
  $("confirmModal").addEventListener("click", (e) => {
    if (e.target === $("confirmModal")) closeConfirm(false);
  });

  // 设置项：滑块拖动时实时更新显示与联动文案，松手（change）才落库
  $("settings").addEventListener("input", (e) => {
    const el = e.target;
    if (!el.dataset || !el.dataset.key) return;
    if (el.type === "range") updateSlider(el);
    if (STYLE_KEYS.includes(el.dataset.key)) updateStyleNote();
  });

  $("settings").addEventListener("change", async (e) => {
    const el = e.target;
    if (!el.dataset || !el.dataset.key) return;
    const key = el.dataset.key;
    const parsed = readSettingValue(el);
    if (parsed.error) {
      setSettingMsg(`${key}：${parsed.error}`);
      showToast(`${key}：${parsed.error}`);
      return;
    }
    if (parsed.clamped !== null && parsed.clamped !== undefined) {
      el.value = String(parsed.clamped);
      showToast(`${key} 超出允许范围，已按 ${parsed.clamped} 保存`);
    }
    // 搜索模式先切联动，不必等 load() 回来
    if (key === "web_search_mode") applySearchMode(el.value);
    await saveSetting(key, parsed.value);
  });

  // 记住哪些会话是展开的，刷新后还原
  $("sessions").addEventListener("toggle", (e) => {
    const el = e.target;
    if (!el.classList || !el.classList.contains("session")) return;
    const sid = el.dataset.sid;
    if (el.open) openSessions.add(sid);
    else openSessions.delete(sid);
  }, true);

  // 会话操作（事件委托）
  $("sessions").addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-act]");
    // 输入类控件走自己的 change 事件，别在这里当按钮处理
    if (!btn || ["INPUT", "SELECT", "TEXTAREA"].includes(btn.tagName)) return;
    e.stopPropagation();
    const act = btn.dataset.act;
    const sid = btn.dataset.sid;
    if (act === "pin") {
      // 按钮在 <summary> 里，不拦默认行为会把会话卡片一起折叠
      e.preventDefault();
      return togglePin(sid);
    }
    if (act === "learn") return learn(sid);
    if (act === "add_trait") return openAdd(sid);
    if (act === "clear_history") {
      if (await manage({ action: "clear_session", session_id: sid, scope: "history" })) {
        showToast("已清空待学习记录");
        await load();
      }
      return;
    }
    if (act === "clear_learned") {
      if (!(await askConfirm("清空该会话学到的稳定风格、场景化表达和学习记录？手动记忆和待学习消息都保留，清完可以留着消息重新学。"))) return;
      if (await manageWithMessage({ action: "clear_session", session_id: sid, scope: "learned" }, "已清空学习产出")) await load();
      return;
    }
    if (act === "clear_manual") {
      if (!(await askConfirm("清空该会话所有手动添加的记忆？这是人工写的，删掉无法恢复；学习产出不受影响。"))) return;
      if (await manageWithMessage({ action: "clear_session", session_id: sid, scope: "manual" }, "已清空手动记忆")) await load();
      return;
    }
    if (act === "delete_trait") {
      const label = btn.dataset.source === "manual" ? "手动记忆" : "学习特征";
      if (!(await askConfirm(`删除这条${label}：「${btn.dataset.content}」？`))) return;
      if (await manage({ action: "delete_trait", session_id: sid, content: btn.dataset.content, source: btn.dataset.source })) {
        showToast("已删除");
        await load();
      }
      return;
    }
    if (act === "edit_trait") {
      const session = (lastStatus?.sessions || []).find((s) => s.session_id === sid);
      const trait = [...(session?.manual || []), ...(session?.stable || []), ...(session?.situational || [])]
        .find((t) => (t.content || "") === btn.dataset.content);
      if (trait) openEdit(sid, trait, btn.dataset.source);
      else showToast("没找到这条特征，刷新后再试");
    }
  });

  // 熟练度进度条点击 → 直接进编辑弹窗改
  $("sessions").addEventListener("click", (e) => {
    const bar = e.target.closest(".prof-bar");
    if (!bar) return;
    const row = bar.closest("li");
    const edit = row && row.querySelector('[data-act="edit_trait"]');
    if (edit) edit.click();
  });

  // 学习库：分组置顶 / 条目审核 / 记住分组的折叠状态
  // 逐个判空：浏览器缓存了旧版 index.html 时，绑定到不存在的节点会让整个面板初始化中断。
  const traitsBox = $("traits");
  if (traitsBox) {
    traitsBox.addEventListener("click", async (e) => {
      // 置顶按钮在 <summary> 里，不 preventDefault 会把整个分组一起折叠
      const pin = e.target.closest("[data-pact='pin']");
      if (pin) {
        e.preventDefault();
        e.stopPropagation();
        await togglePin(pin.dataset.sid);
        return;
      }
      const btn = e.target.closest("[data-tact]");
      if (btn) {
        await reviewTrait(btn.dataset.tact, btn.dataset.sid, btn.dataset.content);
        return;
      }
      // 点 summary 时 details.open 还是旧值，取反就是切换后的状态
      const summary = e.target.closest(".trait-group > summary");
      if (summary) {
        const group = summary.parentElement;
        traitGroupState.set(group.dataset.gsid, !group.open);
      }
    });
  }

  const traitSearch = $("traitSearch");
  if (traitSearch) {
    traitSearch.addEventListener("input", (e) => {
      // 纯本地过滤：学习库一次最多几百条，没必要为搜索再跑一趟服务端
      view.traitQuery = e.target.value;
      renderTraits(traitGroups);
    });
  }

  const traitStatusSelect = $("traitStatus");
  if (traitStatusSelect) {
    traitStatusSelect.addEventListener("change", async (e) => {
      view.traitStatus = e.target.value || "";
      await loadTraits();
    });
  }

  const approveAllBtn = $("approveAllBtn");
  if (approveAllBtn) {
    approveAllBtn.addEventListener("click", async () => {
      if (!(await askConfirm("把当前所有待审条目一次性批准为生效？"))) return;
      await reviewTrait("approve_all", "", "");
    });
  }

  // 人格档案：搜索 / 选中 / 置顶 / 保存 / 重新生成
  const personaSearch = $("personaSearch");
  if (personaSearch) {
    personaSearch.addEventListener("input", (e) => {
      view.personaQuery = e.target.value;
      renderPersonaList();
    });
  }

  const personaBox = $("personaList");
  if (personaBox) {
    personaBox.addEventListener("click", async (e) => {
      const pin = e.target.closest("[data-pact='pin']");
      if (pin) {
        e.stopPropagation();
        await togglePin(pin.dataset.sid);
        personaLoaded = false;
        await ensurePersona(true);
        return;
      }
      const item = e.target.closest(".persona-item");
      if (item && item.dataset.sid !== personaCurrent) await openPersona(item.dataset.sid);
    });
  }

  const savePersonaBtn = $("savePersona");
  if (savePersonaBtn) savePersonaBtn.addEventListener("click", savePersona);

  const rebuildBtn = $("rebuildPersona");
  if (rebuildBtn) rebuildBtn.addEventListener("click", () => rebuildPersona("one"));

  const rebuildAllBtn = $("rebuildAllPersona");
  if (rebuildAllBtn) rebuildAllBtn.addEventListener("click", () => rebuildPersona("all"));

  const copyBtn = $("copyPersona");
  if (copyBtn) copyBtn.addEventListener("click", copyPersona);

  const downloadBtn = $("downloadPersona");
  if (downloadBtn) downloadBtn.addEventListener("click", downloadPersona);

  // 会话级开关：学习 / 注入 / 主动回复 / 联网搜索三态
  $("sessions").addEventListener("change", async (e) => {
    const el = e.target;
    if (!el.dataset || !el.dataset.act) return;
    const sid = el.dataset.sid;
    if (el.dataset.act === "toggle_learn") {
      await manage({ action: "set_session_override", session_id: sid, learn: el.checked });
      showToast(el.checked ? "已恢复该群学习" : "已暂停该群学习");
      await load();
    } else if (el.dataset.act === "toggle_inject") {
      await manage({ action: "set_session_override", session_id: sid, inject: el.checked });
      showToast(el.checked ? "已恢复该群注入" : "已关闭该群注入");
      await load();
    } else if (el.dataset.act === "toggle_active") {
      await manageWithMessage(
        { action: "set_active", session_id: sid, enabled: el.checked },
        el.checked ? "已开启该群主动回复" : "已关闭该群主动回复",
      );
      await load();
    } else if (el.dataset.act === "set_search") {
      // 三态用字符串传，不用 null：宿主桥接层对 null 的处理不可控，
      // 万一丢键就变成「不改」，用户选「跟随全局」会静默失效且看不出来。
      const raw = el.value;
      const mode = raw === "" ? "inherit" : raw === "on" ? "on" : "off";
      await manageWithMessage(
        { action: "set_session_override", session_id: sid, search_mode: mode },
        mode === "inherit"
          ? "已改为跟随全局"
          : mode === "on"
            ? "已强制开启本群搜索"
            : "已强制关闭本群搜索",
      );
      await load();
    }
  });

  load();
});

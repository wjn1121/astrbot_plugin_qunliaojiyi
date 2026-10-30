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
const view = { q: "", page: 1, sort: "traits" };
// 展开过的会话，刷新后恢复，避免「加完记忆一刷新就收起」的割裂感
const openSessions = new Set();

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
    $("version").textContent = `v${data.version || "?"} · 从群聊里学习说话风格，低频提炼、按语境注入`;
    renderStats(data.stats || {}, data.manager || {});
    renderSettings(data.settings || {});
    renderSessions(data.sessions || []);
    fillPreviewSessions(data);
    renderPager(data);
  } catch (err) {
    console.error("加载状态失败:", err);
    showToast("加载失败，请查看日志");
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

// ── 设置（热调）────────────────────────────────────────────────────

const SETTING_DEFS = [
  { key: "enabled", label: "启用群聊记忆", type: "bool" },
  { key: "inject_enabled", label: "启用风格注入", type: "bool" },
  { key: "analysis_interval_seconds", label: "学习间隔（秒）", type: "int", min: 60, step: 60 },
  { key: "min_history_for_analysis", label: "触发学习的最少消息数", type: "int", min: 1, step: 1 },
  { key: "max_messages_per_learn", label: "单次提炼最多消息数", type: "int", min: 1, step: 10 },
  { key: "max_stable_inject", label: "最多注入学习风格", type: "int", min: 0, step: 1 },
  { key: "max_manual_inject", label: "最多注入手动记忆", type: "int", min: 0, step: 1 },
  { key: "max_stable_traits", label: "学习风格存储上限", type: "int", min: 0, step: 5 },
  { key: "enable_situational_inject", label: "启用场景化表达注入", type: "bool" },
  { key: "max_situational_inject", label: "最多注入场景化表达", type: "int", min: 0, step: 1 },
  { key: "max_situational_traits", label: "场景化表达存储上限", type: "int", min: 0, step: 5 },
  { key: "enable_trigger_matching", label: "启用触发词匹配", type: "bool" },
  { key: "situational_similarity_threshold", label: "场景化相似度阈值", type: "float", min: 0, max: 1, step: 0.05 },
  { key: "embedding_timeout_seconds", label: "嵌入请求超时（秒）", type: "float", min: 0.5, max: 30, step: 0.5 },
  { key: "enable_cross_group", label: "启用跨群风格参考", type: "bool" },
  { key: "max_global_styles", label: "最多注入全局风格", type: "int", min: 0, step: 1 },
  { key: "debug_mode", label: "调试日志", type: "bool" },
];

function renderSettings(settings) {
  const overrides = settings._runtime_overrides || {};
  $("settings").innerHTML = SETTING_DEFS.map((def) => {
    const value = settings[def.key];
    const hot = def.key in overrides ? '<i class="hot">已热调</i>' : "";
    if (def.type === "bool") {
      return `<label class="set-row">
        <input type="checkbox" data-key="${def.key}" ${value ? "checked" : ""} />
        <span>${esc(def.label)} ${hot}</span>
      </label>`;
    }
    const attrs = [`type="number"`, `data-key="${def.key}"`, `value="${escAttr(value)}"`];
    if (def.min !== undefined) attrs.push(`min="${def.min}"`);
    if (def.max !== undefined) attrs.push(`max="${def.max}"`);
    if (def.step !== undefined) attrs.push(`step="${def.step}"`);
    return `<label class="set-row">
      <span>${esc(def.label)} ${hot}</span>
      <input ${attrs.join(" ")} />
    </label>`;
  }).join("");
}

async function saveSetting(key, raw, isBool) {
  const value = isBool ? raw : Number(raw);
  try {
    const payload = unwrap(await Bridge.apiPost("manage", {
      action: "set_setting",
      key,
      value,
    }));
    if (payload && payload.success === false) {
      showToast(payload.message || "保存失败");
      return;
    }
    showToast(`已更新 ${key}`);
    await load();
  } catch (err) {
    console.error("保存设置失败:", err);
    showToast("保存失败");
  }
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
  const sid = escAttr(s.session_id);
  const opened = openSessions.has(s.session_id) ? " open" : "";

  const quota = `注入名额：手动 ${limits.manual ?? "?"} 条 · 学习 ${limits.learned ?? "?"} 条`;

  return `<details class="session" data-sid="${sid}"${opened}>
    <summary>
      <b>${esc(s.display_name)}</b>
      <span>学习 ${learnedCount} · 手动 ${manual.length} · 待学习 ${s.history_count ?? 0}</span>
    </summary>
    <div class="body">
      <p class="hint">${esc(stateLine)}<br>${esc(quota)}</p>
      <div class="session-switches">
        <label><input type="checkbox" data-act="toggle_learn" data-sid="${sid}" ${learnOn ? "checked" : ""}/> 学习</label>
        <label><input type="checkbox" data-act="toggle_inject" data-sid="${sid}" ${injectOn ? "checked" : ""}/> 注入</label>
        ${learnOn && injectOn ? "" : '<i class="hot">已单独调整</i>'}
      </div>
      <div class="session-actions">
        <button data-act="add_trait" data-sid="${sid}">+ 手动记忆</button>
        <button data-act="learn" data-sid="${sid}">立即学习</button>
        <button class="ghost" data-act="clear_history" data-sid="${sid}">清空待学习</button>
        <button class="ghost danger" data-act="clear_learned" data-sid="${sid}">清空学习产出</button>
        <button class="ghost danger" data-act="clear_manual" data-sid="${sid}">清空手动记忆</button>
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

function fillPreviewSessions(data) {
  const select = $("previewSession");
  const current = select.value;
  const ids = data.known_ids || [];
  if (!ids.length) {
    select.innerHTML = '<option value="">（还没有任何会话）</option>';
    return;
  }
  select.innerHTML = ids
    .map((id) => `<option value="${escAttr(id)}">${esc(nameOf(id))}</option>`)
    .join("");
  if (current && ids.includes(current)) select.value = current;
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
  }));
  const out = $("previewOut");
  out.textContent = payload.text || (payload.would_inject ? "" : "（该会话当前不会注入任何内容）");
  out.classList.toggle("empty", !payload.text);
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
    const ok = await askConfirm(`会话 ${sid} 目前还没出现过，确认要给它加记忆？\n填错平台实例的话这些记忆永远不会生效。`);
    if (!ok) return;
  }
  closeNewSession();
  openAdd(sid);
}

// ── 启动 ────────────────────────────────────────────────────────────

Bridge.ready().then(() => {
  $("refresh").addEventListener("click", load);
  $("importBtn").addEventListener("click", importBackup);
  $("exportBtn").addEventListener("click", exportData);
  $("newSessionBtn").addEventListener("click", openNewSession);
  $("previewBtn").addEventListener("click", preview);
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
    if (!(await askConfirm("清空所有会话的自动学习产出与待学习记录？手动记忆不受影响。"))) return;
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

  // 设置项：变更即保存
  $("settings").addEventListener("change", (e) => {
    const el = e.target;
    if (!el.dataset || !el.dataset.key) return;
    saveSetting(el.dataset.key, el.type === "checkbox" ? el.checked : el.value, el.type === "checkbox");
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
    if (!btn || btn.tagName === "INPUT") return;
    e.stopPropagation();
    const act = btn.dataset.act;
    const sid = btn.dataset.sid;
    if (act === "learn") return learn(sid);
    if (act === "add_trait") return openAdd(sid);
    if (act === "clear_history") {
      if (await manage({ action: "clear_history", session_id: sid })) { showToast("已清空待学习记录"); await load(); }
      return;
    }
    if (act === "clear_learned") {
      if (!(await askConfirm("清空该会话的学习产出与待学习记录？手动记忆不受影响。"))) return;
      if (await manageWithMessage({ action: "clear_session", session_id: sid, scope: "learned" }, "已清空学习产出")) await load();
      return;
    }
    if (act === "clear_manual") {
      if (!(await askConfirm("清空该会话的手动记忆？删掉无法恢复。"))) return;
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

  // 会话级开关：学习 / 注入
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
    }
  });

  load();
});

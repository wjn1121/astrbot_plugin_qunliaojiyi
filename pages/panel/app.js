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

function showToast(message) {
  const toast = $("toast");
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { toast.hidden = true; }, 3000);
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
    fillPreviewSessions(data.sessions || []);
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
    ["稳定 / 场景", `${stats.stable ?? 0} / ${stats.situational ?? 0}`],
    ["待学习消息", stats.pending_messages ?? 0],
    ["学习间隔", fmtDuration(manager.interval)],
    ["下次学习", manager.running ? fmtDuration(manager.next_run_in) : "已停止"],
    ["上次学习", manager.last_run_at ? fmtTime(manager.last_run_at) : "—"],
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
  { key: "max_stable_inject", label: "最多注入稳定风格", type: "int", min: 0, step: 1 },
  { key: "max_stable_traits", label: "稳定风格存储上限", type: "int", min: 0, step: 5 },
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
  $("sessions").innerHTML = sessions.map((s) => {
    const state = s.learn_state || {};
    const scale = state.last_message_count
      ? ` · ${state.last_message_count} 条 / ${state.last_chars ?? 0} 字 / ${state.last_elapsed ?? 0}s`
      : "";
    const stateLine = state.last_learn_at
      ? `上次学习 ${fmtTime(state.last_learn_at)} · ${state.last_ok ? "成功" : "失败"}${scale} · 累计 ${state.total_learns || 0} 次`
      : "尚未学习";
    const stable = (s.stable || []).map((t) => traitRow(s.session_id, t, false)).join("");
    const situational = (s.situational || []).map((t) => traitRow(s.session_id, t, true)).join("");
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
    return `<details class="session">
      <summary>
        <b>${esc(s.display_name)}</b>
        <span>稳定 ${(s.stable || []).length} · 场景 ${(s.situational || []).length} · 待学习 ${s.history_count ?? 0}</span>
      </summary>
      <div class="body">
        <p class="hint">${esc(stateLine)}</p>
        <div class="session-switches">
          <label><input type="checkbox" data-act="toggle_learn" data-sid="${escAttr(s.session_id)}" ${learnOn ? "checked" : ""}/> 学习</label>
          <label><input type="checkbox" data-act="toggle_inject" data-sid="${escAttr(s.session_id)}" ${injectOn ? "checked" : ""}/> 注入</label>
          ${learnOn && injectOn ? "" : '<i class="hot">已单独调整</i>'}
        </div>
        <div class="session-actions">
          <button data-act="learn" data-sid="${escAttr(s.session_id)}">立即学习</button>
          <button class="ghost" data-act="clear_history" data-sid="${escAttr(s.session_id)}">清空待学习</button>
          <button class="ghost danger" data-act="clear_session" data-sid="${escAttr(s.session_id)}">清空该会话</button>
        </div>
        ${stable ? `<h3>稳定风格（${(s.stable || []).length}）</h3><ul class="sublist">${stable}</ul>` : ""}
        ${situational ? `<h3>场景化表达（${(s.situational || []).length}）</h3><ul class="sublist">${situational}</ul>` : ""}
        ${timeline ? `<h3>学习记录（最近 ${(state.history || []).length} 次）</h3><ul class="sublist">${timeline}</ul>` : ""}
        ${history ? `<h3>最近待学习消息（${s.history_count ?? 0}）</h3><ul class="sublist">${history}</ul>` : ""}
      </div>
    </details>`;
  }).join("");
}

function traitRow(sessionId, trait, situational) {
  const content = trait.content || "";
  const ctx = situational && trait.context ? `<i>${esc(trait.context)}</i>` : "";
  const triggers =
    situational && Array.isArray(trait.triggers) && trait.triggers.length
      ? `<i>触发：${esc(trait.triggers.join("、"))}</i>`
      : "";
  const prof = trait.proficiency !== undefined ? `<i>熟练度 ${esc(trait.proficiency)}</i>` : "";
  return `<li>${esc(content)}${ctx}${triggers}${prof}
    <button class="edit" data-act="edit_trait" data-sid="${escAttr(sessionId)}" data-content="${escAttr(content)}" title="编辑">✎</button>
    <button class="del" data-act="delete_trait" data-sid="${escAttr(sessionId)}" data-content="${escAttr(content)}" title="删除该条">×</button>
  </li>`;
}

function fillPreviewSessions(sessions) {
  const select = $("previewSession");
  const current = select.value;
  select.innerHTML = sessions
    .map((s) => `<option value="${escAttr(s.session_id)}">${esc(s.display_name)}</option>`)
    .join("");
  if (current && sessions.some((s) => s.session_id === current)) select.value = current;
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
  if (!(await askConfirm("把 backup/ 里的旧版备份合并进当前数据？同名特征会保留更高熟练度。"))) return;
  const payload = unwrap(await Bridge.apiPost("import", { mode: "merge" }));
  if (payload && payload.sessions !== undefined) {
    showToast(`导入完成：${payload.sessions} 个会话、${payload.traits} 条特征`);
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
    showToast("已导出数据");
  } catch (err) {
    console.error("导出失败:", err);
    showToast("导出失败，请查看日志");
  }
}

// ── 特征编辑 ────────────────────────────────────────────────────────

let pendingEdit = null;

function openEdit(sessionId, trait) {
  pendingEdit = { sessionId, oldContent: trait.content || "" };
  $("editContent").value = trait.content || "";
  $("editContext").value = trait.context || "";
  $("editTriggers").value = Array.isArray(trait.triggers) ? trait.triggers.join(",") : "";
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
  const triggers = $("editTriggers").value
    .split(/[,，\s]+/)
    .map((x) => x.trim())
    .filter(Boolean);
  const payload = unwrap(await Bridge.apiPost("manage", {
    action: "update_trait",
    session_id: pendingEdit.sessionId,
    content: pendingEdit.oldContent,
    new_content: content,
    context: $("editContext").value.trim(),
    triggers,
  }));
  if (payload && payload.success === false) {
    showToast(payload.message || "保存失败");
    return;
  }
  closeEdit();
  showToast("已保存");
  await load();
}

// ── 启动 ────────────────────────────────────────────────────────────

Bridge.ready().then(() => {
  $("refresh").addEventListener("click", load);
  $("importBtn").addEventListener("click", importBackup);
  $("exportBtn").addEventListener("click", exportData);
  $("previewBtn").addEventListener("click", preview);
  $("editSave").addEventListener("click", saveEdit);
  $("editCancel").addEventListener("click", closeEdit);
  $("editModal").addEventListener("click", (e) => {
    if (e.target === $("editModal")) closeEdit();
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
  $("clearAll").addEventListener("click", async () => {
    if (!(await askConfirm("确认清空所有会话的风格特征与待学习记录？此操作不可恢复。"))) return;
    if (await manage({ action: "clear_all" })) { showToast("已清空全部"); await load(); }
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

  // 会话操作（事件委托）
  $("sessions").addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn || btn.tagName === "INPUT") return;
    e.stopPropagation();
    const act = btn.dataset.act;
    const sid = btn.dataset.sid;
    if (act === "learn") return learn(sid);
    if (act === "clear_history") {
      if (await manage({ action: "clear_history", session_id: sid })) { showToast("已清空待学习记录"); await load(); }
      return;
    }
    if (act === "clear_session") {
      if (!(await askConfirm("确认清空该会话的风格特征与待学习记录？"))) return;
      if (await manage({ action: "clear_session", session_id: sid })) { showToast("已清空该会话"); await load(); }
      return;
    }
    if (act === "delete_trait") {
      if (await manage({ action: "delete_trait", session_id: sid, content: btn.dataset.content })) await load();
      return;
    }
    if (act === "edit_trait") {
      const session = (lastStatus?.sessions || []).find((s) => s.session_id === sid);
      const trait = [...(session?.stable || []), ...(session?.situational || [])]
        .find((t) => (t.content || "") === btn.dataset.content);
      if (trait) openEdit(sid, trait);
      else showToast("没找到这条特征，刷新后再试");
    }
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


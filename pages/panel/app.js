const $ = (id) => document.getElementById(id);

// ── 面板自诊断（1.7.x）────────────────────────────────────────────────
// JS 挂了不能只表现为「这块没刷新」——一片空白，用户没法描述，开发者只能猜。
// 这里把错误直接画在页面顶部：用户截个图，报错就带过来了。
// 注意诊断自己绝对不能再抛异常，否则就从「白屏」变成「白屏 + 崩」。
(function initPanelDiag() {
  const show = (msg) => {
    try {
      let bar = document.getElementById("__panelErr");
      if (!bar) {
        bar = document.createElement("div");
        bar.id = "__panelErr";
        bar.style.cssText =
          "position:fixed;left:0;right:0;top:0;z-index:99999;background:#5c1f1f;" +
          "color:#ffd7d7;padding:8px 12px;font:12px/1.6 ui-monospace,monospace;" +
          "white-space:pre-wrap;max-height:36vh;overflow:auto";
        document.body.appendChild(bar);
      }
      if (bar.textContent.indexOf(msg) === -1) bar.textContent += msg + "\n";
    } catch (_) {
      /* 诊断失败就沉默，不能影响页面本身 */
    }
  };
  window.__panelDiag = show;
  window.addEventListener("error", (e) =>
    show(
      "脚本错误：" +
        e.message +
        " @ " +
        String(e.filename || "").split("/").pop() +
        ":" +
        e.lineno,
    ),
  );
  window.addEventListener("unhandledrejection", (e) => {
    const r = e.reason;
    show("异步错误：" + ((r && (r.message || r)) || "未知"));
  });
})();

// 渲染兜底：某一个面板坏了，至少把原因写在它自己的位置，
// 别连累旁边的面板（以前一个抛错整页都空）
function renderGuard(targetId, label, fn) {
  try {
    fn();
  } catch (err) {
    console.error(label + " 渲染失败:", err);
    const box = $(targetId);
    if (box) {
      box.innerHTML =
        '<p class="empty">' +
        label +
        " 渲染失败：" +
        esc((err && err.message) || String(err)) +
        "</p>";
    }
    if (window.__panelDiag) window.__panelDiag(label + " 渲染失败：" + ((err && err.message) || err));
  }
}

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
// 学习库的搜索/状态/排序已经移到 TRAIT_GF（列头），这里只剩人格档案和分页
const view = { q: "", page: 1, sort: "traits", personaQuery: "" };
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
  bond: "关系与输出",
  feed: "喂食",
  active: "主动回复",
  sessions: "会话与特征",
};

// 当前停在哪一页。只在「刷新后要不要补拉这一页的数据」时用
function currentPane() {
  const el = document.querySelector(".pane.on");
  return el ? el.dataset.pane : "";
}

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
  // 关系数据一直在变（群里聊天就在涨），每次切过来都重拉
  if (tab === "bond") {
    ensureBond();
    syncToneSwitches();
    loadFacts();
  }
  // 投喂榜每次切过来都重拉：群里可能刚有人点过菜
  if (tab === "feed") loadFeed();
  // 主动回复的计数一直在变，切过来就重拉一次
  if (tab === "active") loadActive();
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
    renderOwnerCard(data);
    renderUsage(data);
    renderSettings(data.settings || {});
    // 喂食和主动回复各自一页，参数区也要跟着这份配置重画
    renderFeedSettings(data.settings || {});
    renderPaneSettings("active", "activeSettings", data.settings || {});
    renderSessions(data.sessions || []);
    fillPreviewSessions(data);
    renderPager(data);
    // 预览会话下拉刚填好，「本群今日搜索」要按它重算
    renderUsageSearch(data);
    await loadTraits();
    // 输出层调试的开关要用当前配置当初始值；直接停在关系页时
    // switchTab 早于这里执行，那一次拿不到配置，靠这次补上
    syncToneSwitches();
    // 停在喂食 / 主动回复页时，保存参数引发的重载要顺带把上面的统计也刷新
    if (currentPane() === "feed") await loadFeed();
    if (currentPane() === "active") await loadActive();
  } catch (err) {
    console.error("加载状态失败:", err);
    showToast("加载失败，请查看日志");
  }
}

// ── 学习库（1.4.0 累积链路）──────────────────────────────────────────

let traitGroups = [];

// 学习库拆两条：群聊 / 私聊。默认停群聊（那是主战场）。
// 私聊默认不学，所以那一页更像「开关面板」——列出来让你逐个开。
let traitScope = "group";
const traitMeta = { group: true, private: false, owners: [] };
// 用户手动折叠 / 展开过的分组。记下来是因为默认只展开「有待审」的组，
// 十几个群全展开等于没分组，但用户主动点开的那个不该被每次刷新收回去。
const traitGroupState = new Map();

const TRAIT_STATUS_TEXT = {
  pending: "待审",
  active: "已生效",
  archived: "已归档",
  rejected: "已否决",
};

// 事实记忆的三档。顺序就是「由生到熟」，面板和按钮都按这个顺序走
const TIER_TEXT = { shallow: "浅层", deep: "深层", core: "核心" };

// ── 学习库的列定义 ───────────────────────────────────────────────────
// 状态筛选从「后端过滤」改成「前端多选」：原来一个下拉只能选一种状态，
// 而实际想看的往往是「待审 + 已生效」这种组合。数据量在几百条以内，
// 前端过滤比来回请求划算得多。
const traitFilter = { text: {}, sets: {}, sort: "", dir: "desc" };

function matchTraitFlag(group, flag) {
  if (flag === "hasPending") return Number((group.counts || {}).pending || 0) > 0;
  if (flag === "pinned") return !!group.pinned;
  if (flag === "private") return String(group.session_id || "").startsWith("private");
  return false;
}

const TRAIT_FLAG_TEXT = {
  hasPending: "有待审的群",
  pinned: "置顶的群",
  private: "只看私聊",
};

const TRAIT_GF = {
  bar: "traitFilterBar",
  pop: "traitFilterPop",
  state: traitFilter,
  items: () => traitGroups.flatMap((g) => g.traits || []),
  render: () => renderTraits(traitGroups),
  reset: () => {
    traitFilter.text = {};
    traitFilter.sets = {};
    traitFilter.sort = "";
    traitFilter.dir = "desc";
  },
  columns: [
    {
      col: "who",
      label: "对象",
      type: "text",
      title: "按群 / 内容搜索",
      placeholder: "群号 / 群名 / 特征内容",
      groups: [
        {
          key: "flags",
          title: "附加条件",
          options: Object.keys(TRAIT_FLAG_TEXT).map((key) => ({
            key,
            label: TRAIT_FLAG_TEXT[key],
          })),
          // 这一列筛的是「群」，不是条目，所以计数直接数分组
          count: (_items, key) => traitGroups.filter((g) => matchTraitFlag(g, key)).length,
        },
      ],
    },
    {
      col: "status",
      label: "状态",
      groups: [
        {
          key: "statuses",
          title: "按状态（可多选）",
          options: Object.entries(TRAIT_STATUS_TEXT).map(([key, label]) => ({ key, label })),
          count: (items, key) => items.filter((t) => String(t.status || "active") === key).length,
        },
      ],
    },
    {
      col: "score",
      label: "权重（有效分）",
      type: "sort",
      descLabel: "降序（最可能进注入的）",
      ascLabel: "升序（最边缘的）",
    },
  ],
};

// 子库切换时同步按钮状态、数量和那句说明。
// 数量用「有内容的组」而不是全部：群聊列表里那几十个 0 条空壳不显示，
// 标出来的数字得和列表里能看到的对得上。
function syncTraitScope(all, scoped) {
  const gCount = all.filter((g) => !g.is_private && (g.learned || g.total > 0)).length;
  const pCount = all.filter((g) => g.is_private).length;

  const seg = $("traitScope");
  if (seg) {
    seg.querySelectorAll("button[data-scope]").forEach((btn) => {
      const on = btn.dataset.scope === traitScope;
      btn.classList.toggle("on", on);
      const n = btn.dataset.scope === "private" ? pCount : gCount;
      btn.innerHTML = `${btn.dataset.scope === "private" ? "私聊学习库" : "群聊学习库"}${
        n ? `<span class="seg-count">${n}</span>` : ""
      }`;
    });
  }

  const note = $("traitScopeNote");
  if (!note) return;
  if (traitScope === "private") {
    note.textContent = traitMeta.private
      ? `私聊学习已全局开启：所有私聊都会学（这会持续消耗 token）。下面每个会话仍可单独关。`
      : `私聊默认不学${traitMeta.owners.length ? "（主人自己的私聊例外）" : ""}。` +
        `想学某个人，在下面那一行点「不学」变成「学习中」即可。`;
  } else {
    note.textContent = traitMeta.group
      ? "群聊学习全部开启（默认）。想关某一个群，点它那一行的「学习中」。"
      : "群聊学习已全局关闭。单个群仍可以在这里单独打开。";
  }
}

// 组内按权重排。用 effective_score（按时效打折后的分）而不是原始权重：
// 原始分高但半年没出现过的特征，实际进不了注入，按它排会误导。
function sortTraitsInGroup(traits) {
  const st = gfState(TRAIT_GF);
  if (st.sort !== "score") return traits;
  const mul = st.dir === "asc" ? 1 : -1;
  const score = (t) => Number(t.effective_score ?? t.proficiency ?? 0);
  return [...traits].sort((a, b) => mul * (score(a) - score(b)));
}

async function loadTraits() {
  try {
    // 状态过滤在前端做（多选），所以这里始终取全量。
    // 取全量还有个好处：列头上能显示每种状态各有多少条，
    // 只取待审的话「已生效 32」这个数字就永远显示不出来
    const data = unwrap(await Bridge.apiGet("traits", {}));
    traitGroups = data.groups || [];
    traitMeta.group = data.learn_group_enabled !== false;
    traitMeta.private = !!data.learn_private_enabled;
    traitMeta.owners = data.owners || [];
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
// 在学习库缓存里找一条（判断是不是钉住的那条）。
// 走缓存而不是再请求一次：这个判断只为了给确认框加一句「已钉住不可删」，
// 为它发一次 HTTP 不值得。
function traitItemOf(sessionId, content) {
  for (const group of traitGroups) {
    if (group.session_id !== sessionId) continue;
    for (const item of group.traits || []) {
      if (String(item.content) === String(content)) return item;
    }
  }
  return null;
}

function renderTraits(groups) {
  const box = $("traits");
  if (!box) return;

  // 先按「群聊 / 私聊」这一层切，下面的搜索 / 状态 / 排序都只作用在当前这一页
  const isPrivate = traitScope === "private";
  const scoped = groups.filter((g) => {
    if (!!g.is_private !== isPrivate) return false;
    // 群聊列表过滤掉纯空壳（聊过天但还没学到东西）——几十个 0 条的群只是噪音；
    // 私聊列表**保留**，因为那是「点开学习」的唯一入口
    if (!isPrivate && !g.learned && !(g.total > 0)) return false;
    return true;
  });
  syncTraitScope(groups, scoped);

  const st = gfState(TRAIT_GF);
  const kw = String(st.text.who || "").trim().toLowerCase();
  const statuses = st.sets.statuses || [];
  const flags = st.sets.flags || [];
  const filtered = !!kw || statuses.length > 0 || flags.length > 0;

  const visible = scoped
    .map((g) => {
      // 附加条件是「群」这一级的：不满足就整组不显示
      if (flags.length && !flags.some((f) => matchTraitFlag(g, f))) return null;
      // 搜群号 / 群名命中时整组原样保留：搜群号的人想看这个群里的全部内容，
      // 而不是一个标题留下、条目被内容过滤清空的空壳。
      const bySession =
        !!kw && `${g.session_id} ${g.display_name || ""}`.toLowerCase().includes(kw);
      let traits = g.traits || [];
      if (kw && !bySession) {
        traits = traits.filter((t) =>
          [t.content, t.context, ...(t.triggers || []), ...(t.alias || [])]
            .join(" ")
            .toLowerCase()
            .includes(kw),
        );
      }
      if (statuses.length) {
        traits = traits.filter((t) => statuses.includes(String(t.status || "active")));
      }
      return { ...g, traits: sortTraitsInGroup(traits), bySession };
    })
    .filter(Boolean)
    // 有筛选时，筛完只剩空壳的组不显示——留着会让列表全是「0 条」
    .filter(
      (g) =>
        !filtered ||
        g.traits.length > 0 ||
        // 搜群号命中的组即使条目被状态筛空也要留着，否则用户会以为这个群没数据
        (g.bySession && !statuses.length && !flags.length),
    )
    // 群之间也要有顺序。后端给的是「第一次学习的先后」，新群永远沉在最后，
    // 打开面板根本注意不到。改成：置顶 → 有待审的 → 条目多的。
    // 这三档正好对应「我要找它」「我要处理它」「它内容最多」三种动机
    .sort((a, b) => {
      if (!!a.pinned !== !!b.pinned) return a.pinned ? -1 : 1;
      const pendingA = Number((a.counts || {}).pending || 0);
      const pendingB = Number((b.counts || {}).pending || 0);
      if (pendingA !== pendingB) return pendingB - pendingA;
      return Number(b.total || 0) - Number(a.total || 0);
    });

  // 「全部批准」只在真有待审条目时出现。注意要看**过滤后**的：
  // 筛了「已生效」还挂着「全部批准」，点了会批准一批看不见的东西
  const allBtn = $("approveAllBtn");
  if (allBtn) {
    const hasPending = visible.some((g) =>
      (g.traits || []).some((t) => String(t.status || "active") === "pending"),
    );
    allBtn.style.display = hasPending ? "" : "none";
  }

  gfPaint(TRAIT_GF);

  // 有筛选时给命中数，否则「怎么只剩这几个群」会让人以为数据丢了
  const note = $("traitSearchNote");
  if (note) {
    const cond = gfNoteText(TRAIT_GF);
    if (cond) {
      const hitGroups = visible.filter((g) => (g.traits || []).length).length;
      const hitItems = visible.reduce((n, g) => n + (g.traits || []).length, 0);
      note.hidden = false;
      note.textContent = `${cond}：${hitItems} 条 / ${hitGroups} 个${isPrivate ? "私聊" : "群"}（共 ${scoped.length} 个）`;
    } else {
      note.hidden = true;
      note.textContent = "";
    }
  }

  if (!visible.length) {
    box.innerHTML = `<p class="hint">${
      scoped.length
        ? "没有匹配的条目。"
        : isPrivate
          ? traitMeta.private || traitMeta.owners.length
            ? "还没有私聊内容。私聊会话聊过天之后会出现在这里，可以逐个开关学习。"
            : "私聊学习库默认关闭：没有会话会学。想学某个人，先在这个人的私聊里聊几句，"
              + "他就会出现在这个列表，然后点「开始学习」。"
          : "还没有学到任何内容，先在群里聊几句。"
    }</p>`;
    return;
  }
  box.innerHTML = visible.map(groupCard).join("");
}

function groupOpen(group) {
  // 搜索时全部展开：收着的话搜到了也看不见，等于没搜
  if (String(gfState(TRAIT_GF).text.who || "").trim()) return true;
  if (traitGroupState.has(group.session_id)) return traitGroupState.get(group.session_id);
  // 默认全收起。以前是「有待审的组自动展开」，但十来个群里有七八个待审时
  // 等于整页全开，一屏只看得下两个群——现在是折叠列表，点开哪个看哪个
  return false;
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
  // 「多久没消息」。bot 被踢出群之后插件不会收到通知（拿不到群列表），
  // 只能靠「这个群已经 N 天零消息」来提示——30 天是很稳的信号
  const idle = Number(group.inactive_days ?? 0);
  const staleTag =
    idle >= 30
      ? `<span class="trait-stale" title="bot 可能已经不在这个群了">${Math.round(idle)} 天没消息</span>`
      : "";
  // 删除整组。**置顶的一律不能删**——用户明确要求，也正好当保护标记用。
  // 置顶时**整个按钮不渲染**（而不是 disabled）：一个灰着的按钮既难看，
  // 又容易在窄屏上被误点，直接消失最省事。
  const delBtn = group.pinned
    ? ""
    : `<button type="button" class="pin-btn danger" data-drop="${sid}"
        data-name="${escAttr(group.display_name || sid)}" data-pinned="0"
        title="删除这个会话的全部学习数据">删除</button>`;

  // 学习开关。私聊默认不学，所以这一页的每个会话都得能单独开——
  // 私聊列表里那些「0 条」的组，按钮就是它们唯一的用途
  const learnOn = group.learn_enabled !== false;
  const learnBtn = `<button type="button" class="pin-btn learn-btn${learnOn ? " on" : ""}"
    data-tact="${learnOn ? "learn_off" : "learn_on"}" data-sid="${sid}"
    title="${learnOn ? "点一下停止学习这个会话" : "点一下开始学习这个会话"}">${
      learnOn ? "学习中" : "不学"
    }</button>`;
  return `<details class="trait-group" data-gsid="${sid}"${groupOpen(group) ? " open" : ""}>
    <summary>
      <b>${group.pinned ? "📌 " : ""}${esc(group.display_name)}</b>
      <span class="group-count">${esc(traits.length)} / ${esc(group.total ?? traits.length)} 条</span>
      ${tags.join("")}
      ${staleTag}
      ${learnBtn}
      <button type="button" class="pin-btn${group.pinned ? " on" : ""}" data-pact="pin" data-sid="${sid}"
        title="${group.pinned ? "取消置顶（置顶时不能删这个会话）" : "置顶这个会话，排在列表最前"}">${
        group.pinned ? "已置顶" : "置顶"
      }</button>
      ${delBtn}
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
    btns.push(`<button class="ghost" data-tact="approve" data-sid="${sid}" data-content="${content}" title="批准：这条从下一轮开始参与注入">批准</button>`);
    btns.push(`<button class="ghost danger" data-tact="reject" data-sid="${sid}" data-content="${content}" title="否决：不再注入，但留档可恢复">否决</button>`);
  } else if (status === "active") {
    btns.push(
      `<button class="ghost" data-tact="${t.pinned ? "unpin" : "pin"}" data-sid="${sid}" data-content="${content}"
        title="${t.pinned ? "取消钉住，之后可以被删除" : "钉住这条：优先注入，且不能直接删除"}">${t.pinned ? "取消钉住" : "钉住"}</button>`,
    );
    btns.push(`<button class="ghost danger" data-tact="reject" data-sid="${sid}" data-content="${content}" title="否决：不再注入，留档可恢复">否决</button>`);
  } else {
    btns.push(`<button class="ghost" data-tact="restore" data-sid="${sid}" data-content="${content}" title="恢复成生效状态，重新参与注入">恢复</button>`);
  }
  // 单条删除。钉住的不给按钮——不是隐藏，而是换成一句说明，
  // 否则用户会以为「这条没有删除功能」而去找别的地方
  btns.push(
    t.pinned
      ? '<span class="hint" title="钉住的一律不能删，先取消钉住">已钉住 · 不可删</span>'
      : `<button class="ghost danger" data-traitdel="${content}" data-sid="${sid}" title="删掉这条学到的内容（不可恢复）">删除</button>`,
  );
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

// 删除一个会话的全部学习数据。
// 二次确认是硬要求：这个动作会一次性带走学到的东西 + 手动记忆 + 待学习消息。
// ``alsoSessions`` 给「会话与特征」页用——那一页刷新的是另一份数据。
async function dropTraitGroup(sid, name, pinned, alsoSessions = false) {
  if (pinned) {
    showToast("置顶的一律不能删，先取消置顶");
    return false;
  }
  const ok = await askConfirm(
    `删除「${name}」的全部学习数据？\n\n` +
      `会一起清掉：学到的特征、手动记忆、待学习消息、学习状态。\n` +
      `**不可恢复**，但不会动这个群的聊天记录本身。`,
  );
  if (!ok) return false;
  const res = unpack(await Bridge.apiPost("review", { action: "drop_session", session_id: sid }));
  showToast(res.message || (res.ok ? "已删除" : "删除失败"));
  await loadTraits();
  if (alsoSessions) await load();
  return res.ok;
}

// 删除单条学习内容。同样二次确认。
async function deleteTraitItem(sid, content, pinned) {
  if (pinned) {
    showToast("这条已钉住，先取消钉住才能删");
    return;
  }
  const preview = String(content || "").slice(0, 60);
  const ok = await askConfirm(
    `删除这条学习内容？\n\n「${preview}」\n\n删除后不可恢复。`,
  );
  if (!ok) return;
  const res = unpack(
    await Bridge.apiPost("review", { action: "delete_trait", session_id: sid, content }),
  );
  showToast(res.message || (res.ok ? "已删除" : "删除失败"));
  await loadTraits();
}

// 清理「bot 可能已经不在了」的会话：30 天没有新消息。
// 插件拿不到平台的群列表，只能这么推测——所以先确认，且置顶的一律跳过。
async function purgeInactiveTraits() {
  const ok = await askConfirm(
    "清理 30 天没有新消息的会话？\n\n" +
      "通常就是 bot 已经被踢出 / 群已经废弃的那些。\n" +
      "置顶的会跳过；删除不可恢复。",
  );
  if (!ok) return;
  const res = unpack(
    await Bridge.apiPost("review", { action: "purge_inactive", days: 30 }),
  );
  showToast(res.message || (res.ok ? "已清理" : "清理失败"));
  await loadTraits();
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

// 概览页的「主人与学习」卡。
// 这三件事影响四个模块却都藏在配置里，用户回到面板第一眼该看见，
// 而不是「为什么私聊不学」去翻配置才发现。
function renderOwnerCard(data) {
  const box = $("ownerCard");
  if (!box) return;
  const owners = data.owners || [];
  const groupOn = data.learn_group_enabled !== false;
  const privOn = !!data.learn_private_enabled;
  const rows = [
    {
      icon: "👑",
      label: "主人 QQ",
      value: owners.length ? owners.join(" · ") : "未设置",
      note: owners.length
        ? "关系不封顶 · 事实上限 300 条 · 私聊默认学 · 一键清理跳过 · 可单独清理"
        : "去「参数 → 全局」填 QQ 号。填一次，关系 / 事实 / 学习 / 清理四个模块一起生效",
      cls: owners.length ? "on" : "off",
    },
    {
      icon: "💬",
      label: "群聊学习库",
      value: groupOn ? "全部开启" : "已关闭",
      note: groupOn
        ? "所有群聊自动学「这个群怎么说话」，进群就开始积累"
        : "群聊不学风格，只记消息。没有学习请求 = 这块 token 归零",
      cls: groupOn ? "on" : "off",
    },
    {
      icon: "🔒",
      label: "私聊学习库",
      value: privOn ? "全部开启" : owners.length ? "仅主人" : "全部关闭",
      note: privOn
        ? "所有私聊都会学，token 消耗会明显增加"
        : owners.length
          ? "私聊默认不学；主人自己的私聊例外。其余可在「学习库 → 私聊」单独开某一个"
          : "私聊默认不学。要学某个人，去「学习库 → 私聊」单独开",
      cls: privOn ? "warn" : "on",
    },
  ];
  box.innerHTML = rows
    .map(
      (r) => `<div class="owner-row ${r.cls}">
        <span class="owner-icon">${r.icon}</span>
        <div class="owner-main">
          <b>${esc(r.label)}</b>
          <span class="owner-value">${esc(r.value)}</span>
          <p class="owner-note">${esc(r.note)}</p>
        </div>
      </div>`,
    )
    .join("");
}

function renderStats(stats, manager) {
  const metrics = stats.metrics || {};
  // 第三个元素是悬停提示（面板里那种白底小字）。数字为什么是这个值，
  // 光看标签猜不出来，所以每个都写一句
  const items = [
    ["会话数", stats.sessions ?? 0, "本插件见过的群和私聊总数，去「会话与特征」看明细"],
    ["风格特征", stats.traits ?? 0, "学习库里的条目总数，含待审和已否决的"],
    ["学习 / 手动", `${stats.learned_traits ?? 0} / ${stats.manual_traits ?? 0}`, "自动学到的条数 / 你自己手动加的条数"],
    ["稳定 / 场景", `${stats.stable ?? 0} / ${stats.situational ?? 0}`, "稳定风格跨场景都用，场景化表达只在语境匹配时注入"],
    ["待学习消息", stats.pending_messages ?? 0, "已经收下、还没送去提炼的消息数。攒够门槛才学"],
    ["学习间隔", fmtDuration(manager.interval), "两次自动学习之间至少隔这么久，这是成本的主要控制点"],
    ["下次学习", manager.running ? fmtDuration(manager.next_run_in) : "已停止", "距离下一次自动学习还有多久"],
    ["状态", manager.running ? "运行中" : "已停止", "调度器是否在跑。停用插件或关掉总开关都会变成已停止"],
  ];
  $("stats").innerHTML = items
    .map(
      ([k, v, tip]) =>
        `<div class="stat"${tip ? ` title="${escAttr(tip)}"` : ""}><span>${esc(k)}</span><b>${esc(
          v,
        )}</b></div>`,
    )
    .join("");

  // 累计成本：让「这套机制到底花了多少」可见
  const calls = metrics.learn_calls ?? 0;
  const ok = metrics.learn_ok ?? 0;
  const fail = metrics.learn_fail ?? 0;
  const chars = metrics.chars_analyzed ?? 0;
  $("cost").innerHTML = [
    ["累计学习次数", calls, "从装上到现在一共发起了多少次学习请求"],
    ["成功 / 失败", `${ok} / ${fail}`, "失败通常是模型报错或超时，不计费"],
    ["累计分析消息", metrics.messages_analyzed ?? 0, "这些请求一共读了多少条群消息"],
    ["累计分析字数", chars >= 10000 ? `${(chars / 10000).toFixed(1)} 万` : chars, "读进来的纯文字量，token 基本按这个算"],
    ["累计耗时", fmtDuration(Math.round(metrics.elapsed_total ?? 0)), "所有学习请求加起来花了多少时间"],
  ]
    .map(
      ([k, v, tip]) =>
        `<div class="stat"${tip ? ` title="${escAttr(tip)}"` : ""}><span>${esc(k)}</span><b>${esc(
          v,
        )}</b></div>`,
    )
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
    <div class="usage-hero" title="按 DeepSeek 牌价估算，高峰时段翻倍。本插件自己发起的请求才算，不含你和别人正常聊天">
      <span>今日金额</span><b>¥${esc(fmtMoney(cost))}</b></div>
    <div class="stat" title="今天一共发起了几次模型请求。搜索单独计数，不占这里">
      <span>今日调用</span><b>${esc(today.calls ?? 0)}</b></div>
    <div class="stat" title="联网搜索的次数。按次单独计费，不占 token">
      <span>今日搜索</span><b>${esc(today.search_calls ?? 0)}</b></div>`;

  $("usageTokens").innerHTML = [
    ["缓存命中输入", fmtCount(tokens.input_cached), "命中了前缀缓存的部分，价格只有未命中的三十分之一"],
    ["未命中输入", fmtCount(tokens.input_other), "没命中缓存、按原价算的输入。改 system prompt 会让这个数暴涨"],
    ["输出", fmtCount(tokens.output), "模型吐出来的字。输出价是缓存命中输入价的九十倍"],
  ]
    .map(
      ([k, v, tip]) =>
        `<div class="stat"${tip ? ` title="${escAttr(tip)}"` : ""}><span>${esc(k)} token</span><b>${esc(
          v,
        )}</b></div>`,
    )
    .join("");

  renderUsageSource(usage);
  renderHours(usage);
  renderBars(usage.days || []);
}

// ── 今日消耗构成 ─────────────────────────────────────────────────────
//
// 分成两块钱：**回复**（bot 说出去的每句话）和**后台**（插件自己发的请求）。
// 这么分是因为它们的开关是分开的——回复由群友决定，后台由配置决定，
// 混在一起看，用户没法判断「关掉哪个能省钱」。
//
// 每块钱先给总额（次数 / 金额 / token），再往下列各自的下级项目。
// 只列今天真的有数据的项目，没发生的来源不占位置。

const USAGE_SOURCE_META = {
  reply: ["👤 被叫回复", "有人 @bot、回复 bot 的消息，或者私聊"],
  active: ["🗣 主动插话", "插件自己找时机接的话，可以整块关掉"],
  bot: ["💬 其他回复", "群里没点名但触发了回复，或认不出来的来源"],
  learn: ["📚 学习风格", "定时提炼群聊风格的后台请求"],
  profile: ["🪞 人物画像", "给某个人重写「他是谁」的那次请求"],
  judge: ["🔎 插话判定", "让模型判断这轮该不该插话"],
};

const USAGE_BLOCKS = [
  {
    key: "chat",
    title: "💬 回复",
    hint: "bot 说出去的每句话，一轮就是一次完整请求",
    sources: ["reply", "active", "bot"],
  },
  {
    key: "back",
    title: "🧠 后台",
    hint: "插件自己发起的请求，跟聊天无关，可以整块关掉",
    sources: ["learn", "profile", "judge"],
  },
];

function usageBucket(bySource, key) {
  const raw = bySource[key] || {};
  const tokens = raw.tokens || {};
  return {
    key,
    calls: Number(raw.calls) || 0,
    cost: Number(raw.cost) || 0,
    tokens:
      (Number(tokens.input_other) || 0) +
      (Number(tokens.input_cached) || 0) +
      (Number(tokens.output) || 0),
  };
}

function renderUsageSource(usage) {
  const box = $("usageSource");
  if (!box) return;
  const bySource = usage.by_source || {};

  const blocks = USAGE_BLOCKS.map((block) => {
    const rows = block.sources
      .map((key) => usageBucket(bySource, key))
      .filter((row) => row.calls > 0);
    return {
      ...block,
      rows,
      calls: rows.reduce((sum, r) => sum + r.calls, 0),
      cost: rows.reduce((sum, r) => sum + r.cost, 0),
      tokens: rows.reduce((sum, r) => sum + r.tokens, 0),
    };
  });
  const live = blocks.filter((b) => b.calls > 0);

  if (!live.length) {
    box.innerHTML =
      `<p class="hint">今天还没有按来源的记录。这一版起会把「回复 / 后台」分开记，` +
      `更早的消耗都在「其他回复」里。</p>`;
    return;
  }

  // 占比按金额算；万一每笔 token 都是 0（只有空 usage），退回按次数算
  const totalCost = live.reduce((sum, b) => sum + b.cost, 0);
  const totalCalls = live.reduce((sum, b) => sum + b.calls, 0);
  const share = (b) =>
    totalCost > 0 ? b.cost / totalCost : totalCalls > 0 ? b.calls / totalCalls : 0;
  const pctOf = (b) => Math.max(1, Math.round(share(b) * 100));

  const sumHtml = live
    .map(
      (b) => `<div class="ub-block${b.key === "chat" ? " hot" : ""}">
        <span class="ub-name">${esc(b.title)}</span>
        <b>¥${fmtMoney(b.cost)}</b>
        <small>${b.calls} 次 · ${esc(fmtCount(b.tokens))} token</small>
        <small class="ub-share">占今日 ${pctOf(b)}%</small>
      </div>`,
    )
    .join("");

  const barHtml = live
    .map(
      (b) =>
        `<i class="${b.key}" style="width:${pctOf(b)}%" title="${esc(
          `${b.title} 占 ${pctOf(b)}%`,
        )}"></i>`,
    )
    .join("");

  const listHtml = live
    .map((b) => {
      const rows = b.rows
        .map((row) => {
          const [label, hint] = USAGE_SOURCE_META[row.key] || [row.key, ""];
          const avg = row.calls > 0 ? row.cost / row.calls : 0;
          // 只有一笔时不写「平均」——一次的价格就是它本身
          const detail =
            row.calls > 1
              ? `平均 ¥${fmtMoney(avg)}/次 · ${fmtCount(row.tokens)} token`
              : `${fmtCount(row.tokens)} token`;
          return `<div class="ub-row" title="${esc(hint)}">
            <span class="ub-row-name">${label}</span>
            <b>${row.calls} 次</b>
            <span class="ub-row-cost">¥${fmtMoney(row.cost)}</span>
            <small>${esc(detail)}</small>
          </div>`;
        })
        .join("");
      const shareText = `${pctOf(b)}%`;
      return `<div class="ub-group">
        <p class="ub-group-head">${esc(b.title)}<small>${esc(b.hint)}</small>
          <em>占今日 ${shareText}</em>
        </p>
        ${rows}
      </div>`;
    })
    .join("");

  box.innerHTML = `
    <div class="ub-sum">${sumHtml}</div>
    <div class="ub-bar">${barHtml}</div>
    <div class="ub-list">${listHtml}</div>`;
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
// 参数页的顺序不用定义顺序决定，看 order 字段——
// 想调整分组位置只改数字，不用搬几十行代码。
// 排法：先「这是谁、怎么说话」，再「怎么学、记什么」，最后是外部能力和运维。
const SETTING_GROUPS = [
  {
    id: "global",
    order: 10,
    title: "👑 全局",
    sub: "主人 QQ 与学习库总开关。改一次，关系 / 事实 / 学习 / 清理一起生效",
    items: [
      {
        key: "owner_ids",
        label: "主人 QQ",
        type: "text",
        default: "",
        hint:
          "填 QQ 号，多个用逗号分隔（中英文逗号、空格都行）。\n" +
          "一处生效、四个模块共用：关系（群聊不封顶 + 记忆配额最高）、" +
          "事实（存储上限 300 条 + 一键清理跳过）、学习（私聊默认开启）、" +
          "清理（可单独清理主人记忆）。\n" +
          "以前这一项藏在「关系状态」分组里，但它不只影响关系。影响：token 0。",
      },
      {
        key: "learn_group_enabled",
        label: "群聊学习库",
        type: "bool",
        default: true,
        hint:
          "所有群聊自动学「这个群怎么说话」，默认开。\n" +
          "关掉后群聊只记消息、不学风格——学习请求归零，省 token。影响：token ↓。",
      },
      {
        key: "learn_private_enabled",
        label: "私聊学习库",
        type: "bool",
        default: false,
        hint:
          "私聊默认不学：私聊是私域，默认把一对一聊天送去提炼不合适。\n" +
          "**主人自己的私聊自动开启**，不受这个开关影响。\n" +
          "想学所有人的私聊就打开它；只想学某个人，去「学习库 → 私聊」单独开。影响：token ↑（打开后）。",
      },
    ],
  },
  {
    id: "base",
    order: 50,
    profileGroup: "learn",
    title: "⚙️ 运行与学习",
    sub: "原有参数，改动同样即时生效",
    items: [
      { key: "enabled", label: "启用群聊记忆", type: "bool", default: true, hint: "总开关。关掉后不收集、不学习、不注入，完全静默。影响：token 0。" },
      { key: "inject_enabled", label: "启用风格注入", type: "bool", default: true, hint: "关闭后不再注入风格，字数上限也一起失效（两者在同一个钩子里）。学习照常、仍有消耗。影响：token ↓↓（关掉后每条回复少几十到一百多字，这是最大的单项开销）。" },
      { key: "analysis_interval_seconds", label: "学习间隔（秒）", type: "int", min: 60, max: 604800, step: 60, default: 21600, hint: "默认 21600（6 小时）≈ 一天最多 4 次，这是「几乎零消耗」的核心。影响：token ↑↑（调小等于按倍数变贵）。" },
      { key: "min_history_for_analysis", label: "触发学习的最少消息数", type: "int", min: 1, max: 1000, step: 1, default: 30, hint: "攒够这么多条新消息才送去提炼，不够直接跳过不发请求。默认 30：10 条太容易学到「嗯」「哈」这类噪音。影响：token ↓（调大）。" },
      { key: "max_messages_per_learn", label: "单次提炼最多消息数", type: "int", min: 1, max: 500, step: 10, default: 80, hint: "单次最多带多少条消息进 prompt。影响：token ↑↑（学一次的钱几乎全在这里）。" },
      { key: "max_stable_inject", label: "最多注入学习风格", type: "int", min: 0, max: 20, step: 1, default: 5, hint: "每轮按熟练度取前 N 条学习风格注入。影响：token ↑↑（每加一条就多一段文本，且每轮都带）。" },
      { key: "max_manual_inject", label: "最多注入手动记忆", type: "int", min: 0, max: 20, step: 1, default: 3, hint: "手动记忆单独占名额，和学习名额各算各的。设 0 = 只存不注入。影响：token ↑（调大，每轮多带几条）。" },
      { key: "max_stable_traits", label: "学习风格存储上限", type: "int", min: 0, max: 200, step: 5, default: 40, hint: "超出按熟练度淘汰。特征越多，每次学习的 prompt 越长。影响：token ↑（调大）。" },
      { key: "enable_situational_inject", label: "启用场景化表达注入", type: "bool", default: true, hint: "只在语境匹配时才注入，成本影响很小；关掉主要损失接梗能力。影响：token ↑（开启时，幅度小——多数轮次匹配不到不注入）。" },
      { key: "max_situational_inject", label: "最多注入场景化表达", type: "int", min: 0, max: 20, step: 1, default: 2, hint: "每轮最多带几条语境匹配的表达。影响：token ↑（调大）。" },
      { key: "max_situational_traits", label: "场景化表达存储上限", type: "int", min: 0, max: 100, step: 5, default: 20, hint: "同上，针对存储。设 0 = 不再保留这类特征。影响：token 0（只影响存储，不影响每轮注入）。" },
      { key: "enable_trigger_matching", label: "启用触发词匹配", type: "bool", default: true, hint: "先做纯本地字符串匹配，命中就不发向量请求——省钱的关键开关。影响：token ↓（保持开启）。" },
      { key: "situational_similarity_threshold", label: "场景化相似度阈值", type: "float", min: 0, max: 1, step: 0.05, default: 0.4, hint: "向量匹配阈值，越低越容易注入。影响：token ↑（调低后）。" },
      { key: "embedding_timeout_seconds", label: "嵌入请求超时（秒）", type: "float", min: 0.5, max: 30, step: 0.5, default: 2, hint: "向量请求跑在回复链路上，超时降级为关键词匹配。影响：token 0（只影响延迟）。" },
      { key: "enable_cross_group", label: "启用跨群风格参考", type: "bool", default: false, hint: "把别群的稳定风格也注入进来。影响：token ↑↑，收益有限，默认关。" },
      { key: "max_global_styles", label: "最多注入全局风格", type: "int", min: 0, max: 20, step: 1, default: 3, hint: "跨群开启时最多注入几条别群风格。影响：token ↑（调大）。" },
      { key: "debug_mode", label: "调试日志", type: "bool", default: false, hint: "日志里输出完整注入内容和学习结果。只增加日志量，排查完关掉。影响：token 0。" },
    ],
  },
  {
    id: "style",
    order: 30,
    profileGroup: "style",
    title: "🎭 风格与人格",
    sub: "调低＝省钱＋更像人设，调高＝更像群里的人",
    items: [
      {
        key: "style_strength", label: "被动回复风格强度", type: "slider", min: 0, max: 100, step: 5, default: 100,
        hint: "0 = 完全不注入（纯人格）；1-30 只留 1 条且措辞很弱；31-69 名额减半；70-99 名额四分之三；100 满额，且必定让位（100 时不再掷骰，每轮都按风格走）。强度和注入名额一起降。影响：token ↓↓（调低，每轮少带几十到一百多字）。",
      },
      {
        key: "active_style_strength", label: "主动插话风格强度", type: "slider", min: 0, max: 100, step: 5, default: 60,
        hint: "机器人自己插话时用这一档。默认 60 故意低于阈值 70，这样插话永远不推翻人格。影响：token ↓（通常比被动低）。",
      },
      {
        key: "style_override_threshold", label: "风格压过人格的起始强度", type: "slider", min: 0, max: 100, step: 5, default: 100,
        hint: "强度【严格大于】这个值才让位。默认 100 + 强度 100 → 开箱不让位，和 1.2.0 行为一致。想让群风格压过人设就调低它，例如 70 配强度 100 = 每轮都按风格走。只是指令层优先级，不是真删人格。影响：token 0。",
      },
      {
        key: "style_override_deep", label: "深度覆盖人格（会击穿缓存）", type: "bool", warn: true, default: false,
        hint: "会让该轮缓存失效：每次命中都改 system prompt，DeepSeek 前缀缓存必然失效，输入价从 ¥0.05 跳到 ¥1.5/百万（30 倍）。默认关闭。影响：token ↑↑（打开后每轮都可能击穿缓存）。",
      },
    ],
  },
  {
    id: "length",
    order: 40,
    title: "📏 回复长度",
    sub: "「上限」是倾向不是硬限制",
    items: [
      { key: "reply_max_chars", label: "被动回复字数上限", type: "int", min: 0, max: 2000, step: 10, default: 0, hint: "0 = 不限制。设 80 实际常见 90-120 字，别当硬限制用。影响：token ↓（设置后回复变短）。" },
      { key: "active_reply_max_chars", label: "主动插话字数上限", type: "int", min: 0, max: 2000, step: 10, default: 80, hint: "插话本来就该路过接一句，三行字容易变成抢话。同样只是倾向。影响：token ↓。" },
      {
        key: "reply_max_chars_hard", label: "超限时硬截断", type: "bool", warn: true, default: false,
        hint: "会切坏 markdown：按句末标点硬切，表格、链接、代码块和表情占位符都可能被切残。不建议开。影响：token 0。",
      },
    ],
  },
  {
    id: "facts",
    order: 60,
    profileGroup: "facts",
    title: "🧠 事实记忆与个人画像",
    sub: "上面三个是遗忘节奏（多久没提就忘），下面是画像（这个人是谁）",
    items: [
      {
        key: "fact_shallow_days", label: "浅层遗忘（天）", type: "int", min: 1, max: 90, step: 1, default: 15,
        hint: "只提过一次的事，这么多天没再提到就归档（不再注入，面板里还查得到）。默认 15 天：7 天剩 78%、10 天剩 55%、15 天归零。影响：token 0（只改记忆寿命）。",
      },
      {
        key: "fact_deep_days", label: "深层降档（天）", type: "int", min: 1, max: 180, step: 1, default: 30,
        hint: "深层记忆这么多天没再提到，就掉回浅层（下一轮再按浅层的天数归档）。默认 30 天。要比浅层大，否则深层没意义。影响：token 0。",
      },
      {
        key: "fact_core_days", label: "核心降档（天）", type: "int", min: 1, max: 365, step: 1, default: 60,
        hint: "核心记忆这么多天没再提到，就掉成深层。默认 60 天。核心只是忘得慢，不是永不遗忘。影响：token 0。",
      },
      {
        key: "profile_enabled", label: "启用个人画像", type: "bool", default: true,
        hint: "从事实里提炼一段「这个人是谁」，按好感给字数（陌生 120 → 老友 420，**主人 800**），每轮注入时单独一段。\n不待见档不生成也不注入；关掉等于回到「只有事实、没有画像」。影响：token ↑（每轮多几十到四百多字）。",
      },
      {
        key: "profile_min_facts", label: "画像最少几条事实", type: "int", min: 1, max: 50, step: 1, default: 3,
        hint: "攒够这么多条才写第一版。太少写出来只是复述，没有提炼价值。默认 3 条。影响：token 0（只决定什么时候写）。",
      },
      {
        key: "profile_rewrite_hours", label: "画像重写间隔（小时）", type: "int", min: 1, max: 720, step: 1, default: 24,
        hint: "两次重写至少隔多久。画像本来就该稳定，天天改写会来回抖。默认 24 小时，而且必须「新增 ≥2 条事实」才会触发。影响：token ↑（调小）。",
      },
      {
        key: "profile_daily_cap", label: "每天最多重写几人", type: "int", min: 0, max: 100, step: 1, default: 6,
        hint: "一次学习可能同时触发好几个人的画像，这个上限把「一天最坏开销」钉死。默认 6 人 ≈ 5000 token/天。0 = 全禁（只用手动重新生成）。影响：token ↑（调大）。",
      },
    ],
  },
  {
    id: "feed",
    order: 999,
    // 单独一页渲染：投喂榜、加成、每日次数是同一个话题，拆在参数里要翻两处
    pane: "feed",
    title: "🍚 喂食",
    sub: "群友用「吃什么」插件点菜时自动加好感；没装那个插件就是空转，不报错",
    items: [
      {
        key: "feed_enabled", label: "启用喂食", type: "bool", default: true,
        hint: "开了之后，「吃什么」出菜时会往消息上挂一条标记，本插件读到就记一次投喂。需要那个插件也更新到带标记的版本，否则两边各跑各的、不会加好感。影响：token 0（不加请求）。",
      },
      {
        key: "feed_gain_random", label: "随机推荐加多少", type: "slider", min: 0, max: 20, step: 0.5, default: 2,
        hint: "群友发「吃什么」、bot 随机报一道菜时加多少。默认 2：一次直接对话是 3.1，随手问一句不该比认真聊天更值钱。影响：token 0。",
      },
      {
        key: "feed_gain_order", label: "点菜加多少", type: "slider", min: 0, max: 30, step: 0.5, default: 5,
        hint: "群友明确点菜（「点菜KFC」「我要吃红烧肉」）时加多少。默认 5：有明确意图，比随机推荐重，但仍低于聊三轮。影响：token 0。",
      },
      {
        key: "feed_daily_limit", label: "每人每天计几次", type: "int", min: 0, max: 50, step: 1, default: 2,
        hint: "同一个人一天最多算几次有效投喂，超出不涨分也不报错。默认 2：随机推荐一次 + 点菜一次，两种都能加分；再往上会盖过认真聊天的权重。0 = 不限次数（不建议）。影响：token 0。",
      },
    ],
  },
  {
    id: "active",
    order: 20,
    // 单独一页：预设、按群开关、13 个参数挤在参数页里太长了
    pane: "active",
    profileGroup: "active",
    title: "🗣 主动回复",
    sub: "唯一会凭空产生请求的功能。顶部五档一键切换，改完还能单独微调",
    items: [
      { key: "active_reply_enabled", label: "启用主动回复", type: "bool", default: false, hint: "开启后机器人自己找时机接话。影响：token ↑（一天最多日限那么多次）。" },
      {
        key: "active_reply_chance", label: "每个窗口的参与概率", type: "slider", min: 0, max: 1, step: 0.05, default: 0.15,
        hint: "仅 probability 模式。0.15 ≈ 一天约 7 次，正好落在日限 8 以内。骰子按窗口掷，不按消息——否则群越热闹越容易被插话。影响：token ↑↑（每次插话都是一次完整请求）。",
      },
      { key: "active_reply_cooldown_seconds", label: "冷却时间（秒）", type: "int", min: 60, max: 86400, step: 60, default: 1800, hint: "两次评估至少隔多久，默认 1800 ≈ 一天 48 个窗口。影响：token ↓（调大）。" },
      { key: "active_reply_daily_limit", label: "每日上限（每群）", type: "int", min: 0, max: 100, step: 1, default: 8, hint: "每群每天最多主动说几次，自然日重置，**这是最硬的一道闸门**——概率写得再高也超不过它。每次约 1500 token：8 次 ≈ 1.2 万、40 次 ≈ 6 万、70 次 ≈ 10 万 token/天。30 以上要留意平台风控和被嫌吵。影响：token ↑↑（每多一次就是一次完整请求）。" },
      { key: "active_reply_per_user_limit", label: "单人每日上限", type: "int", min: 0, max: 100, step: 1, default: 6, hint: "同一个人一天最多被主动搭话几次。群日限管「这个群今天还能说几次」，这个管「别把额度全花在一个人身上」。默认 6 次；0 = 不限。影响：token ↓（跳过的那次不发请求）。" },
      { key: "active_reply_quiet_hours", label: "静默时段", type: "text", default: "01:00-08:00", hint: "格式 01:00-08:00，支持跨天（如 22:00-06:00）。这段时间零消耗。留空 = 不静默。影响：token ↓（静默时段完全不发请求）。" },
      { key: "active_reply_min_messages", label: "冷清阈值", type: "int", min: 0, max: 100, step: 1, default: 3, hint: "最近 10 分钟少于这么多条就不插话，避免对着空气说话。0 = 关闭检测。影响：token ↓。" },
      {
        key: "active_reply_mode", label: "触发方式", type: "select", default: "probability",
        options: [["probability", "概率掷骰（零额外消耗）"], ["rule", "关键词规则（零额外消耗）"], ["llm_judge", "让模型判定（有消耗）"]],
        hint: "probability 每个冷却窗口掷一次骰；rule 命中提问句式/关键词才接；llm_judge 攒够消息问一次模型，判定本身就有消耗。影响：llm_judge 的 token ↑↑。",
      },
      { key: "active_reply_judge_stack", label: "判定攒消息数", type: "int", min: 1, max: 100, step: 1, default: 8, hint: "仅 llm_judge：攒够多少条才问一次模型。默认 8，一天 200 条 ≈ 25 次判定 ≈ 1.5 万 token/天。影响：token ↑（调小）。" },
      { key: "active_reply_backoff_limit", label: "连击退避阈值", type: "int", min: 0, max: 20, step: 1, default: 3, hint: "连续这么多次插话没人接就把冷却放大，防止自嗨（设计上限 4 倍）。0 = 关闭退避。一旦有人说话立刻清零。影响：token ↓。" },
      { key: "active_reply_keywords", label: "额外触发词", type: "list", default: [], hint: "仅 rule 模式：逗号分隔，命中就接话。留空只用内置提问句式。影响：token ↑（命中越多越贵）。" },
      {
        key: "active_reply_guidance", label: "语气引导", type: "textarea", default:
          "这是一条由你主动发起的群聊回复，没有人向你提问。\n不要使用「有什么可以帮你」「我在」「已收到」这类服务式或召唤式开头；\n不要提及自己是主动发言或任何机制；\n像普通群友一样简短接话；话题不适合接就少说或不说。",
        hint: "告诉模型这是主动发言、别用客服腔。留空则完全不注入这段。影响：token ↑（多几十字）。",
      },
      { key: "active_reply_provider_id", label: "使用的 Provider ID", type: "text", default: "", hint: "判定和插话共用这个模型，留空用当前会话默认。换成便宜的 flash 可显著降低单次成本。影响：token ↓（换便宜模型）。" },
    ],
  },
  {
    id: "search",
    order: 70,
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
        hint: "deepseek 最划算：¥0.005/次、复用模型 key、失败不计费。tavily/bocha 有月额度，20 次/群/天 × 10 群一个月就会烧穿。影响：token 0（搜索按次单独计费，不占对话 token）。",
      },
      { key: "web_search_api_key", label: "API Key", type: "password", default: "", hint: "留空 + 下面复用全局 key 开着时会自动读 AstrBot 里同家的 key，多数情况不用填。填了会明文写进 runtime.json。影响：token 0。" },
      { key: "web_search_reuse_global_key", label: "复用 AstrBot 全局 key", type: "bool", default: true, hint: "key 只存一处，插件目录不留明文。DeepSeek 的搜索 key 和模型 key 是同一个。影响：token 0。" },
      { key: "web_search_daily_limit", label: "每日上限（每群）", type: "int", min: 0, max: 200, step: 1, default: 20, hint: "每群每天最多搜几次，跨群独立，第二天归零。20 次 ≈ ¥0.15/群/天。正常群一天真正需要实时信息不超过 5 次。影响：token ↑（调大）。" },
      { key: "web_search_max_per_turn", label: "单轮上限", type: "int", min: 0, max: 10, step: 1, default: 2, hint: "一轮最多搜几次，防止模型连环搜把当天配额烧完。0 = 不限（不建议）。影响：token ↑（调大）。" },
      { key: "web_search_max_results", label: "返回条数", type: "int", min: 1, max: 50, step: 1, default: 5, hint: "一次搜几条。5 条约 1200 token，调到 10 大约翻倍。影响：token ↑（调大）。" },
      {
        key: "web_search_need_content", label: "返回网页正文", type: "bool", warn: true, default: false,
        hint: "成本翻十倍：每条结果都带整页正文，5 条可能 3-5 万 token，一次搜索顶一天的学习消耗。默认的 summary 已有 600-800 字概要，够用。影响：token ↑↑↑。",
      },
      { key: "web_search_timeout", label: "超时（秒）", type: "int", min: 1, max: 60, step: 1, default: 8, hint: "超过就放弃搜索按失败处理（不阻塞、不计费）。调大会让群里等回复更久。影响：token 0。" },
      { key: "web_search_hint", label: "搜索提示语", type: "textarea", default: "", hint: "留空完全不注入。填了会提示模型「需要实时信息先搜一下」，但可能搜得太勤把配额提前用完。影响：token ↑（且显著增加搜索次数）。" },
    ],
  },
  {
    id: "admin",
    order: 80,
    title: "🛡 管理员",
    sub: "让管理员在群里直接改参数",
    items: [
      { key: "admin_extra_users", label: "额外管理员 QQ", type: "list", default: [], hint: "除 AstrBot 全局管理员外还能管理这个插件的人，逗号分隔。他们能在群里调参数、加记忆、查消耗。影响：token 0。" },
      { key: "admin_tool_enabled", label: "允许对话调参", type: "bool", default: false, hint: "开启后管理员在群里说话就能改设置、加记忆。非管理员群里这个工具根本不挂给模型。影响：token ↑（约 200 token 的工具定义）。" },
    ],
  },
  {
    id: "price",
    order: 90,
    title: "💰 计价",
    sub: "只影响面板估的金额，不影响真实扣费",
    items: [
      { key: "token_price_input_cached", label: "缓存命中输入价（元/百万）", type: "float", min: 0, max: 1000, step: 0.01, default: 0.05, hint: "DeepSeek 官方牌价，调价了就改这里，不用等插件更新。影响：token 0（只影响金额换算）。" },
      { key: "token_price_input_miss", label: "未命中输入价（元/百万）", type: "float", min: 0, max: 1000, step: 0.01, default: 1.5, hint: "缓存没命中时的价格，是命中价的 30 倍——这就是插件坚持不改 system prompt 的原因。影响：token 0（只影响金额换算）。" },
      { key: "token_price_output", label: "输出价（元/百万）", type: "float", min: 0, max: 1000, step: 0.01, default: 4.5, hint: "每百万输出 token 的价格。影响：token 0（只影响金额换算）。" },
      { key: "token_price_peak_multiplier", label: "高峰倍率", type: "float", min: 1, max: 10, step: 0.1, default: 2, hint: "高峰时段（工作日 09:00-12:00、14:00-18:00 北京时间）按这个倍率算。DeepSeek 目前 2 倍。影响：token 0（只影响金额换算）。" },
      { key: "token_price_peak_enabled", label: "区分峰谷价", type: "bool", default: true, hint: "关掉就全按空闲价算。节假日不单独处理，会略微高估。影响：token 0（只影响金额换算）。" },
      { key: "token_stats_keep_days", label: "消耗记录保留天数", type: "int", min: 1, max: 60, step: 1, default: 7, hint: "按天存文件，超出的自动删。只存数字不存聊天内容，泄露也不含隐私。影响：token 0（只占磁盘）。" },
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

// 参数页搜索关键词 + 最后一次拿到的配置（改筛选时不用重新请求）
let settingQuery = "";
let lastSettingsData = null;

// 一条预设是否与当前配置完全吻合。数值留 1e-6 容差——
// 概率是浮点，0.15 存进去再读出来可能带尾数
function profileMatches(profile, settings) {
  const values = (profile && profile.values) || {};
  return Object.entries(values).every(([key, want]) => {
    const cur = settings[key];
    if (typeof want === "number") return Math.abs(Number(cur) - want) < 1e-6;
    return String(cur ?? "") === String(want);
  });
}

// 一组预设 =「几个互相咬合的参数一起改」。点一下整组写进去，之后还能单独
// 微调；微调到不匹配任何预设时，右边那句提示会说「已单独改过」。
//
// 标签跟着组走：主动回复叫「强度预设」，别处叫档位——在「事实记忆」里看到
// 「强度」两个字，用户不知道在说强度的什么。
const PROFILE_LABELS = {
  active: "强度预设",
  learn: "学习强度",
  style: "风格档位",
  facts: "记忆档位",
};

function profileBar(groupKey, settings) {
  const all = (lastStatus && lastStatus.profiles) || {};
  // 老浏览器缓存了旧 app.js 时只有 active_profiles，这里兜一下
  const profiles =
    all[groupKey] ||
    (groupKey === "active" && lastStatus && lastStatus.active_profiles) ||
    [];
  if (!profiles.length) return "";
  const current = profiles.find((p) => profileMatches(p, settings));
  const buttons = profiles
    .map(
      (p) =>
        `<button type="button" data-profile="${escAttr(p.id)}" data-profile-group="${escAttr(
          groupKey,
        )}"${current && current.id === p.id ? ' class="on"' : ""} title="${escAttr(
          p.desc,
        )}">${esc(p.label)}</button>`,
    )
    .join("");
  const note = current
    ? esc(current.desc)
    : "这里被单独微调过，已经不属于任何一个档位";
  const label = PROFILE_LABELS[groupKey] || "预设";
  return `<div class="profile-bar">
    <span class="profile-label" title="点一下会同时改这组里互相咬合的几个参数。改完还能继续单独调，调过之后就不再属于任何档位">${esc(
      label,
    )}</span>
    <div class="seg">${buttons}</div>
    <span class="hint">${note}</span>
  </div>`;
}

// ── 主动回复页 ───────────────────────────────────────────────────────
//
// 从参数页搬出来单独成页：十几个参数 + 预设 + 按群开关挤在同一组里，
// 想找「哪个群在说话」得滚半天。
//
// 筛选/排序复用学习库那套 gf 组件，和事实、关系两页同一套手感。

let activeGroups = [];
let activeGlobalOn = false;
let activePerUserLimit = 0;

const activeFilter = { text: {}, sets: {}, sort: "today", dir: "desc" };

const ACTIVE_FLAGS = {
  full: "今日已满",
  spoke: "今天说过话",
  quiet: "今天还没说话",
  streak: "连续没被接",
};

function matchActiveFlag(item, flag) {
  if (flag === "full") return !!item.full;
  if (flag === "spoke") return (item.today || 0) > 0;
  if (flag === "quiet") return (item.today || 0) === 0;
  if (flag === "streak") return (item.streak || 0) >= 3;
  return false;
}

const ACTIVE_GF = {
  bar: "activeFilterBar",
  pop: "activeFilterPop",
  search: "activeSearch",
  state: activeFilter,
  items: () => activeGroups,
  render: renderActiveList,
  reset: () => {
    activeFilter.text = {};
    activeFilter.sets = {};
    activeFilter.sort = "today";
    activeFilter.dir = "desc";
  },
  columns: [
    {
      col: "who",
      label: "群",
      type: "text",
      title: "按群筛选",
      placeholder: "群号 / 群名",
      groups: [
        {
          key: "flags",
          title: "附加条件",
          options: Object.keys(ACTIVE_FLAGS).map((key) => ({ key, label: ACTIVE_FLAGS[key] })),
          count: (items, key) => items.filter((i) => matchActiveFlag(i, key)).length,
        },
      ],
    },
    {
      col: "state",
      label: "状态",
      groups: [
        {
          key: "states",
          title: "按开关状态",
          options: [
            { key: "on", label: "开着" },
            { key: "off", label: "关掉" },
          ],
          count: (items, key) =>
            items.filter((i) => (key === "on" ? !!i.enabled : !i.enabled)).length,
        },
      ],
    },
    {
      col: "today",
      label: "今日",
      type: "sort",
      descLabel: "降序（说得多的在前）",
      ascLabel: "升序（说得少的在前）",
    },
    {
      col: "idle",
      label: "最近",
      type: "sort",
      descLabel: "降序（刚说过的在前）",
      ascLabel: "升序（最久没说的在前）",
    },
  ],
};

function matchActive(item) {
  const st = gfState(ACTIVE_GF);
  const kw = String(st.text.who || "").trim().toLowerCase();
  if (kw && !`${item.session_id} ${item.name}`.toLowerCase().includes(kw)) return false;
  const flags = st.sets.flags || [];
  if (flags.length && !flags.some((f) => matchActiveFlag(item, f))) return false;
  const states = st.sets.states || [];
  if (states.length && states.includes("on") !== !!item.enabled) return false;
  return true;
}

function sortActive(items) {
  const st = gfState(ACTIVE_GF);
  const mul = st.dir === "asc" ? 1 : -1;
  const key = st.sort || "today";
  const pick = key === "idle" ? (i) => i.last_at || 0 : (i) => i.today || 0;
  return items.sort((a, b) => {
    const diff = (pick(a) - pick(b)) * mul;
    // 排不出先后时让开着的排前面，否则同一档的群每次刷新顺序都在跳
    return diff !== 0 ? diff : (b.enabled ? 1 : 0) - (a.enabled ? 1 : 0);
  });
}

function activeRow(g) {
  const stage = !g.enabled ? "off" : g.full ? "full" : "on";
  const label = { on: "开着", off: "关掉", full: "今日已满" }[stage];
  const today = g.limit > 0 ? `今日 ${g.today}/${g.limit}` : `今日 ${g.today} 次`;
  const roll = g.next_roll_in ? `下次评估 ${fmtDuration(g.next_roll_in)}后` : "待评估";
  const last = g.last_at ? `上次插话 ${fmtTime(g.last_at)}` : "还没插过话";
  // 中途把上限调小时，已经用掉的次数不会跟着清零——今天就是会超过上限。
  // 不解释一句，用户会以为计数坏了
  const tip =
    g.full && g.today > g.limit
      ? "今天已经用超了：把上限调小不会清掉已经用掉的次数，明天零点自动重置"
      : g.enabled
        ? "这个群会自己找时机接话。取消勾选就安静下来，其它群不受影响"
        : "这个群不会主动插话，只在被 @ 或回复时说话。勾上恢复";
  return `<div class="arow${g.enabled ? "" : " off"}" title="${escAttr(tip)}">
    <input type="checkbox" data-active-group="${escAttr(g.session_id)}" ${
      g.enabled ? "checked" : ""
    } />
    <b class="arow-name">${esc(g.name)}</b>
    <span class="arow-meta">${esc(today)} · ${esc(roll)} · ${esc(last)}</span>
    <i class="arow-tag ${stage}">${esc(label)}</i>
  </div>`;
}

function renderActiveList() {
  const box = $("activeList");
  if (!box) return;
  const all = activeGroups || [];
  const visible = sortActive(all.filter(matchActive).slice());
  const note = $("activeListNote");
  if (note) {
    note.textContent =
      visible.length === all.length ? `共 ${all.length} 个群` : `${visible.length} / ${all.length} 个群`;
  }
  const cond = $("activeFilterNote");
  if (cond) cond.textContent = gfNoteText(ACTIVE_GF);
  gfPaint(ACTIVE_GF);

  if (!all.length) {
    box.innerHTML =
      `<p class="empty">还没有群聊会话。bot 在群里收到过消息之后，这里才会出现对应的群。</p>`;
    return;
  }
  if (!visible.length) {
    box.innerHTML = `<p class="empty">没有符合条件的群</p>`;
    return;
  }
  box.innerHTML = visible.map(activeRow).join("");
}

function renderActiveStats() {
  const items = activeGroups || [];
  const on = items.filter((g) => g.enabled).length;
  const total = items.reduce((sum, g) => sum + (g.today || 0), 0);
  const spoke = items.filter((g) => (g.today || 0) > 0).length;
  const limit = items.length ? items[0].limit : 0;
  $("activeStats").innerHTML = [
    ["总开关", activeGlobalOn ? "开着" : "关着", "总开关关掉后，下面每个群的开关都不再起作用"],
    ["开着的群", `${on} / ${items.length}`, "允许自己找时机接话的群数"],
    ["今日主动发言", `${total} 次`, "所有群加起来，今天 bot 自己插了几次话"],
    ["今天说过话的群", `${spoke} 个`, "今天至少插过一次话的群数"],
    ["每群每日上限", limit > 0 ? `${limit} 次` : "不限", "单个群每天最多主动说几次，自然日重置。这是最硬的一道闸门"],
    [
      "每人每日上限",
      activePerUserLimit > 0 ? `${activePerUserLimit} 次` : "不限",
      "同一个人一天最多被主动搭话几次，防止把额度全花在一个人身上",
    ],
  ]
    .map(
      ([k, v, tip]) =>
        `<div class="stat"${tip ? ` title="${escAttr(tip)}"` : ""}><span>${esc(k)}</span><b>${esc(
          v,
        )}</b></div>`,
    )
    .join("");

  const notes = [];
  if (!activeGlobalOn) notes.push("总开关现在是关着的，下面群开了也不会插话——往上翻参数区打开它。");
  if (limit > 0 && total >= limit * Math.max(1, on)) {
    notes.push(`今天所有群都接近或达到上限（每群 ${limit} 次），想多说只能改「每群每日上限」。`);
  }
  notes.push("这里只管「自己找时机接话」。被 @ 或被回复时的正常回复不受任何开关影响。");
  $("activeNote").textContent = notes.join(" ");
}

async function loadActive() {
  try {
    const data = unwrap(await Bridge.apiGet("active", {}));
    activeGroups = data.items || [];
    activeGlobalOn = !!data.enabled;
    activePerUserLimit = Number(data.per_user_limit || 0);
  } catch (err) {
    console.error("加载主动回复数据失败:", err);
    activeGroups = [];
    showToast("主动回复数据加载失败，请查看日志");
    return;
  }
  renderActiveStats();
  renderActiveList();
}

// 关掉某个群的主动回复。只改本地那一行，不整页重渲染：
// 重渲染会把滚动条弹回顶部，用户刚点的那个群反而找不到了
async function toggleActiveGroup(sid, enabled) {
  try {
    const payload = unwrap(
      await Bridge.apiPost("manage", { action: "set_active", session_id: sid, enabled }),
    );
    if (payload && payload.success === false) {
      showToast(payload.message || "切换失败");
      return;
    }
    const row = activeGroups.find((g) => g.session_id === sid);
    if (row) {
      row.enabled = enabled;
      row.full = enabled ? row.full : false;
    }
    showToast(enabled ? "已开启这个群的主动回复" : "已关掉这个群的主动回复");
    renderActiveStats();
    renderActiveList();
  } catch (err) {
    console.error("切换单群主动回复失败:", err);
    showToast("切换失败，请查看日志");
  }
}

function renderSettings(settings) {
  lastSettingsData = settings;
  const overrides = settings._runtime_overrides || {};
  const q = settingQuery.trim().toLowerCase();
  let hitCount = 0;

  // 带 pane 的组属于别的页（比如喂食），不在这里出现——一页一个话题。
  // filter 已经返回新数组，可以直接排序，不会打乱原定义
  const blocks = SETTING_GROUPS.filter((group) => !group.pane)
    .sort((a, b) => (a.order ?? 99) - (b.order ?? 99))
    .map((group) => {
    // 搜索时按「参数名 / key / 说明」过滤，一整组都没命中就整组不渲染——
    // 67 项参数一屏放不下，用户要的是「直接定位到那一条」
    const defs = q
      ? group.items.filter((def) =>
          `${def.label || ""} ${def.key} ${def.hint || ""}`.toLowerCase().includes(q),
        )
      : group.items;
    if (!defs.length) return "";
    hitCount += defs.length;
    const rows = defs.map((def) => settingRow(def, settings, overrides)).join("");
    const sub = group.sub ? `<small>${esc(group.sub)}</small>` : "";
    // 搜索组的联动提示挂在标题旁，选模式时立刻能看到
    const note = group.id === "search" ? '<span class="group-note" id="searchNote"></span>' : "";
    const footer = group.id === "style" ? '<p class="hint compute" id="styleNote"></p>' : "";
    // 主动回复组顶部是「三档一键切换」：这几个参数互相咬合，
    // 单独改一个看不出效果，所以给整体档位
    const head = group.profileGroup ? profileBar(group.profileGroup, settings) : "";
    return `<section class="set-group">
      <h3>${esc(group.title)}${sub}${note}</h3>
      ${head}
      <div class="set-grid">${rows}</div>
      ${footer}
    </section>`;
  });

  const note = $("settingFilterNote");
  if (note) {
    note.textContent = q ? `匹配 ${hitCount} 项` : "";
  }
  $("settings").innerHTML =
    blocks.join("") ||
    `<p class="empty">没有匹配「${esc(settingQuery.trim())}」的参数</p>`;
  clearSettingMsg();
  updateStyleNote();
  applySearchMode(settings.web_search_mode || "plugin");
}

// ── 喂食页 ───────────────────────────────────────────────────────────
//
// 这一页原来挤在参数页里，滚到一半才能看见；投喂榜又要跟加成对着看，
// 所以整块搬出来：上面是今天喂了多少，中间是参数，下面是排行榜。

let feedBoard = { items: [], stats: {} };
const feedQuery = { text: "" };

// 参数区复用参数页那套渲染，输入框的 data-key 一样，保存走同一个接口。
// 主动回复和喂食各自成页，但参数行的长相和交互不该有两套
function renderPaneSettings(paneId, containerId, settings) {
  const box = $(containerId);
  if (!box) return;
  const group = SETTING_GROUPS.find((g) => g.pane === paneId);
  if (!group) return;
  const overrides = settings._runtime_overrides || {};
  const head = group.profileGroup ? profileBar(group.profileGroup, settings) : "";
  box.innerHTML =
    head +
    `<div class="set-grid">${group.items
      .map((def) => settingRow(def, settings, overrides))
      .join("")}</div>`;
}

function renderFeedSettings(settings) {
  renderPaneSettings("feed", "feedSettings", settings);
}

async function loadFeed() {
  try {
    const data = unwrap(await Bridge.apiGet("feed", {}));
    feedBoard = data || { items: [], stats: {} };
  } catch (err) {
    console.error("加载投喂数据失败:", err);
    showToast("投喂数据加载失败，请查看日志");
    return;
  }
  renderFeedBoard();
}

function renderFeedBoard() {
  const stats = feedBoard.stats || {};
  const items = feedBoard.items || [];
  const limit = Number(feedBoard.limit || 0);
  const gainRandom = Number(feedBoard.gain_random || 0);
  const gainOrder = Number(feedBoard.gain_order || 0);

  const enabled = !!feedBoard.enabled;
  $("feedStats").innerHTML = [
    ["今日投喂", `${stats.today ?? 0} 次`, "今天被记成有效投喂的次数，超出每人上限的不算"],
    ["今日人数", `${stats.today_people ?? 0} 人`, "今天喂过的人有多少"],
    ["累计投喂", `${stats.total ?? 0} 次`, "从装上到现在一共记了多少次"],
    ["喂过的人", `${stats.people ?? 0} 人`, "至少喂过一次的人数"],
    ["每人每天上限", limit > 0 ? `${limit} 次` : "不限", "同一个人一天最多算几次，超出不涨分也不报错"],
    ["最近一次", stats.last_at ? fmtTime(stats.last_at) : "—", "最近一次投喂的时间"],
  ]
    .map(
      ([k, v, tip]) =>
        `<div class="stat"${tip ? ` title="${escAttr(tip)}"` : ""}><span>${esc(k)}</span><b>${esc(
          v,
        )}</b></div>`,
    )
    .join("");

  const notes = [];
  notes.push(
    gainRandom > 0 || gainOrder > 0
      ? `当前一次随机推荐 +${gainRandom} 点、一次点菜 +${gainOrder} 点好感。`
      : "两个加成都是 0，现在投喂只记次数、不加好感。",
  );
  if (!enabled) notes.push("喂食总开关关着，投喂不会被记录。");
  notes.push(
    "投喂的加分同样占「单日单人上限」的额度，所以它不会变成绕开日限的捷径；" +
      "打满之后就只记次数、不再涨分。",
  );
  $("feedNote").textContent = notes.join(" ");

  const kw = feedQuery.text.trim().toLowerCase();
  const visible = kw
    ? items.filter((it) =>
        `${it.qq} ${it.name} ${it.last_dish}`.toLowerCase().includes(kw),
      )
    : items;
  $("feedListNote").textContent = `${visible.length} 人`;

  if (!items.length) {
    $("feedList").innerHTML =
      `<p class="empty">还没有投喂记录。群友在群里发「吃什么」、或者点一道菜，` +
      `这里就会出现第一条。没装「吃什么」插件的话，这一页会一直是空的。</p>`;
    return;
  }
  if (!visible.length) {
    $("feedList").innerHTML = `<p class="empty">没有匹配「${esc(feedQuery.text.trim())}」的人</p>`;
    return;
  }

  $("feedList").innerHTML = visible
    .map((it, i) => {
      const rank = i === 0 ? "🥇" : i === 1 ? "🥈" : i === 2 ? "🥉" : `${i + 1}.`;
      const dish = it.last_dish
        ? `最近吃了${it.last_dish}${it.last_mode === "order" ? "（点菜）" : "（随机）"}`
        : "还没记到菜名";
      const todayText = limit > 0 ? `今天 ${it.today}/${limit}` : `今天 ${it.today} 次`;
      return `<div class="feed-row">
        <span class="feed-rank">${rank}</span>
        <b class="feed-who">${esc(it.name)}<small>${esc(it.qq)}</small></b>
        <span class="feed-dish">${esc(dish)}</span>
        <span class="feed-count"><b>${it.total}</b> 次<small>${esc(todayText)}</small></span>
      </div>`;
    })
    .join("");
}

// token 影响标签：从 hint 里那句「影响：token ↑」提取出来单独渲染。
//
// 为什么要单独做成标签：hint 是几十字的一段话，躺在里面根本看不见；
// 做成彩色标签挂在标题旁，扫一眼就知道这个选项动不动钱。
// 箭头数量就是影响大小：↑↑ = 每轮都变、↑ = 有影响、0 = 不改 token。
function tokenTag(hint) {
  const m = /token\s*([↑↓]{1,3}|0)/.exec(String(hint || ""));
  if (!m) return "";
  const raw = m[1];
  if (raw === "0") return '<i class="set-tok zero" title="不影响 token 消耗">token 0</i>';
  const up = raw.startsWith("↑");
  const level = raw.length >= 2 ? "big" : "small";
  const arrow = up ? "↑".repeat(raw.length) : "↓".repeat(raw.length);
  return `<i class="set-tok ${up ? "up" : "down"} ${level}" title="对 token 的影响：${
    up ? "调大/开启会变贵" : "调大/开启会变便宜"
  }">${arrow}</i>`;
}

// 「默认：xxx」徽标。bool 显示开/关——直接写 true/false 对非技术用户是噪音。
function fmtDefault(def) {
  const v = def.default;
  if (v === undefined || v === null) return "";
  if (typeof v === "boolean") return v ? "开" : "关";
  if (Array.isArray(v)) return v.length ? v.join(",") : "空";
  if (typeof v === "string") return v.trim() || "空";
  if (def.type === "slider" || def.type === "float") return String(v);
  return String(v);
}

function settingRow(def, settings, overrides) {
  const raw = settings[def.key] === undefined || settings[def.key] === null
    ? def.default
    : settings[def.key];
  const hot = def.key in overrides ? '<i class="hot">已热调</i>' : "";
  const defText = fmtDefault(def);
  const defBadge =
    defText === ""
      ? ""
      : `<i class="set-def" title="配置文件里的默认值。控件里显示的才是当前生效的，改过的会在右边标「已热调」">默认 ${esc(
          defText,
        )}</i>`;
  const hint = def.hint
    ? `<p class="set-hint${def.warn ? " warn" : ""}">${esc(def.hint)}</p>`
    : "";
  const wide = def.type === "textarea" || def.type === "list" ? " wide" : "";
  const warn = def.warn ? " warn-row" : "";
  return `<label class="set-row${warn}${wide}" data-row="${def.key}">
    <span class="set-label">${esc(def.label)} ${defBadge}${tokenTag(def.hint)} ${hot}</span>
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
  if (el) el.textContent = message || "";
  // 喂食页和主动回复页的参数走同一个保存接口，报错也各写一份，
  // 否则在那一页改错了值只会看到一条转瞬即逝的 toast
  ["feedMsg", "activeMsg"].forEach((id) => {
    const box = $(id);
    if (box) box.textContent = message || "";
  });
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
      <button type="button" class="pin-btn${s.pinned ? " on" : ""}" data-act="pin" data-sid="${sid}"
        title="${s.pinned ? "取消置顶（置顶时不能删这个会话）" : "置顶这个会话，排在列表最前"}">${
        s.pinned ? "已置顶" : "置顶"
      }</button>
      ${
        // 置顶的一律不给删除按钮（和学习库同一套规矩）：不是灰掉，是整块不渲染
        s.pinned
          ? ""
          : `<button type="button" class="pin-btn danger" data-act="drop_session" data-sid="${sid}"
              data-name="${escAttr(s.display_name || sid)}"
              title="删除这个会话的全部记忆（学到的东西 + 手动记忆 + 待学习消息）">删除</button>`
      }
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
        <button data-act="add_trait" data-sid="${sid}" title="人工加一条记忆：不看聊天记录，学习也删不掉它">+ 手动记忆</button>
        <button data-act="learn" data-sid="${sid}" title="立刻对这个会话跑一次学习（会发一次请求）">立即学习</button>
        <button class="ghost" data-act="clear_history" data-sid="${sid}" title="只清「待学习消息」缓冲，已学到的东西不动">清待学习记录</button>
        <button class="ghost danger" data-act="clear_learned" data-sid="${sid}" title="清这个会话学到的特征和待审条目，手动记忆保留">清学习产出</button>
        <button class="ghost danger" data-act="clear_manual" data-sid="${sid}" title="清这个会话所有人工添加的记忆，学习产出不受影响">清手动记忆</button>
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
        <button type="button" class="pin-btn${s.pinned ? " on" : ""}" data-pact="pin" data-sid="${escAttr(s.session_id)}"
          title="${s.pinned ? "取消置顶" : "置顶这个会话（会排在会话列表最前）"}">${
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
  // 显示完整注入（风格段 + 关系段 + 字数要求），而不是只显示风格段：
  // 只显示一段的话，看到的和实际发出去的不是一回事
  const body = payload.combined || payload.text || "";
  out.textContent = body || "（该会话当前不会注入任何内容）";
  out.classList.toggle("empty", !body);

  const parts = [];
  if (payload.strength !== undefined) {
    parts.push(`本次${previewActive ? "主动" : "被动"}强度 ${payload.strength}，让位概率 ${payload.override_probability ?? 0}%`);
  }
  if (payload.length_rule) parts.push("已带字数要求");
  parts.push("关系段按人不同，去「关系与输出」页看某个人实际会注入什么");
  $("previewNote").textContent = parts.join(" · ");
}

// ── 关系状态与输出层（1.5.0）─────────────────────────────────────────

const STAGE_TEXT = {
  hostile: "不待见",
  stranger: "陌生",
  familiar: "熟悉",
  close: "亲近",
  intimate: "老友",
};
const bondData = { items: [], settings: {}, groups: {}, enabled: false };

// 切到关系页时调。数据一直在变（群里聊着天就在涨），所以每次都重拉，
// 不做缓存——缓存的数字只会误导人。
async function ensureBond() {
  await loadBond();
}

// 拉关系数据并渲染。**这个函数上一版被我整段误删过**：
// 替换代码块时范围算大了，把 ensureBond 和 loadBond 一起带走了，
// 结果是页面上一片空白——列头在、统计和列表没有，
// 因为根本没人去拉数据。教训：大段替换后要 grep 一遍被替换区里的函数名。
async function loadBond() {
  try {
    const data = unwrap(await Bridge.apiGet("bond", {}));
    bondData.items = data.items || [];
    bondData.settings = data.settings || {};
    bondData.groups = data.groups || {};
    bondData.enabled = !!data.enabled;
    renderBond();
  } catch (err) {
    console.error("加载关系状态失败:", err);
    showToast("加载关系状态失败，请查看日志");
  }
}

// ── 通用列头筛选（Excel 式）──────────────────────────────────────────
//
// 关系 / 事实 / 学习库三个列表共用这一套。之前每个列表各写一遍：关系面板是
// 「排序下拉 + 阶段下拉」并排，事实面板是「搜索框 + 状态下拉 + 排序下拉」，
// 学习库塞在标题栏里。同一个产品里三种筛选交互，用户每换一页都要重新学一遍。
//
// 现在统一成列头：**每个字段一个按钮，点开是它自己的面板**——和 Excel 的
// 自动筛选一致。列头上直接标状态（有筛选挂 ●，排序中显示 ↑↓），
// 不用点开就知道当前是什么组合。
//
// 列定义支持三种能力，可以叠加：
//   type: "text"  关键词输入框
//   groups        多组复选框（组内「或」、组间也「或」：都是「我要看这些」）
//   type: "sort"  排序方向
//
// state 统一形状：{ text: {}, sets: {}, sort: "", dir: "desc" }
// 各面板只写自己的 columns 和 match 逻辑，交互全部由这里管。

function gfState(cfg) {
  const st = cfg.state;
  if (!st.text) st.text = {};
  if (!st.sets) st.sets = {};
  if (st.sort === undefined) st.sort = "";
  if (!st.dir) st.dir = "desc";
  return st;
}

function gfCol(cfg, col) {
  return (cfg.columns || []).find((c) => c.col === col);
}

function gfHasFilter(cfg, def) {
  const st = gfState(cfg);
  if (def.type === "text" && String(st.text[def.col] || "").trim()) return true;
  return (def.groups || []).some((g) => (st.sets[g.key] || []).length > 0);
}

// 列头上的状态：筛选挂 ●，排序显示箭头。就这么两件事，但必须画——
// 不画的话用户点完面板收起来，就看不出列表为什么只剩三个人了。
function gfPaint(cfg) {
  const bar = $(cfg.bar);
  if (!bar) return;
  const st = gfState(cfg);
  bar.querySelectorAll(".gf-head").forEach((btn) => {
    const def = gfCol(cfg, btn.dataset.col);
    if (!def) return;
    const caret = btn.querySelector(".gf-caret");
    const sorted = def.type === "sort" && st.sort === def.col;
    const filtered = gfHasFilter(cfg, def);
    btn.classList.toggle("sorted", sorted);
    btn.classList.toggle("filtered", filtered && !sorted);
    if (caret) {
      caret.textContent = sorted ? (st.dir === "asc" ? "↑" : "↓") : filtered ? "●" : "▼";
    }
  });
}

// 当前筛选组合的一句话摘要，挂在工具条右边。
// 比逐个列头看箭头直白：「熟悉 / 亲近 · 好感 ↓：12 / 66 人」。
function gfNoteText(cfg) {
  const st = gfState(cfg);
  const parts = [];
  (cfg.columns || []).forEach((def) => {
    const kw = String(st.text[def.col] || "").trim();
    if (def.type === "text" && kw) parts.push(`搜「${kw}」`);
    (def.groups || []).forEach((g) => {
      const cur = st.sets[g.key] || [];
      if (!cur.length) return;
      const opts = typeof g.options === "function" ? g.options() : g.options || [];
      parts.push(
        cur.map((k) => (opts.find((o) => o.key === k) || {}).label || k).join("/"),
      );
    });
    if (def.type === "sort" && st.sort === def.col) {
      parts.push(`${def.label} ${st.dir === "asc" ? "↑" : "↓"}`);
    }
  });
  return parts.join(" · ");
}

const GF_TEXT_ID = "gfText";

function gfPopHtml(cfg, col) {
  const def = gfCol(cfg, col);
  if (!def) return "";
  const st = gfState(cfg);
  const items = cfg.items ? cfg.items() : [];
  const blocks = [];

  if (def.type === "text") {
    blocks.push(
      `<p class="gf-title">${esc(def.title || "按对象筛选")}</p>` +
        `<input id="${GF_TEXT_ID}" type="search" placeholder="${escAttr(
          def.placeholder || "",
        )}" value="${escAttr(st.text[col] || "")}" />`,
    );
  }

  (def.groups || []).forEach((g) => {
    const opts = typeof g.options === "function" ? g.options() : g.options || [];
    const cur = st.sets[g.key] || [];
    const rows = opts
      .map((o) => {
        const on = cur.includes(o.key);
        const n = g.count ? Number(g.count(items, o.key) || 0) : null;
        // 0 人且没勾的置灰：勾上等于筛出空列表，看着像坏了
        const dead = n === 0 && !on;
        return `<label class="gf-opt${dead ? " disabled" : ""}">
          <input type="checkbox" data-set="${escAttr(g.key)}" value="${escAttr(
            o.key,
          )}"${on ? " checked" : ""}${dead ? " disabled" : ""} /> ${esc(o.label)}${
            n === null ? "" : `<span class="gf-count">${n}</span>`
          }
        </label>`;
      })
      .join("");
    blocks.push(
      `<p class="gf-title"${
        blocks.length ? ' style="margin-top:10px"' : ""
      }>${esc(g.title || "筛选")}</p>${rows}`,
    );
  });

  if (def.type === "sort") {
    const on = st.sort === col;
    blocks.push(
      `<p class="gf-title"${
        blocks.length ? ' style="margin-top:10px"' : ""
      }>按${esc(def.label || "值")}排序</p>
      <label class="gf-opt"><input type="radio" name="gfsort" value="desc"${
        on && st.dir === "desc" ? " checked" : ""
      } /> ${esc(def.descLabel || "降序（大的在前）")}</label>
      <label class="gf-opt"><input type="radio" name="gfsort" value="asc"${
        on && st.dir === "asc" ? " checked" : ""
      } /> ${esc(def.ascLabel || "升序（小的在前）")}</label>`,
    );
  }

  // 只清这一列，整表重置在工具条上——两个粒度都要有，
  // 只想撤掉「状态」的人不该被迫连搜索词一起丢
  const acts = ['<button class="ghost tiny" data-gf="clear" title="只清掉这一列的筛选条件">清空这列</button>'];
  if (def.type === "sort") {
    acts.push(
      `<button class="ghost tiny" data-gf="${
        st.sort === col ? "sortoff" : "sortdesc"
      }">${st.sort === col ? "不按这列排" : "按降序排"}</button>`,
    );
  }
  blocks.push(`<div class="gf-actions">${acts.join("")}</div>`);
  return blocks.join("");
}

function gfBind(cfg, col, pop) {
  const def = gfCol(cfg, col);
  const st = gfState(cfg);

  // 面板内容每次都是重写的，所以没有「局部更新」——任何操作都是
  // 改 state → 重渲染列表 → 重开面板（顺带把输入框焦点放回去）
  const again = () => {
    const bar = $(cfg.bar);
    const btn = bar ? bar.querySelector(`[data-col="${col}"]`) : null;
    gfOpen(cfg, col, btn);
  };

  const text = pop.querySelector("#" + GF_TEXT_ID);
  if (text) {
    text.addEventListener("input", (e) => {
      st.text[col] = e.target.value || "";
      syncSearchBox(cfg);
      cfg.render();
      again();
      // 重渲染会把面板重建，光标就丢了。打一个字跳一次焦点没法用。
      const box = $(cfg.pop).querySelector("#" + GF_TEXT_ID);
      if (box) {
        box.focus();
        box.setSelectionRange(box.value.length, box.value.length);
      }
    });
  }

  pop.querySelectorAll("input[data-set]").forEach((box) => {
    box.addEventListener("change", () => {
      const key = box.dataset.set;
      const cur = st.sets[key] || [];
      st.sets[key] = box.checked
        ? [...new Set([...cur, box.value])]
        : cur.filter((v) => v !== box.value);
      cfg.render();
      again();
    });
  });

  pop.querySelectorAll('input[name="gfsort"]').forEach((radio) => {
    radio.addEventListener("change", () => {
      st.sort = col;
      st.dir = radio.value;
      cfg.render();
      again();
    });
  });

  pop.querySelectorAll("button[data-gf]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      const act = btn.dataset.gf;
      if (act === "clear") {
        if (def.type === "text") st.text[col] = "";
        (def.groups || []).forEach((g) => {
          st.sets[g.key] = [];
        });
      } else if (act === "sortoff") {
        st.sort = "";
      } else if (act === "sortdesc") {
        st.sort = col;
        st.dir = "desc";
      }
      cfg.render();
      again();
    });
  });
}

function gfClose(cfg) {
  const pop = $(cfg.pop);
  if (!pop) return;
  pop.hidden = true;
  pop.dataset.col = "";
  pop.innerHTML = "";
}

// ``toggle`` 只有点列头时才传：勾复选框、打字这些「面板内部的操作」
// 不该把面板收起来——Excel 里也不这样。
function gfOpen(cfg, col, btn, { toggle = false } = {}) {
  const pop = $(cfg.pop);
  if (!pop || !btn) return;
  if (toggle && !pop.hidden && pop.dataset.col === col) {
    gfClose(cfg);
    return;
  }
  pop.dataset.col = col;
  pop.innerHTML = gfPopHtml(cfg, col);
  pop.hidden = false;
  // 跟被点的列左对齐；靠右的列往回拉一点，别顶出容器
  const width = pop.offsetWidth || 240;
  const limit = (btn.parentElement?.clientWidth || 600) - width - 8;
  pop.style.left = `${Math.max(0, Math.min(btn.offsetLeft, limit))}px`;
  gfBind(cfg, col, pop);
}

// 一个工具条只初始化一次：页面切来切去不会把监听器叠成一堆
// 外部搜索框和列头弹窗里的输入框改的是同一份 state（text.who）。
// 改完要把另一处的显示对齐，否则会出现「明明筛了，框里还是旧词」。
function syncSearchBox(cfg) {
  const box = document.getElementById(cfg.search || "");
  if (!box) return;
  const want = String(gfState(cfg).text.who || "");
  if (box.value !== want) box.value = want;
}

function gfInitBar(cfg) {
  const bar = $(cfg.bar);
  if (!bar || bar.dataset.gfInit) return;
  bar.dataset.gfInit = "1";

  // 常驻搜索框：输入即筛，走的是同一套 state，所以列头上的 ● 也会亮
  const box = document.getElementById(cfg.search || "");
  if (box) {
    box.addEventListener("input", () => {
      gfState(cfg).text.who = box.value;
      gfPaint(cfg);
      cfg.render();
    });
  }
  bar.addEventListener("click", (e) => {
    const head = e.target.closest(".gf-head");
    if (!head) return;
    e.preventDefault();
    e.stopPropagation();
    gfOpen(cfg, head.dataset.col, head, { toggle: true });
  });
  const reset = bar.querySelector("[data-gf-reset]");
  if (reset) {
    reset.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      cfg.reset();
      gfClose(cfg);
      syncSearchBox(cfg);
      cfg.render();
    });
  }
  // 点面板以外收起。用 mousedown 而不是 click：点另一个列头时
  // document 的 click 会先到，把刚打开的面板立刻关掉。
  document.addEventListener("mousedown", (e) => {
    if (!e.target.closest("#" + cfg.bar)) gfClose(cfg);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") gfClose(cfg);
  });
}

// ── 关系面板的列定义 ─────────────────────────────────────────────────

const bondFilter = { text: {}, sets: {}, sort: "", dir: "desc" };

const FLAG_TEXT = {
  idle: "7 天以上没聊",
  owner: "只看主人",
  pinned: "只看置顶",
};

function matchFlag(item, flag) {
  if (flag === "idle") return (item.idle_days || 0) >= 7;
  if (flag === "owner") return !!item.is_owner;
  if (flag === "pinned") return !!(item.pinned || item.is_owner);
  return false;
}

const BOND_GF = {
  bar: "bondFilterBar",
  pop: "bondFilterPop",
  // 列表上方那个常驻搜索框的 id。它和列头弹窗里的输入框共用 state.text.who
  search: "bondSearch",
  state: bondFilter,
  items: () => bondData.items,
  render: renderBond,
  reset: () => {
    bondFilter.text = {};
    bondFilter.sets = {};
    bondFilter.sort = "";
    bondFilter.dir = "desc";
  },
  columns: [
    {
      col: "who",
      label: "对象",
      type: "text",
      title: "按对象筛选",
      placeholder: "QQ 号 / 昵称 / 群号",
      groups: [
        {
          key: "flags",
          title: "附加条件",
          options: Object.keys(FLAG_TEXT).map((key) => ({ key, label: FLAG_TEXT[key] })),
          count: (items, key) => items.filter((i) => matchFlag(i, key)).length,
        },
      ],
    },
    {
      col: "stage",
      label: "阶段",
      groups: [
        {
          key: "stages",
          title: "按阶段（可多选）",
          options: Object.entries(STAGE_TEXT).map(([key, label]) => ({ key, label })),
          count: (items, key) => items.filter((i) => i.stage === key).length,
        },
      ],
    },
    { col: "intimacy", label: "好感", type: "sort" },
    { col: "turns", label: "轮次", type: "sort" },
    {
      col: "idle",
      label: "最近聊天",
      type: "sort",
      descLabel: "降序（最久没聊的在前）",
      ascLabel: "升序（刚聊过的在前）",
    },
  ],
};

function matchBond(item) {
  const st = gfState(BOND_GF);
  const kw = String(st.text.who || "").trim().toLowerCase();
  if (kw) {
    const hay = [item.sender_id, item.sender_name, item.nickname, item.session_label, item.key]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    if (!hay.includes(kw)) return false;
  }
  // 阶段和附加条件同属「阶段」这一列，用「或」：
  // 勾了「熟悉」又勾了「7 天没聊」，意思是这两拨人我都要看。
  const stages = st.sets.stages || [];
  const flags = st.sets.flags || [];
  if (stages.length || flags.length) {
    const hitStage = stages.includes(item.stage);
    const hitFlag = flags.some((f) => matchFlag(item, f));
    if (!hitStage && !hitFlag) return false;
  }
  return true;
}

function isPinnedBond(item) {
  return !!(item.is_owner || item.pinned);
}

function sortBond(list) {
  const st = gfState(BOND_GF);
  if (!st.sort) return list;
  const pick = {
    intimacy: (i) => Number(i.intimacy || 0),
    turns: (i) => Number(i.turns || 0),
    idle: (i) => Number(i.idle_days || 0),
  }[st.sort];
  if (!pick) return list;
  const mul = st.dir === "asc" ? 1 : -1;
  const cmp = (a, b) => mul * (pick(a) - pick(b));
  // 分三段：主人永远第一，手动置顶的次之，其余最后。
  //
  // 只分「钉住 / 不钉住」两段是不够的：把一个「不待见」的人钉上去，
  // 再按好感升序排，他会排到主人前面——「主人默认置顶」就不成立了。
  // 段内仍然按当前排序走（主人有好几个时也用得上）。
  const owner = list.filter((i) => i.is_owner).sort(cmp);
  const pinned = list.filter((i) => !i.is_owner && i.pinned).sort(cmp);
  const rest = list.filter((i) => !i.is_owner && !i.pinned).sort(cmp);
  return owner.concat(pinned, rest);
}

// 壳：真正的渲染逻辑在 renderBondBody，这里只负责接住异常。
// 包在这一层是因为调用点有十来个（切页、刷新、筛选、置顶、改数值…），
// 逐个加 try 一定会漏，漏掉的那个就是下次的「一片空白」。
function renderBond() {
  renderGuard("bondList", "关系列表", renderBondBody);
}

function renderBondBody() {
  const { items, settings, groups, enabled } = bondData;
  const capText = settings.group_cap_familiar
    ? "群聊封顶在「熟悉」，只有私聊会到「亲近」"
    : "群聊也能到「亲近」";
  const tierText = settings.tiered_memory === false
    ? "记忆量不分档（按配置固定上限）"
    : "记忆量按好感分档，越熟记得越多";
  $("bondNote").textContent = enabled
    ? `共 ${groups.total ?? 0} 人 · ${groups.familiar_or_more ?? 0} 人已「熟悉」以上 · ` +
      `${groups.hostile ?? 0} 人负好感 · ${tierText} · ${capText}`
    : "关系状态没开。开启后才会记录每个人和 bot 的相处进度，每轮注入一小段，不产生额外 LLM 调用。";

  $("bondStats").innerHTML = [
    ["关系人数", groups.total ?? 0],
    ["已熟悉以上", groups.familiar_or_more ?? 0],
    ["负好感", groups.hostile ?? 0],
    ["7 天没聊", groups.idle ?? 0],
    ["状态", enabled ? "已开启" : "已关闭"],
  ]
    .map(([k, v]) => `<div class="stat"><span>${esc(k)}</span><b>${esc(String(v))}</b></div>`)
    .join("");

  // 列头状态先画：数据变了，各档的计数和箭头也要跟着变
  gfPaint(BOND_GF);

  if (!items.length) {
    $("bondFilterNote").textContent = "";
    $("bondList").innerHTML = `<p class="empty">${
      enabled ? "还没有记录到关系——群里正常聊几句就有了" : "开启后这里会列出每个人和 bot 的相处进度"
    }</p>`;
    return;
  }

  const visible = sortBond(items.filter(matchBond));
  const note = gfNoteText(BOND_GF);
  $("bondFilterNote").textContent = note
    ? `${note}：${visible.length} / ${items.length} 人`
    : `${visible.length} 人`;
  if (!visible.length) {
    $("bondList").innerHTML = `<p class="empty">没有符合条件的记录</p>`;
    return;
  }
  $("bondList").innerHTML = visible.map(bondCard).join("");
}

function bondCard(item) {
  const stage = item.stage || "stranger";
  const who = item.sender_name
    ? `${item.sender_name}（${item.sender_id}）`
    : item.sender_id;
  const progress = item.capped
    ? "群聊已封顶"
    : item.next_label
      ? `差 ${item.need} 点到「${item.next_label}」`
      : "已到最高档";
  const nick = item.nickname
    ? `称呼「${item.nickname}」`
    : `未设称呼${item.sender_name ? `（按平台昵称「${item.sender_name}」）` : ""}`;
  // 注入文本是这条记录真正的样子，包括「被整段丢弃」这种情况
  const inject = item.inject_text
    ? esc(item.inject_text)
    : "（这条现在不会注入：可能关系总开关关着，或字数超上限被整段丢弃）";
  const stageName = STAGE_TEXT[stage] || stage;
  // 投喂记录：没喂过就整行不渲染，免得每张卡都挂一行「喂过 0 次」
  const fed = item.fed || {};
  const fedNote = Number(fed.total || 0)
    ? `🍚 喂过 ${fed.total} 次${fed.last_dish ? `（最近：${fed.last_dish}）` : ""}${
        fed.limit ? ` · 今天 ${fed.today ?? 0}/${fed.limit}` : ""
      }`
    : "";
  // 主人单独标出来：他的阶段不会停在「熟悉」，不标的话会被当成配置没生效
  const ownerTag = item.is_owner ? "👑 " : "";
  const ownerNote = item.is_owner ? " · 主人（群聊不封顶）" : "";
  // 主人恒定排在最前，不需要也不能取消；其余人给一个手动钉的开关
  const pinBtn = item.is_owner
    ? `<span class="bond-pin owner">默认置顶</span>`
    : `<button class="ghost tiny bond-pin-btn" data-bond="${
        item.pinned ? "unpin" : "pin"
      }" data-key="${escAttr(item.key)}">${item.pinned ? "📌 取消置顶" : "置顶"}</button>`;
  const pinnedTag = !item.is_owner && item.pinned ? "📌 " : "";
  // 这一档实际会注入多少记忆：把「省钱省在哪」直接写在卡片上，
  // 否则用户看到陌生人只有 120 字，会怀疑是 bug
  const tier = Array.isArray(item.tier)
    ? `记忆配额：关系段 ${item.tier[0]} 字 · 事实 ${item.tier[1]} 条 / ${item.tier[2]} 字`
    : "记忆配额：不分档（按配置固定上限）";
  const idleDays = item.idle_days || 0;
  // 衰减只在「已经掉过」的时候提示，0 天不刷屏
  const idleNote =
    idleDays >= 1
      ? ` · ${idleDays} 天没聊，衰减后保留 ${Math.round(Number(item.keep_ratio ?? 1) * 100)}%`
      : "";
  // 冷却中说明下一次变动还要等多久。不写这句的话，
  // 用户在群里说完话回来看点数没动，会以为插件坏了
  const cdLeft = Number(item.cooldown_left || 0);
  const cdNote = cdLeft > 0 ? ` · 冷却中，还要 ${Math.ceil(cdLeft / 60)} 分钟才会再变` : "";
  const value = Number(item.intimacy || 0);
  // 千分制下 300.0 显示成「300.0 点」很别扭；只有真的带小数（群聊 +1.5）才留一位
  const valueText = Number.isInteger(value) ? String(value) : value.toFixed(1);
  return `<details class="session">
    <summary>
      <b>${ownerTag}${pinnedTag}${esc(who)}</b>
      <span>最近 ${esc(item.session_label)} · ${esc(stageName)} · ${item.turns} 轮 · ${
        item.days
      } 天 · ${esc(valueText)} 点${ownerNote}</span>
      ${pinBtn}
    </summary>
    <div class="body">
      <p class="hint">${esc(nick)} · ${esc(progress)} · 上次 ${esc(
        item.last_talk_text || "—",
      )}${esc(idleNote)}${esc(cdNote)}</p>
      <p class="hint">${esc(tier)}</p>
      ${fedNote ? `<p class="hint fed-note">${esc(fedNote)}</p>` : ""}
      <div class="bond-adjust">
        <span>亲密度</span>
        <input type="number" class="bond-value" data-key="${escAttr(item.key)}"
          min="-100" max="1000" step="10" value="${value}" />
        <button class="ghost tiny" data-bond="set_intimacy" data-key="${escAttr(
          item.key,
        )}" title="把亲密度改成输入框里的值（不改变聊过多少轮、认识多少天）">应用</button>
        <span class="hint">范围 -100 ~ 1000，改完立刻生效</span>
      </div>
      <div class="bond-adjust">
        <span>称呼</span>
        <input type="text" class="bond-nickname" data-key="${escAttr(item.key)}"
          value="${escAttr(item.nickname || "")}"
          placeholder="留空 = 用平台昵称${item.sender_name ? `（${escAttr(item.sender_name)}）` : ""}" />
        <button class="ghost tiny" data-bond="set_nickname" data-key="${escAttr(
          item.key,
        )}" title="保存称呼。多个称呼用逗号分隔，注入时会告诉模型「你叫他 A 或 B」">保存</button>
        <span class="hint">多个称呼用逗号 / 顿号分隔，最多 40 字；不影响自动判定，只覆盖叫法</span>
      </div>
      <div class="session-actions">
        <button class="ghost" data-bond="clear_nickname" data-key="${escAttr(item.key)}"
          title="清掉给 bot 指定的叫法，之后按默认方式称呼"${
          item.nickname ? "" : " disabled"
        }>清称呼</button>
        <button class="ghost" data-bond="reset" data-key="${escAttr(item.key)}"
          title="把好感归零（回到陌生起点），关系记录还在">亲密度归零</button>
        <button class="ghost danger" data-bond="delete" data-key="${escAttr(item.key)}"
          title="删掉这个人的关系记录（好感、称呼一起没，不可恢复）">删除</button>
      </div>
      <p class="hint">这条关系现在会注入的内容：</p>
      <pre class="preview-out">${inject}</pre>
    </div>
  </details>`;
}

async function bondAction(action, key, value) {
  if (action === "delete") {
    const ok = await askConfirm("删掉这条关系记录？亲密度、轮次、称呼会一起没，无法恢复。");
    if (!ok) return;
  }
  const body = { action, key };
  if (action === "set_intimacy") {
    const num = Number(value);
    if (value === "" || !Number.isFinite(num)) {
      showToast("亲密度要填个数字");
      return;
    }
    body.value = Math.max(-100, Math.min(1000, num));
  }
  if (action === "set_nickname") {
    body.nickname = String(value ?? "").slice(0, 40);
  }
  const payload = unwrap(await Bridge.apiPost("bond", body));
  if (payload && payload.success === false) {
    showToast(payload.message || "操作失败");
    return;
  }
  showToast(payload.message || "已处理");
  await loadBond();
}

// ── 输出层调试 ──────────────────────────────────────────────────────

let toneReady = false;

function syncToneSwitches() {
  // 用当前配置当初始值，之后改开关只影响这次预览，不写回配置
  if (toneReady) return;
  const groups = (lastStatus && lastStatus.groups) || {};
  const toneCfg = groups.tone;
  if (!toneCfg) return;
  $("toneClean").checked = !!toneCfg.clean;
  $("toneSqueeze").checked = !!toneCfg.squeeze;
  $("toneMarkdown").checked = !!toneCfg.markdown;
  $("toneSegment").checked = !!toneCfg.segment;
  $("toneChars").value = toneCfg.segment_chars || 60;
  $("toneParts").value = toneCfg.segment_parts || 4;
  toneReady = true;
}

async function runTone() {
  const text = $("toneInput").value;
  if (!text.trim()) {
    showToast("先粘一段回复进去");
    return;
  }
  const body = {
    text,
    clean: $("toneClean").checked,
    squeeze: $("toneSqueeze").checked,
    markdown: $("toneMarkdown").checked,
    segment: $("toneSegment").checked,
    segment_chars: Number($("toneChars").value) || 60,
    segment_parts: Number($("toneParts").value) || 4,
  };
  const payload = unwrap(await Bridge.apiPost("tone", body));
  if (payload && payload.success === false) {
    showToast(payload.message || "解析失败");
    return;
  }

  const saved = payload.chars_saved ?? 0;
  const savedText = saved > 0 ? `省了 ${saved} 字` : saved < 0 ? `多了 ${-saved} 字` : "字数没变";
  const segs = payload.segments || [];
  const segHtml = segs.length
    ? `<div class="tone-segs">${segs
        .map(
          (s, i) =>
            `<div class="tone-seg"><i>第 ${i + 1} 条 · ${s.chars} 字</i><span>${esc(s.text)}</span></div>`,
        )
        .join("")}</div>`
    : `<p class="empty">拆条没产出内容</p>`;
  const blocked = payload.segment_blocked
    ? `<p class="hint">这段里有代码块或链接，按规则不拆条——切开会把格式切坏。</p>`
    : "";

  $("toneOut").innerHTML = `
    <p class="hint">原 ${payload.chars_before ?? 0} 字 → 处理后 ${payload.chars_after ?? 0} 字 · ${savedText}</p>
    <p class="hint">清理后的完整回复：</p>
    <pre class="preview-out">${esc(payload.cleaned || "")}</pre>
    ${blocked}
    <p class="hint">如果宿主开了「分段回复」，实际会按这样发出去：</p>
    ${segHtml}
  `;
}

// ── 事实记忆（1.5.0）─────────────────────────────────────────────────

const factData = { items: [], settings: {}, stats: {}, enabled: false };

async function loadFacts() {
  try {
    const data = unwrap(await Bridge.apiGet("facts", {}));
    factData.items = data.items || [];
    factData.settings = data.settings || {};
    factData.stats = data.stats || {};
    factData.enabled = !!data.enabled;
    renderFacts();
  } catch (err) {
    console.error("加载事实记忆失败:", err);
    showToast("加载事实记忆失败，请查看日志");
  }
}

// ── 事实面板的列定义 ─────────────────────────────────────────────────

const factFilter = { text: {}, sets: {}, sort: "", dir: "desc" };

// 「还记着几条」：不含已否决。清理过的人如果按 total 算，
// 清完一遍显示的还是 12 条，用户会以为按钮没生效。
function factAlive(row) {
  if (row.alive !== undefined && row.alive !== null) return Number(row.alive);
  return (row.items || []).filter((i) => i.status_key !== "rejected").length;
}

// 最近一次被提到的时间
function factLastSeen(row) {
  return Math.max(0, ...(row.items || []).map((i) => Number(i.last_seen || 0)));
}

// 这个人记忆里最低的保留度：一键清理该不该动他，看这个最直观
function factMinKeep(row) {
  const vals = (row.items || []).map((i) => Number(i.keep ?? 1));
  return vals.length ? Math.min(...vals) : 1;
}

// 附加条件的判定。写成函数而不是内联在 matchFact 里，是因为列头的计数
// 也要用它——两处各写一遍，早晚会不一致（显示 3 人，点开只剩 2 人）。
function matchFactFlag(row, flag) {
  if (flag === "owner") return !!row.is_owner;
  // 「接近上限」用 80%：剩不到两成额度时，下一条新事实进来就要淘汰旧的，
  // 这时候该主动清一次，而不是等系统按分数偷偷扔
  if (flag === "full") return factAlive(row) >= Number(row.limit || 0) * 0.8;
  // 「一键清理会动」：现在条数超过这个档位的保留目标，且不是主人
  if (flag === "prunable") {
    return !row.is_owner && Number(row.keep_target || 0) > 0 &&
      factAlive(row) > Number(row.keep_target || 0);
  }
  if (flag === "nopin") return !(row.items || []).some((i) => i.pinned);
  return false;
}

const FACT_GF = {
  bar: "factFilterBar",
  pop: "factFilterPop",
  search: "factSearch",
  state: factFilter,
  items: () => factData.items,
  render: renderFacts,
  reset: () => {
    factFilter.text = {};
    factFilter.sets = {};
    factFilter.sort = "";
    factFilter.dir = "desc";
  },
  columns: [
    {
      col: "who",
      label: "对象",
      type: "text",
      title: "按对象筛选",
      placeholder: "QQ 号 / 昵称 / 记忆内容",
      groups: [
        {
          key: "flags",
          title: "附加条件",
          options: [
            { key: "owner", label: "只看主人" },
            { key: "full", label: "接近上限（≥80%）" },
            { key: "prunable", label: "一键清理会动" },
            { key: "nopin", label: "没有钉住的" },
          ],
          count: (items, key) => items.filter((i) => matchFactFlag(i, key)).length,
        },
      ],
    },
    {
      col: "stage",
      label: "档位",
      groups: [
        {
          key: "stages",
          title: "按好感档位（存储上限跟着档位走）",
          options: Object.entries(STAGE_TEXT).map(([key, label]) => ({ key, label })),
          count: (items, key) => items.filter((i) => i.stage === key).length,
        },
      ],
    },
    {
      col: "status",
      label: "状态",
      groups: [
        {
          key: "statuses",
          title: "按记忆状态（可多选）",
          options: [
            { key: "active", label: "已生效" },
            { key: "pending", label: "待观察" },
            { key: "suspect", label: "疑似记错" },
            { key: "archived", label: "已归档" },
            { key: "rejected", label: "已否决" },
          ],
          // 状态是条目级的，行是「人」：只要这个人有一条处于该状态就算命中，
          // 展开时也只显示那些条目
          count: (items, key) =>
            items.filter((i) => (i.items || []).some((x) => x.status_key === key)).length,
        },
      ],
    },
    { col: "total", label: "条数", type: "sort" },
    {
      col: "keep",
      label: "保留度",
      type: "sort",
      descLabel: "降序（记得最牢的）",
      ascLabel: "升序（快忘光的在前）",
    },
  ],
};

function matchFact(row) {
  const st = gfState(FACT_GF);

  // 行级条件先过：档位、附加条件都是「这个人」的属性
  const stages = st.sets.stages || [];
  const flags = st.sets.flags || [];
  if (stages.length || flags.length) {
    const hitStage = stages.includes(row.stage);
    const hitFlag = flags.some((f) => matchFactFlag(row, f));
    if (!hitStage && !hitFlag) return null;
  }

  let items = row.items || [];

  const kw = String(st.text.who || "").trim().toLowerCase();
  if (kw) {
    const meta = [row.sender_id, row.sender_name, row.session_label, row.key]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    if (!meta.includes(kw)) {
      // 只命中内容时，只留命中的那几条：否则会出现「搜到了但看不出是哪条」
      items = items.filter((i) => String(i.text || "").toLowerCase().includes(kw));
      if (!items.length) return null;
    }
  }

  const statuses = st.sets.statuses || [];
  if (statuses.length) {
    items = items.filter((i) => statuses.includes(i.status_key));
    if (!items.length) return null;
  }
  return { ...row, items };
}

function sortFacts(list) {
  const st = gfState(FACT_GF);
  const mul = st.dir === "asc" ? 1 : -1;
  // 没选排序时按条数降序：这是原来的默认，不改变习惯。
  // 「快忘光的」只在显式选了保留度时才管排序，平时不该抢这个位置
  const pick =
    st.sort === "keep"
      ? factMinKeep
      : st.sort === "total" || !st.sort
        ? (i) => Number(i.total || 0)
        : null;
  if (!pick) return [...list];
  const cmp = (a, b) => mul * (pick(a) - pick(b));
  // 三段式：主人 → 置顶 → 其余，和关系页同一套规矩。
  // 以前置顶只写进了 storage，列表排序没认它，「置顶这个人」点了等于没反应
  const owner = list.filter((i) => i.is_owner).sort(cmp);
  const pinned = list.filter((i) => !i.is_owner && i.pinned).sort(cmp);
  const rest = list.filter((i) => !i.is_owner && !i.pinned).sort(cmp);
  return owner.concat(pinned, rest);
}

function renderFacts() {
  renderGuard("factsList", "事实记忆", renderFactsBody);
}

function renderFactsBody() {
  const { items, settings, stats, enabled } = factData;
  const ownerFacts = stats.owner_facts ?? 0;
  // 写「存储上限按档位」而不是「每轮注入多少字」：前者是这一版的新规则，
  // 用户看到「这个人上限 20、那个 300」时会想知道为什么
  $("factsNote").textContent = enabled
    ? `共 ${stats.alive ?? stats.facts ?? 0} 条，分属 ${stats.people ?? 0} 个人 · 生效 ${stats.active ?? 0} · ` +
      `已归档 ${stats.archived ?? 0} · 按 QQ 号唯一（跨群共享）· 存储上限按好感档位（陌生 20 → 老友 100）` +
      ` · 三档按熟练度自动分，可手动升降` +
      (ownerFacts ? ` · 主人 ${ownerFacts} 条不受清理影响` : "")
    : "事实记忆没开。开启后会记住「关于某个人的具体事」，按 QQ 号存，A 的私事不会注入到回复 B 里。";

  // 「一键清理后」比「已生效」有用得多：前者是「点一下会变成什么」，
  // 后者只有点开每张卡才看得出效果。
  // 中间那格是三档分布：核心 / 深层 / 浅层，顺序固定，和条目上的徽标一致
  const tiers = stats.tiers || {};
  $("factStats").innerHTML = [
    ["记忆条数", stats.alive ?? stats.facts ?? 0],
    ["覆盖人数", stats.people ?? 0],
    ["核心 / 深层 / 浅层", `${tiers.core ?? 0} / ${tiers.deep ?? 0} / ${tiers.shallow ?? 0}`],
    ["一键清理后", stats.keep_after ?? stats.alive ?? stats.facts ?? 0],
    ["主人（不清）", ownerFacts],
  ]
    .map(([k, v]) => `<div class="stat"><span>${esc(k)}</span><b>${esc(String(v))}</b></div>`)
    .join("");

  // 列头状态先画：数据变了，各档人数和箭头也要跟着变
  gfPaint(FACT_GF);

  // 「清理主人」只在真有主人记忆时出现。文案带保留量，用户点之前就知道会剩几条
  const pruneOwnerBtn = $("factPruneOwner");
  if (pruneOwnerBtn) {
    const ownerRow = items.find((r) => r.is_owner);
    const keep = ownerRow ? Number(ownerRow.keep_target || 0) : 0;
    pruneOwnerBtn.hidden = !ownerRow;
    if (ownerRow) {
      // 主人单独清理的保留量（150），和一键清理的档位表无关
      pruneOwnerBtn.textContent = keep ? `清理主人（留 ${keep}）` : "清理主人";
      pruneOwnerBtn.disabled = !(Number(ownerRow.alive || 0) > keep);
    }
  }

  if (!items.length) {
    $("factsList").innerHTML = `<p class="empty">${
      enabled ? "还没有记住任何事——群友聊到具体的安排、偏好时会自动抽取" : "开启后这里会列出每个人被记住的事"
    }</p>`;
    if ($("factFilterNote")) $("factFilterNote").textContent = "";
    return;
  }

  const visible = sortFacts(items.map(matchFact).filter(Boolean));
  const cond = gfNoteText(FACT_GF);
  const note = $("factFilterNote");
  if (note) {
    note.textContent = cond
      ? `${cond}：${visible.length} / ${items.length} 人`
      : `${visible.length} / ${items.length} 人`;
  }
  if (!visible.length) {
    $("factsList").innerHTML = `<p class="empty">没有符合条件的记忆</p>`;
    return;
  }
  $("factsList").innerHTML = visible.map(factBucketCard).join("");
}

function factBucketCard(row) {
  // 名字在前、QQ 在后，和关系页同一套写法：翻记忆时认名字比认号快，
  // 号留在括号里是给「要核对是不是同一个人」的时候用的
  const name = String(row.sender_name || "").trim();
  const who = name
    ? `${row.is_owner ? "👑 " : ""}${name}（${row.sender_id}）`
    : `${row.is_owner ? "👑 " : ""}${row.sender_id}`;
  // 手动称呼单独标：它和平台昵称不是一回事，别让人以为昵称被改过
  const nickNote = row.nickname ? ` · 你叫他「${row.nickname}」` : "";
  // 「最近在哪个群」只是展示信息：事实按 QQ 号存，跨群共享，
  // 写在这里是为了让人知道这个人主要在哪儿出现过
  const where = row.session ? `最近在 ${esc(row.session_label)}` : "会话未知";
  const cap = row.limit ? ` / ${row.limit}` : "";
  const archived = row.archived ? ` · 归档 ${row.archived}` : "";
  // 已否决的单独标出来：它们还在文件里留档（能恢复），但不算「记着」
  const rejected = row.rejected ? ` · 已否决 ${row.rejected}` : "";
  const alive = factAlive(row);
  // 档位决定上限：写出来用户才知道「为什么这个人只能存 5 条」
  const stage = row.stage_label ? ` · ${esc(row.stage_label)}档` : "";
  const keepNote = row.is_owner
    ? " · 主人不参与清理"
    : row.keep_target
      ? ` · 清理后留 ${row.keep_target} 条`
      : "";
  // 档位计数：让用户知道「这个人最多能写死几条、中间那层现在有几条」，
  // 而不是看到核心数量莫名其妙。深层没有名额限制，所以只显示数字
  const coreNote = row.core_quota
    ? ` · 核心 ${row.core_count ?? 0}/${row.core_quota} · 深层 ${row.deep_count ?? 0}`
    : "";
  // 快到上限时才提示，平时不刷屏
  const full = alive >= Number(row.limit || 0) * 0.8;
  const fullTag = full ? '<span class="fact-full">接近上限</span>' : "";
  // 单人清理按钮：条数没超过保留目标时禁用，点了也是「不用清」
  const canPrune = !row.is_owner && alive > Number(row.keep_target || 0);
  const pruneBtn = `<button class="ghost tiny" data-fact-prune="${escAttr(row.key)}"${
    canPrune ? "" : " disabled"
  }>按档位瘦身${row.keep_target ? `（留 ${row.keep_target}）` : ""}</button>`;
  const clearBtn = row.total
    ? `<button class="ghost tiny danger" data-fact-clear="${escAttr(row.key)}"
        title="把这个人的记忆全部删掉（不可恢复，画像也一起清）">清空这人</button>`
    : "";
  const rows = (row.items || [])
    .map((it) => {
      // 保留度：30 天归零。低于 60% 标黄，归零标红——一眼看出哪些快忘了
      const keep = Number(it.keep ?? 1);
      const pct = Math.round(keep * 100);
      const cls = keep <= 0 ? "gone" : keep < 0.6 ? "low" : "";
      const tier = it.tier || "shallow";
      const label = it.tier_label || TIER_TEXT[tier] || "浅层";
      // 熟练度：三档就是按它分的，所以必须显示出来——
      // 不然用户看到「这条是浅层、那条是核心」会不知道依据是什么
      const prof = Math.round(Number(it.prof ?? 0));
      const manual = it.tier_manual ? " · 手动" : "";
      const tierTag = `<span class="fact-tier ${esc(tier)}" data-fact-auto="${escAttr(it.id)}"
        data-fact-key="${escAttr(row.key)}"
        title="${esc(
          `${label}记忆 · 熟练度 ${prof}${manual}。熟练度 = 提到次数 60% + 重要性 25% + 新鲜度 15%` +
            `\n点一下交回自动判定`,
        )}">${esc(label)} <b>${prof}</b></span>`;
      // ↑ / ↓ 各挪一档，点错了再点回来，所以不做二次确认
      const upBtn = `<button class="ghost tiny" data-fact-tier="${escAttr(it.id)}"
        data-fact-dir="1" data-fact-key="${escAttr(row.key)}"
        title="升一级（浅层 → 深层 → 核心）"
        ${tier === "core" ? "disabled" : ""}>↑</button>`;
      const downBtn = `<button class="ghost tiny" data-fact-tier="${escAttr(it.id)}"
        data-fact-dir="-1" data-fact-key="${escAttr(row.key)}"
        title="降一级（核心 → 深层 → 浅层）"
        ${tier === "shallow" ? "disabled" : ""}>↓</button>`;
      const variant = (it.variants || []).length
        ? `<span class="fact-variant" title="${escAttr((it.variants || []).join("\n"))}">还有 ${
            (it.variants || []).length
          } 种说法</span>`
        : "";
      return `<li class="fact-row${it.pinned ? " pinned" : ""}">
        <span class="fact-tag${it.active ? " on" : ""}">${esc(it.status)}</span>
        <span class="fact-kind">${esc(it.kind)}</span>
        ${tierTag}
        <span class="fact-text">${esc(it.text)}</span>
        ${variant}
        <span class="fact-keep ${cls}" title="按遗忘曲线还剩多少：到了就归零并归档">${
          keep <= 0 ? "忘光" : `${pct}%`
        }</span>
        <span class="fact-tier-btns">${upBtn}${downBtn}</span>
        <button class="ghost tiny" data-fact-del="${escAttr(it.id)}" data-fact-key="${escAttr(row.key)}"
          title="删掉这条记忆（不可恢复）">删</button>
      </li>`;
    })
    .join("");
  // 个人画像：一段连续的「这个人是谁」，和下面的事实明细分工不同——
  // 画像答「他是谁」，事实答「他最近在干嘛」。字数上限按好感档位给
  const prof = row.profile || {};
  const profQuota = Number(prof.quota || 0);
  const profStageQuota = Number(prof.stage_quota || profQuota || 0);
  const profBasis = Number(prof.basis || 0);
  const profUpd = prof.updated_at ? ` · ${fmtTime(prof.updated_at)} 更新` : "";
  const profileBox =
    profQuota > 0
      ? `<div class="profile-box">
      <div class="profile-head">
        <b>🧬 画像</b>
        <label class="profile-quota" title="改这个 QQ 的画像字数上限；填 0 恢复档位默认 ${profStageQuota} 字">
          上限
          <input type="number" data-prof-chars="${escAttr(row.key)}" value="${profQuota}"
            min="0" max="800" step="10" />
          字
        </label>
        <span class="hint">${profBasis ? `基于 ${profBasis} 条事实` : ""}${
          prof.chars ? ` · 已手调（档位默认 ${profStageQuota}）` : ` · 档位默认 ${profStageQuota}`
        }${profUpd}</span>
        <button class="ghost tiny" data-prof-rewrite="${escAttr(row.key)}"
          title="让模型重写这段画像。会发一次请求（约 800 token），每天有次数上限">重新生成</button>
      </div>
      <p class="profile-text">${
        prof.text ? esc(prof.text) : '<span class="hint">还没有画像——事实攒到 3 条会自动生成</span>'
      }</p>
      <details class="profile-edit">
        <summary>人工补充${prof.manual ? "（已填）" : ""}</summary>
        <textarea data-prof-manual="${escAttr(row.key)}" rows="2"
          placeholder="例如：他是我室友，别调侃他学历">${esc(prof.manual || "")}</textarea>
        <button class="ghost tiny" data-prof-save="${escAttr(row.key)}">保存</button>
      </details>
    </div>`
      : "";

  // 人级别的「置顶」放在标题栏：控制的是这个人在列表里的位置，
  // 和里面每条记忆的档位、钉住是两回事
  const pinUserBtn = `<button type="button" class="ghost tiny${row.pinned ? " pinned" : ""}"
    data-fact-pin-user="${escAttr(row.key)}" data-fact-pinned="${row.pinned ? "0" : "1"}"
    title="${row.pinned ? "取消置顶" : "置顶这个人，排在事实列表最前"}">${row.pinned ? "已置顶" : "置顶"}</button>`;

  return `<details class="session">
    <summary>
      <b>${row.pinned ? "📌 " : ""}${esc(who)}</b>
      <span>${where}${nickNote}${stage}${coreNote} · ${alive}${cap} 条（生效 ${row.active} · 待观察 ${row.pending} · 疑似记错 ${row.suspect}${archived}${rejected}）${keepNote}</span>
      ${fullTag}
      ${pinUserBtn}
    </summary>
    <div class="body">
      ${profileBox}
      <ul class="fact-list">${rows}</ul>
      <div class="fact-actions">${pruneBtn}${clearBtn}</div>
    </div>
  </details>`;
}

// 重新生成画像：会发一次请求，所以先确认一句
async function rewriteProfile(key) {
  const ok = await askConfirm(
    "让模型重写这个人的画像？\n\n" +
      "会把现在记住的事实重新提炼成一段描述，约 800 token（一次不到一分钱）。\n" +
      "人工补充不会被覆盖。每天有重写次数上限。",
  );
  if (!ok) return;
  const res = unpack(await Bridge.apiPost("facts", { action: "rewrite_profile", key }));
  showToast(res.message || (res.ok ? "已重新生成" : "生成失败"));
  if (res.ok) await loadFacts();
}

// 给单个 QQ 单独设画像字数上限。填 0 = 回到档位默认
async function saveProfileChars(key, chars) {
  const res = unpack(
    await Bridge.apiPost("facts", { action: "set_profile_chars", key, chars }),
  );
  showToast(res.message || (res.ok ? "已更新" : "更新失败"));
  if (res.ok) await loadFacts();
}

// 人物级别的置顶：只影响列表顺序
async function pinFactUser(key, pinned) {
  const res = unpack(
    await Bridge.apiPost("facts", { action: "pin_user", key, pinned }),
  );
  showToast(res.message || (res.ok ? "已更新" : "更新失败"));
  if (res.ok) await loadFacts();
}

// 保存人工补充段。手工写的比模型总结的可靠，注入时排在最前
async function saveProfile(key, text) {
  const res = unpack(
    await Bridge.apiPost("facts", { action: "save_profile", key, manual: text }),
  );
  showToast(res.message || (res.ok ? "已保存" : "保存失败"));
  if (res.ok) await loadFacts();
}

async function deleteFact(key, id) {
  const ok = await askConfirm("删掉这条记忆？删了就真的不记得了，无法恢复。");
  if (!ok) return;
  const payload = unwrap(await Bridge.apiPost("facts", { action: "delete", key, id }));
  if (payload && payload.success === false) {
    showToast(payload.message || "删除失败");
    return;
  }
  showToast(payload.message || "已删除");
  await loadFacts();
}

// 同时要 data 和 message：data 用来显示「清掉几条」，message 是给人看的一句话。
// unwrap 只返回 data，成功时的文案就拿不到了。
function unpack(payload) {
  if (payload && typeof payload === "object" && "success" in payload) {
    return {
      ok: payload.success !== false,
      message: payload.message || "",
      data: payload.data || {},
    };
  }
  return { ok: true, message: "", data: payload || {} };
}

// 手动升降记忆档位。``dir``：1 升一级 / -1 降一级 / 0 交回自动判定。
// 可逆操作，不做二次确认——点错了再点回来就行，为它弹一次确认框反而烦。
async function setFactTier(key, id, dir) {
  try {
    const res = unpack(
      await Bridge.apiPost("facts", { action: "set_tier", key, id, dir }),
    );
    showToast(res.message || (res.ok ? "已更新" : "更新失败"));
    if (res.ok) await loadFacts();
  } catch (err) {
    console.error("改记忆档位失败:", err);
    showToast("操作失败，请查看日志");
  }
}

// 整理合并：把「同一件事的不同说法」并成一条。
// 先 dry_run 问一句「会合几条」再执行——这个动作会改动整库，
// 不能点了就跑。纯本地比对，不花 token。
async function mergeFacts() {
  try {
    const prev = unpack(await Bridge.apiPost("facts", { action: "merge", dry_run: true }));
    if (!prev.ok) {
      showToast(prev.message || "预览失败");
      return;
    }
    const d = prev.data || {};
    if (!d.merged) {
      showToast("没有发现可以合并的重复记忆");
      return;
    }
    const ok = await askConfirm(
      `整理合并：涉及 ${d.people} 人，共 ${d.merged} 组重复\n\n` +
        `每组只留最熟的那条（提到次数多的），其余并进它的「别的说法」里，` +
        `标成「已归档」留档，180 天后才真删。\n\n` +
        `熟练度、重要性、最近提到时间都会合并过去，不会因为合并而变生。`,
    );
    if (!ok) return;
    const res = unpack(await Bridge.apiPost("facts", { action: "merge" }));
    showToast(res.message || "已合并");
    if (res.ok) await loadFacts();
  } catch (err) {
    console.error("整理合并失败:", err);
    showToast("操作失败，请查看日志");
  }
}

// 单人按档位瘦身。**先预览再确认**：这会改动数据，
// 不能点了就直接执行——用户得先看到「10 → 5 条」这个数字。
async function pruneFact(key) {
  try {
    const prev = unpack(
      await Bridge.apiPost("facts", { action: "prune", key, dry_run: true }),
    );
    if (!prev.ok) {
      showToast(prev.message || "预览失败");
      return;
    }
    const d = prev.data || {};
    if (!d.removed) {
      showToast(`这个人现在 ${d.before ?? 0} 条，没超过 ${d.target ?? 0} 条的目标，不用清`);
      return;
    }
    const ok = await askConfirm(
      `按档位瘦身：${d.before} → ${d.after} 条（保留 ${d.target} 条）\n\n` +
        `会清掉 ${d.removed} 条。清掉的不删除，标成「已否决」留档，` +
        `在「状态」列勾「已否决」还能看到。\n\n` +
        `保留顺序：钉住的、亲口说过的边界永远留；其余按重要性和最近提到的排。`,
    );
    if (!ok) return;
    const res = unpack(await Bridge.apiPost("facts", { action: "prune", key }));
    showToast(res.message || "已清理");
    await loadFacts();
  } catch (err) {
    console.error("清理失败:", err);
    showToast("清理失败，请查看日志");
  }
}

// 一键清理：所有非主人各自按档位瘦身。主人跳过——
// 「养得越久记得越多」是这套玩法的核心，主人这条线不该被清。
async function pruneAllFacts() {
  try {
    const prev = unpack(
      await Bridge.apiPost("facts", { action: "prune_all", dry_run: true }),
    );
    if (!prev.ok) {
      showToast(prev.message || "预览失败");
      return;
    }
    const d = prev.data || {};
    if (!d.removed && !d.purged) {
      showToast(
        `没有需要清理的（${d.skipped_owner || 0} 个主人已跳过，其余都没超过档位目标）`,
      );
      return;
    }
    // 按档位列出会动几个人，让用户点确认前心里有数
    const rows = (d.rows || []).slice(0, 6).map((r) => `  ${r.key}：${r.before} → ${r.after} 条`);
    const more = (d.rows || []).length > 6 ? `\n  ……还有 ${d.rows.length - 6} 人` : "";
    const ok = await askConfirm(
      `一键清理会整理 ${d.people} 个人的记忆，清掉 ${d.removed} 条` +
        (d.purged ? `，另删 ${d.purged} 条超期归档` : "") +
        `。\n\n按好感档位保留：不待见 2 / 陌生 5 / 熟悉 12 / 亲近 20 / 老友 30 条。\n` +
        `${d.skipped_owner || 0} 个主人不受影响。\n\n` +
        `${rows.join("\n")}${more}\n\n` +
        `清掉的不删除，标「已否决」留档，随时能筛出来看。`,
    );
    if (!ok) return;
    const res = unpack(await Bridge.apiPost("facts", { action: "prune_all" }));
    showToast(res.message || "已清理");
    await loadFacts();
  } catch (err) {
    console.error("一键清理失败:", err);
    showToast("一键清理失败，请查看日志");
  }
}

// 主人单独清理。和一键清理**刻意分开**：
// 一键清理是日常维护（跳过主人），这个是「主人这份也太长了，我主动收一收」。
async function pruneOwnerFacts() {
  try {
    const prev = unpack(
      await Bridge.apiPost("facts", { action: "prune_owner", dry_run: true }),
    );
    if (!prev.ok) {
      showToast(prev.message || "预览失败");
      return;
    }
    const d = prev.data || {};
    if (!d.removed) {
      showToast(prev.message || "主人现在没超过保留量，不用清");
      return;
    }
    const first = (d.rows || [])[0] || {};
    const ok = await askConfirm(
      `清理主人记忆：${first.before ?? 0} → ${first.after ?? 0} 条` +
        `（保留 ${d.keep} 条，清掉 ${d.removed} 条）\n\n` +
        `主人**不参与一键清理**，这个是单独给你的入口。\n` +
        `保留顺序和普通人一样：钉住的、亲口说过的边界先留，其余按重要性和最近提到的排。\n` +
        `清掉的不删除，标「已否决」留档，随时能筛出来看。`,
    );
    if (!ok) return;
    const res = unpack(await Bridge.apiPost("facts", { action: "prune_owner" }));
    showToast(res.message || "已清理");
    await loadFacts();
  } catch (err) {
    console.error("清理主人失败:", err);
    showToast("清理主人失败，请查看日志");
  }
}

// 清空一个人的全部记忆。真删，不可恢复——和上面的「瘦身」不是一回事，
// 所以文案里要写清楚，别让用户以为也能从「已否决」里找回来。
async function clearFacts(key) {
  const ok = await askConfirm(
    "清空这个人的全部记忆？\n\n这是**真删**，不是瘦身——删了就找不回来了。" +
      "如果只是想省 token，用「按档位瘦身」就够了。",
  );
  if (!ok) return;
  const res = unpack(await Bridge.apiPost("facts", { action: "clear", key }));
  showToast(res.message || "已清空");
  await loadFacts();
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

Bridge.ready()
  .then(() => {
  // 列头能点这件事没人猜得到。统一补一句悬停提示，省得每个按钮写一遍；
  // 已经自己写了 title 的不覆盖
  document.querySelectorAll(".gf-head").forEach((el) => {
    if (el.title) return;
    const name = (el.textContent || "").trim();
    el.title = `${name}：点开按这一列筛选和排序，再点一次收起`;
  });
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

  // 关系面板：刷新、搜索、逐条操作（事件委托，列表是动态渲染的）
  if ($("bondRefresh")) $("bondRefresh").addEventListener("click", loadBond);
  // 三个列表的列头筛选（关系 / 事实 / 学习库）走同一套初始化和交互，
  // 每个只传自己的配置——所以要改「点列头怎么展开」「重置怎么清」，
  // 只改 gfInitBar 一处，不会出现三个面板行为不一样
  gfInitBar(BOND_GF);
  gfInitBar(FACT_GF);
  gfInitBar(TRAIT_GF);
  gfInitBar(ACTIVE_GF);
  if ($("bondList")) {
    $("bondList").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-bond]");
      if (!btn) return;
      const action = btn.dataset.bond;
      // 置顶按钮挂在 summary 里，不拦的话点一下会把整块收起来
      if (action === "pin" || action === "unpin") {
        e.preventDefault();
        e.stopPropagation();
      }
      // 手动改亲密度 / 称呼都要读同一行里的输入框，值不在按钮上
      let value;
      if (action === "set_intimacy" || action === "set_nickname") {
        const box = btn.closest(".bond-adjust");
        const input = box ? box.querySelector("input") : null;
        value = input ? input.value : "";
      }
      bondAction(action, btn.dataset.key, value);
    });
  }
  if ($("settingSearch")) {
    // 参数项是纯本地渲染，搜索不必请求接口，直接拿上次的快照重排
    $("settingSearch").addEventListener("input", (e) => {
      settingQuery = e.target.value || "";
      if (lastSettingsData) renderSettings(lastSettingsData);
    });
  }
  if ($("factsRefresh")) $("factsRefresh").addEventListener("click", loadFacts);
  if ($("factsList")) {
    // 画像字数上限：输入框失焦或回车才提交，避免每敲一位就发一次请求
    $("factsList").addEventListener("change", (e) => {
      const input = e.target.closest("input[data-prof-chars]");
      if (!input) return;
      const key = input.dataset.profChars;
      const value = Number(input.value || 0);
      if (!Number.isFinite(value) || value < 0) {
        showToast("字数要填 0 或正整数");
        return;
      }
      saveProfileChars(key, Math.min(800, Math.round(value)));
    });
  }
  if ($("factPruneAll")) $("factPruneAll").addEventListener("click", pruneAllFacts);
  if ($("factPruneOwner")) $("factPruneOwner").addEventListener("click", pruneOwnerFacts);
  if ($("factMerge")) $("factMerge").addEventListener("click", mergeFacts);
  if ($("factsList")) {
    // 四种按钮共用一个委托：删一条、按档位瘦身、清空这个人、升降档位。
    // 分开写要挂四次监听，而且每加一个按钮就得再改一次这里。
    $("factsList").addEventListener("click", (e) => {
      const del = e.target.closest("button[data-fact-del]");
      const prune = e.target.closest("button[data-fact-prune]");
      const clear = e.target.closest("button[data-fact-clear]");
      const tier = e.target.closest("button[data-fact-tier]");
      // 档位徽标本身就是「交回自动」按钮
      const auto = e.target.closest("[data-fact-auto]");
      const rewrite = e.target.closest("button[data-prof-rewrite]");
      const saveProf = e.target.closest("button[data-prof-save]");
      const pin = e.target.closest("button[data-fact-pin-user]");
      const btn = del || prune || clear || tier || auto || rewrite || saveProf || pin;
      if (!btn) return;
      // 按钮在 details 里，不拦的话点一下会把整块收起来
      e.preventDefault();
      e.stopPropagation();
      if (del) deleteFact(del.dataset.factKey, del.dataset.factDel);
      else if (prune) pruneFact(prune.dataset.factPrune);
      else if (tier) {
        setFactTier(tier.dataset.factKey, tier.dataset.factTier, Number(tier.dataset.factDir || 1));
      } else if (auto) {
        setFactTier(auto.dataset.factKey, auto.dataset.factAuto, 0);
      } else if (pin) {
        pinFactUser(pin.dataset.factPinUser, pin.dataset.factPinned === "1");
      } else if (rewrite) {
        rewriteProfile(rewrite.dataset.profRewrite);
      } else if (saveProf) {
        const box = saveProf.closest(".profile-edit");
        const area = box && box.querySelector("textarea");
        saveProfile(saveProf.dataset.profSave, area ? area.value : "");
      } else clearFacts(clear.dataset.factClear);
    });
  }
  if ($("toneBtn")) $("toneBtn").addEventListener("click", runTone);
  if ($("toneInput")) {
    // Ctrl/Cmd + 回车直接跑，不用去够按钮
    $("toneInput").addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
        e.preventDefault();
        runTone();
      }
    });
  }
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
  $("feedRefresh").addEventListener("click", async () => {
    await loadFeed();
    showToast("已刷新投喂数据");
  });
  $("activeRefresh").addEventListener("click", async () => {
    await loadActive();
    showToast("已刷新主动回复数据");
  });
  $("feedSearch").addEventListener("input", (e) => {
    feedQuery.text = e.target.value || "";
    renderFeedBoard();
  });
  $("confirmOk").addEventListener("click", () => closeConfirm(true));
  $("confirmCancel").addEventListener("click", () => closeConfirm(false));
  $("confirmModal").addEventListener("click", (e) => {
    if (e.target === $("confirmModal")) closeConfirm(false);
  });

  // 设置项：滑块拖动时实时更新显示与联动文案，松手（change）才落库。
  // 喂食页的参数也是同一套控件，共用这两个处理函数——所以那边不用再写一遍
  const onSettingInput = (e) => {
    const el = e.target;
    if (!el.dataset || !el.dataset.key) return;
    if (el.type === "range") updateSlider(el);
    if (STYLE_KEYS.includes(el.dataset.key)) updateStyleNote();
  };
  $("settings").addEventListener("input", onSettingInput);
  $("feedSettings").addEventListener("input", onSettingInput);
  $("activeSettings").addEventListener("input", onSettingInput);

  // 各组的一键预设：点一下整套参数一起写，不用逐个调。
  // 参数页和主动回复页都有预设条，绑同一个处理函数
  const onProfileClick = async (e) => {
    const btn = e.target.closest("button[data-profile]");
    if (!btn) return;
    e.preventDefault();
    const id = btn.dataset.profile;
    const group = btn.dataset.profileGroup || "active";
    btn.disabled = true;
    try {
      const res = unpack(
        await Bridge.apiPost("manage", { action: "apply_profile", group, profile: id }),
      );
      showToast(res.message || (res.ok ? "已切换" : "切换失败"));
      if (res.ok) await load();
    } catch (err) {
      console.error("切换预设失败:", err);
      showToast("切换失败，请查看日志");
    } finally {
      btn.disabled = false;
    }
  };
  $("settings").addEventListener("click", onProfileClick);
  $("activeSettings").addEventListener("click", onProfileClick);

  const onSettingChange = async (e) => {
    const el = e.target;
    // 按群开关不是配置项，是会话级状态，走另一个接口
    if (el.dataset && el.dataset.activeGroup) {
      await toggleActiveGroup(el.dataset.activeGroup, !!el.checked);
      return;
    }
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
  };
  $("settings").addEventListener("change", onSettingChange);
  $("feedSettings").addEventListener("change", onSettingChange);
  $("activeSettings").addEventListener("change", onSettingChange);

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
    if (act === "drop_session") {
      // 按钮在 <summary> 里，不拦默认行为会把整张卡折叠一下
      e.preventDefault();
      await dropTraitGroup(sid, btn.dataset.name || sid, false, true);
      return;
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
      const drop = e.target.closest("[data-drop]");
      if (drop) {
        e.preventDefault();
        e.stopPropagation();
        await dropTraitGroup(drop.dataset.drop, drop.dataset.name, drop.dataset.pinned === "1");
        return;
      }
      const delItem = e.target.closest("[data-traitdel]");
      if (delItem) {
        e.preventDefault();
        e.stopPropagation();
        const item = traitItemOf(delItem.dataset.sid, delItem.dataset.traitdel);
        await deleteTraitItem(
          delItem.dataset.sid,
          delItem.dataset.traitdel,
          !!(item && item.pinned),
        );
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

  // 学习库：群聊 / 私聊两条子库。切换只重渲染，不重新请求——
  // 数据是一次拿全的，切一下没必要再跑一趟接口
  if ($("traitPurge")) $("traitPurge").addEventListener("click", purgeInactiveTraits);

  const traitScopeBox = $("traitScope");
  if (traitScopeBox) {
    traitScopeBox.addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-scope]");
      if (!btn) return;
      traitScope = btn.dataset.scope === "private" ? "private" : "group";
      renderTraits(traitGroups);
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
  })
  .catch((err) => {
    // 宿主桥接没起来 / 初始化抛错：把原因写在页面顶部，而不是留一片空白
    if (window.__panelDiag) {
      window.__panelDiag("面板初始化失败：" + ((err && err.message) || err));
    }
    console.error("面板初始化失败:", err);
  });
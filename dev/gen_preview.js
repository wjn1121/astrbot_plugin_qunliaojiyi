// 生成一份「参数面板 + 学习库 + 事实记忆」的离线预览页，用 Chrome 截图看排版。
// 用法：node gen_preview.js  → 输出 preview.html
const fs = require("fs");
const path = require("path");
// 生产文件在 pages/panel/，本脚本在 dev/，只读不写那边
const PAGES = path.join(__dirname, "..", "pages", "panel");

const src = fs.readFileSync(path.join(PAGES, "app.js"), "utf8");
const css = fs.readFileSync(path.join(PAGES, "style.css"), "utf8");

const el = () => ({
  style: {},
  classList: { add() {}, remove() {}, contains: () => false },
  appendChild() {},
  addEventListener() {},
  setAttribute() {},
  querySelector: () => null,
  querySelectorAll: () => [],
  dataset: {},
  hidden: false,
});
const escEl = () => {
  const o = el();
  let txt = "";
  Object.defineProperty(o, "textContent", { get: () => txt, set: (v) => { txt = v == null ? "" : String(v); } });
  Object.defineProperty(o, "innerHTML", {
    get: () => txt.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"),
    set: (v) => { txt = String(v == null ? "" : v); },
  });
  return o;
};
global.window = {
  addEventListener() {},
  __panelDiag: null,
  location: { href: "" },
  localStorage: { getItem: () => null, setItem() {} },
  AstrBotPluginView: { apiGet: async () => ({}), apiPost: async () => ({}), ready: async () => {} },
};
global.document = {
  getElementById: () => null,
  createElement: escEl,
  body: el(),
  addEventListener() {},
  querySelector: () => null,
  querySelectorAll: () => [],
};
global.navigator = {};
global.location = global.window.location;
global.localStorage = global.window.localStorage;

const api = new Function(
  "window", "document", "navigator", "location", "localStorage",
  src + "\nreturn {groupCard, factBucketCard, settingRow, SETTING_GROUPS, esc};",
)(global.window, global.document, global.navigator, global.location, global.localStorage);

// ── 参数面板：照 renderSettings 的结构拼 ──
// 「强度预设」条：真实渲染要读 lastStatus，这里为了看样式手写一段同构 HTML
const PROFILE_BAR = `<div class="profile-bar">
  <span class="profile-label">强度预设</span>
  <div class="seg" id="activeProfileSeg">
    <button type="button">🛡 安静</button>
    <button type="button" class="on">⚖️ 平衡</button>
    <button type="button">🔥 活跃</button>
  </div>
  <span class="hint">半小时一次，每天 8 次。默认值，像群里一个不太话多的人</span>
</div>`;

const groups = api.SETTING_GROUPS.map((g) => {
  const rows = g.items.map((d) => api.settingRow(d, {}, {})).join("");
  const head = g.id === "active" ? PROFILE_BAR : "";
  return `<section class="set-group"><h3>${api.esc(g.title)}<small></small></h3>
    ${head}<div class="set-grid">${rows}</div></section>`;
}).join("");

// ── 学习库：一未置顶、一置顶 ──
const trait = (t) => `<div class="trait-row"><div class="trait-head">
  <span class="trait-status st-active">已生效</span><span class="trait-situation">任何场合都成立</span>
  <span class="trait-weight">权重 60<small>有效 42</small></span></div>
  <div class="trait-style">${api.esc(t)}</div></div>`;
const gBase = {
  session_id: "bot1:GroupMessage:1038882918",
  display_name: "1038882918",
  total: 32,
  learn_enabled: true,
  counts: { pending: 1 },
  inactive_days: 40,
  traits: ["短句连发，一句一气泡", "爱用「绷不住了」"],
};
const lib = [
  api.groupCard(gBase),
  api.groupCard({ ...gBase, session_id: "bot1:GroupMessage:816855252", display_name: "816855252", pinned: true, counts: { pending: 0 } }),
].join("");

// ── 事实记忆：三档各一条 ──
const factRow = {
  key: "759982326", sender_id: "759982326", session: "", session_label: "—",
  is_owner: true, stage: "intimate", stage_label: "老友", limit: 300,
  alive: 12, total: 12, active: 12, pending: 0, suspect: 0, archived: 0, rejected: 0,
  keep_target: 150, core_quota: 15, core_count: 2, deep_count: 1,
  profile: {
    text: "资深手游玩家，碧蓝航线 150 多级；最近在给群里的 bot 加功能，工作忙很少上线。",
    manual: "是我室友，别调侃学历",
    quota: 260,
    basis: 12,
    updated_at: 1762000000,
  },
  items: [
    { id: "f_0001", text: "在准备考研，已经复习三个月了", kind: "事件", status: "已生效", status_key: "active", active: true, tier: "core", tier_label: "核心", prof: 78.2, tier_manual: false, keep: 1, seen: 3, variants: ["复习了三个月"] },
    { id: "f_0002", text: "高中时就想买英伟达，但当时没有财权", kind: "事实", status: "已生效", status_key: "active", active: true, tier: "deep", tier_label: "深层", prof: 55.1, tier_manual: true, keep: 0.9, seen: 1, variants: [] },
    { id: "f_0003", text: "自述潮批抽卡不出货的概率不到一半", kind: "事实", status: "已生效", status_key: "active", active: true, tier: "shallow", tier_label: "浅层", prof: 40.7, tier_manual: false, keep: 0.55, seen: 1, variants: [] },
  ],
};
const facts = api.factBucketCard(factRow).replace("<details class=\"session\">", "<details class=\"session\" open>");

const html = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>preview</title>
<style>${css}</style></head><body>
<div class="shell" style="display:block"><main class="content">
<section class="pane on"><section class="card">
  <h2 class="card-title"><span>⚙️ 运行参数 <small>（改动即时生效）</small></span></h2>
  <div id="settings" class="settings">${groups}</div>
</section></section>
<section class="pane on"><section class="card">
  <h2 class="card-title"><span>📚 学习库</span></h2>
  <div id="traits" class="traits">${lib}</div>
</section></section>
<section class="pane on"><section class="card">
  <h2 class="card-title"><span>🧠 事实记忆</span></h2>
  <div id="factsList">${facts}</div>
</section></section>
</main></div></body></html>`;

fs.writeFileSync(path.join(__dirname, "preview.html"), html);

// 另外单独出两份：只看学习库 / 只看事实记忆，截图才看得清细节
const wrap = (body) => `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<style>${css}</style></head><body><main class="content" style="padding:16px">${body}</main></body></html>`;
fs.writeFileSync(
  path.join(__dirname, "preview_lib.html"),
  wrap(`<section class="card"><h2 class="card-title"><span>📚 学习库</span></h2>
    <p class="hint">点「证据」能看到这条是从哪句话学来的。分组默认收起，点标题展开；置顶的没有删除按钮。</p>
    <div id="traits" class="traits">${lib}</div></section>`),
);
fs.writeFileSync(
  path.join(__dirname, "preview_fact.html"),
  wrap(`<section class="card"><h2 class="card-title"><span>🧠 事实记忆</span></h2>
    <div id="factsList">${facts}</div></section>`),
);
const twoGroups = api.SETTING_GROUPS
  .filter((g) => g.id === "facts" || g.id === "active")
  .map((g) => {
    const rows = g.items.map((d) => api.settingRow(d, {}, {})).join("");
    const head = g.id === "active" ? PROFILE_BAR : "";
    return `<section class="set-group"><h3>${api.esc(g.title)}<small></small></h3>
      ${head}<div class="set-grid">${rows}</div></section>`;
  })
  .join("");
fs.writeFileSync(
  path.join(__dirname, "preview_active.html"),
  wrap(`<div id="settings" class="settings">${twoGroups}</div>`),
);
console.log("已生成 preview.html / preview_lib.html / preview_fact.html / preview_active.html");

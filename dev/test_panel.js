// 面板渲染冒烟测试：不依赖浏览器，只验证几个纯字符串渲染函数
// 用法：node test_panel.js
const fs = require("fs");
const path = require("path");
// 生产文件在 pages/panel/，本脚本在 dev/，只读不写那边
const PAGES = path.join(__dirname, "..", "pages", "panel");

const src = fs.readFileSync(path.join(PAGES, "app.js"), "utf8");

// 最小 DOM stub —— app.js 顶层会挂 error 监听、末尾会跑 init()，
// 这里只要求它别抛，不要求它真的渲染
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
global.window = {
  addEventListener() {},
  __panelDiag: null,
  location: { href: "" },
  localStorage: { getItem: () => null, setItem() {} },
};
// esc() 靠 div.textContent → div.innerHTML 联动做转义，stub 必须实现这一层
const escEl = () => {
  const o = el();
  let txt = "";
  Object.defineProperty(o, "textContent", {
    get: () => txt,
    set: (v) => { txt = v == null ? "" : String(v); },
  });
  Object.defineProperty(o, "innerHTML", {
    get: () => txt.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"),
    set: (v) => { txt = String(v == null ? "" : v); },
  });
  return o;
};

global.document = {
  getElementById: () => null,
  createElement: escEl,
  body: el(),
  addEventListener() {},
  querySelector: () => null,
  querySelectorAll: () => [],
};
global.window.AstrBotPluginView = {
  apiGet: async () => ({}),
  apiPost: async () => ({}),
  apiPut: async () => ({}),
  ready: async () => {},
  onReady: () => {},
  getContext: () => ({}),
};
global.navigator = {};
global.location = global.window.location;
global.localStorage = global.window.localStorage;

const f = new Function(
  "window",
  "document",
  "navigator",
  "location",
  "localStorage",
  src +
    "\nreturn {groupCard, sessionCard, factBucketCard, settingRow, tokenTag, fmtDefault, TIER_TEXT, profileMatches};",
);

let api;
try {
  api = f(global.window, global.document, global.navigator, global.location, global.localStorage);
} catch (e) {
  console.error("加载 app.js 失败：", e.message);
  process.exit(1);
}

let ok = true;
const chk = (name, cond, extra = "") => {
  console.log((cond ? "PASS " : "FAIL ") + name, extra);
  ok = ok && cond;
};

// ── 学习库分组卡：置顶时不该有删除按钮 ──
const base = {
  session_id: "bot1:GroupMessage:123",
  display_name: "测试群",
  total: 3,
  pinned: false,
  learn_enabled: true,
  counts: { pending: 1 },
  traits: [],
  inactive_days: 40,
};
const normalCard = api.groupCard({ ...base });
const pinnedCard = api.groupCard({ ...base, pinned: true });
chk("未置顶有删除按钮", normalCard.includes('data-drop="bot1:GroupMessage:123"'));
chk("已置顶没有删除按钮", !pinnedCard.includes("data-drop="));
chk("已置顶仍有置顶按钮", pinnedCard.includes("已置顶"));
chk("默认折叠（无 open）", !normalCard.includes("<details class=\"trait-group\" data-gsid=\"bot1:GroupMessage:123\" open"));

// ── 会话卡：有删除按钮，置顶时隐藏 ──
const sess = {
  session_id: "bot1:GroupMessage:123",
  display_name: "测试群",
  pinned: false,
  stable: [],
  situational: [],
  manual: [],
  history: [],
  limits: {},
  learn_state: {},
};
const s1 = api.sessionCard({ ...sess });
const s2 = api.sessionCard({ ...sess, pinned: true });
chk("会话卡有删除按钮", s1.includes('data-act="drop_session"'));
chk("会话置顶后删除按钮消失", !s2.includes('data-act="drop_session"'));

// ── 事实条目：三档徽标 + 熟练度 + 升降按钮 ──
const row = {
  key: "123",
  sender_id: "123",
  session: "",
  session_label: "—",
  is_owner: false,
  stage: "stranger",
  stage_label: "陌生",
  limit: 20,
  alive: 2,
  total: 2,
  active: 2,
  pending: 0,
  suspect: 0,
  archived: 0,
  rejected: 0,
  keep_target: 5,
  core_quota: 1,
  core_count: 1,
  deep_count: 1,
  profile: {
    text: "资深手游玩家，碧蓝航线 150 多级",
    manual: "别调侃学历",
    quota: 180,
    stage_quota: 260,
    basis: 5,
    updated_at: 1762000000,
  },
  items: [
    { id: "f_0001", text: "测试记忆", kind: "事实", status: "已生效", status_key: "active", active: true, tier: "core", tier_label: "核心", prof: 78.5, tier_manual: false, keep: 1, seen: 3, variants: ["另一种说法"], pinned: true },
    { id: "f_0002", text: "第二条", kind: "事实", status: "已生效", status_key: "active", active: true, tier: "shallow", tier_label: "浅层", prof: 40, tier_manual: true, keep: 0.5, seen: 1, variants: [] },
  ],
};
const card = api.factBucketCard(row);
chk("三档徽标渲染", card.includes("fact-tier core") && card.includes("fact-tier shallow"));
chk("熟练度数字显示", card.includes("核心 <b>79</b>") && card.includes("浅层 <b>40</b>"));
chk("有升/降按钮", card.includes('data-fact-dir="1"') && card.includes('data-fact-dir="-1"'));
chk("核心不能再升（disabled）", /data-fact-dir="1"[\s\S]{0,200}?disabled/.test(card));
chk("浅层不能再降（disabled）", /data-fact-dir="-1"[\s\S]{0,120}?disabled/.test(card));
chk("徽标可点回自动", card.includes("data-fact-auto="));
chk("变体提示", card.includes("还有 1 种说法"));
chk("卡片头标深度数量", card.includes("核心 1/1") && card.includes("深层 1"));
chk("画像块渲染", card.includes("profile-box") && card.includes("🧬 画像"));
chk(
  "画像字数上限可编辑",
  card.includes('data-prof-chars="123"') && card.includes('value="180"') && card.includes("档位默认"),
);
chk("画像显示依据条数", card.includes("基于 5 条事实"));
chk("画像人工段可编辑", card.includes("data-prof-manual=") && card.includes("别调侃学历"));
chk("画像有重新生成按钮", card.includes("data-prof-rewrite="));
chk("人物标题栏有置顶按钮", card.includes("data-fact-pin-user=") && card.includes(">置顶<"));
chk("条目级不再有钉住按钮", !card.includes('data-fact-pin="'));

// ── 主动回复预设：整组匹配才算命中 ──
const preset = {
  id: "balanced",
  values: { active_reply_chance: 0.15, active_reply_quiet_hours: "01:00-08:00" },
};
chk(
  "预设匹配：参数一致",
  api.profileMatches(preset, { active_reply_chance: 0.15, active_reply_quiet_hours: "01:00-08:00" }),
);
chk(
  "预设匹配：改过一个就不算",
  !api.profileMatches(preset, { active_reply_chance: 0.35, active_reply_quiet_hours: "01:00-08:00" }),
);

// ── 参数行：默认值徽标 + token 标签 + 灰字说明 ──
const rowHtml = api.settingRow(
  {
    key: "max_inject",
    label: "每轮最多注入几条",
    type: "number",
    default: 5,
    hint: "太多了会抢注意力。影响：token ↑↑",
  },
  {},
  {},
);
chk("参数行带默认值徽标", rowHtml.includes("set-def") && rowHtml.includes("默认 5"));
chk("参数行带 token 标签", rowHtml.includes("set-tok") && rowHtml.includes("↑↑"));
chk("参数行说明是 hint", rowHtml.includes('class="set-hint"'));

console.log("\n" + (ok ? "全部通过" : "有失败项"));
process.exit(ok ? 0 : 1);

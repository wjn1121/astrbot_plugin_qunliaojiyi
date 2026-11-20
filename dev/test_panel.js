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
    "\nreturn {groupCard, sessionCard, factBucketCard, settingRow, tokenTag, fmtDefault, TIER_TEXT, profileMatches," +
    " renderFeedBoard, renderUsageSource, SETTING_GROUPS, profileBar, renderActiveList, renderActiveStats, ACTIVE_GF," +
    " sortFacts," +
    " setFeedBoard: (v) => { feedBoard = v; }," +
    " setActiveGroups: (v, on, perUser) => { activeGroups = v; activeGlobalOn = on; activePerUserLimit = perUser || 0; }," +
    " setStatus: (v) => { lastStatus = v; }};",
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

// ── 事实卡标题：昵称在前、QQ 在后 ──
const namedCard = api.factBucketCard({ ...row, sender_name: "小明", nickname: "明明" });
chk("标题是「昵称（QQ）」", namedCard.includes("小明（123）"));
chk("标出你手动设的称呼", namedCard.includes("你叫他「明明」"));
chk(
  "没昵称时退回只显示 QQ",
  api.factBucketCard({ ...row, sender_name: "", nickname: "" }).includes("<b>123</b>"),
);

// ── 事实列表排序：主人 → 置顶 → 其余 ──
const factSorted = api.sortFacts([
  { key: "a", total: 5, is_owner: false, pinned: false },
  { key: "b", total: 3, is_owner: true, pinned: false },
  { key: "c", total: 9, is_owner: false, pinned: true },
]);
chk(
  "主人第一、置顶第二、其余按条数",
  factSorted.map((r) => r.key).join(",") === "b,c,a",
  factSorted.map((r) => r.key).join(","),
);

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

// ── 参数页：喂食那组不能出现在这里（它有自己的页）──
const settingsGroups = api.SETTING_GROUPS.filter((g) => !g.pane);
chk("参数页不含喂食组", settingsGroups.every((g) => g.id !== "feed"));
chk(
  "喂食组挂在自己的 pane 上",
  api.SETTING_GROUPS.some((g) => g.id === "feed" && g.pane === "feed"),
);

// ── 参数分组顺序：靠 order 排，不靠定义顺序 ──
const ordered = settingsGroups.slice().sort((a, b) => (a.order ?? 99) - (b.order ?? 99));
chk("第一组是全局", ordered[0].id === "global");
chk("主动回复排在运行与学习之前", ordered.findIndex((g) => g.id === "active") < ordered.findIndex((g) => g.id === "base"));
chk("计价永远在最后", ordered[ordered.length - 1].id === "price");
chk("每组都有 order", settingsGroups.every((g) => typeof g.order === "number"));
chk("order 不重复", new Set(settingsGroups.map((g) => g.order)).size === settingsGroups.length);

// ── 三组新预设：和后端 GROUP_PROFILES 的键对上 ──
// 用全量分组：主动回复有自己的页，不在参数页里，但预设照样要挂
const profileGroups = api.SETTING_GROUPS.filter((g) => g.profileGroup);
chk("四个组挂了预设", profileGroups.length === 4);
chk(
  "预设键名符合后端",
  profileGroups.every((g) => ["active", "learn", "style", "facts"].includes(g.profileGroup)),
);

// ── 预设条：按钮带组名，命中时高亮 ──
api.setStatus({
  profiles: {
    learn: [
      { id: "lean", label: "🐢 极省", desc: "一天最多学一次", values: { analysis_interval_seconds: 86400 } },
      { id: "balanced", label: "⚖️ 平衡", desc: "6 小时学一次", values: { analysis_interval_seconds: 21600 } },
    ],
  },
});
const bar = api.profileBar("learn", { analysis_interval_seconds: 21600 });
chk("预设条渲染出按钮", bar.includes("🐢 极省") && bar.includes("⚖️ 平衡"));
chk("按钮带组名", bar.includes('data-profile-group="learn"'));
chk("命中当前档位时高亮", /data-profile="balanced"[^>]*class="on"/.test(bar));
chk("标签跟着组走", bar.includes("学习强度"));
chk("没命中时提示已微调", api.profileBar("learn", { analysis_interval_seconds: 3600 }).includes("微调"));
chk("未知组不渲染", api.profileBar("nope", {}) === "");

// ── 主动回复页：列表 / 筛选 / 排序 ──
const activeNodes = {};
const origActiveGet = global.document.getElementById;
global.document.getElementById = (id) => {
  if (!activeNodes[id]) activeNodes[id] = escEl();
  return activeNodes[id];
};
api.setActiveGroups(
  [
    { session_id: "bot1:GroupMessage:111", name: "群 111", enabled: true, today: 3, limit: 8, full: false, last_at: 1762000000, next_roll_in: 600, streak: 0 },
    { session_id: "bot1:GroupMessage:222", name: "群 222", enabled: false, today: 0, limit: 8, full: false, last_at: null, next_roll_in: null, streak: 0 },
    { session_id: "bot1:GroupMessage:333", name: "群 333", enabled: true, today: 8, limit: 8, full: true, last_at: 1762100000, next_roll_in: null, streak: 4 },
  ],
  true,
  6,
);
api.renderActiveList();
api.renderActiveStats();
const activeHtml = activeNodes.activeList.innerHTML;
const activeStatsHtml = activeNodes.activeStats.innerHTML;

chk("每个群一行", (activeHtml.match(/data-active-group=/g) || []).length === 3);
chk("群名与今日次数都在", activeHtml.includes("群 111") && activeHtml.includes("今日 3/8"));
chk("关掉的群有 off 标记", /class="arow off"[\s\S]{0,240}GroupMessage:222/.test(activeHtml));
chk("今日已满单独标出来", activeHtml.includes("arow-tag full") && activeHtml.includes("今日已满"));
chk("开着的群标「开着」", activeHtml.includes("arow-tag on"));
chk("默认按今日降序", activeHtml.indexOf("群 333") < activeHtml.indexOf("群 111"));
chk("统计里有总开关和群数", activeStatsHtml.includes("总开关") && activeStatsHtml.includes("2 / 3"));
chk("统计里有每人每日上限", activeStatsHtml.includes("6 次"));

// 搜索过滤：走的是列头同一份 state
api.ACTIVE_GF.state.text.who = "111";
api.renderActiveList();
chk("搜群号能过滤", !activeNodes.activeList.innerHTML.includes("群 333"));
api.ACTIVE_GF.state.text.who = "";

// 状态筛选：只看关掉的
api.ACTIVE_GF.state.sets.states = ["off"];
api.renderActiveList();
chk("只看关掉的群", activeNodes.activeList.innerHTML.includes("群 222")
  && !activeNodes.activeList.innerHTML.includes("群 111"));
api.ACTIVE_GF.state.sets.states = [];

// 排序切成升序按最近说话
api.ACTIVE_GF.state.sort = "idle";
api.ACTIVE_GF.state.dir = "asc";
api.renderActiveList();
chk("按最近升序时没说过话的在前", activeNodes.activeList.innerHTML.indexOf("群 222") < activeNodes.activeList.innerHTML.indexOf("群 111"));
api.ACTIVE_GF.reset();
global.document.getElementById = origActiveGet;


// ── 喂食页：投喂榜按次数排、今日次数独立显示 ──
// 这一页是直接写 DOM 的，临时换掉 getElementById 让它在 stub 里跑一遍
const feedNodes = {};
const origGetById = global.document.getElementById;
global.document.getElementById = (id) => {
  if (!feedNodes[id]) feedNodes[id] = escEl();
  return feedNodes[id];
};
api.setFeedBoard({
  enabled: true,
  limit: 1,
  gain_random: 2,
  gain_order: 5,
  stats: { people: 2, total: 9, today: 1, today_people: 1, last_at: 1762000000 },
  items: [
    { qq: "111", name: "小明", total: 6, today: 1, last_dish: "牛肉面", last_mode: "order", last_at: 1762000000 },
    { qq: "222", name: "小红", total: 3, today: 0, last_dish: "饺子", last_mode: "random", last_at: 1761000000 },
  ],
});
api.renderFeedBoard();
global.document.getElementById = origGetById;
const feedListHtml = (feedNodes.feedList || {}).innerHTML || "";
chk("投喂榜渲染出人", feedListHtml.includes("小明") && feedListHtml.includes("111"));
// stub 的 innerHTML getter 会做一次 HTML 转义，所以这里按转义后的文本断言
chk("投喂榜显示累计次数", feedListHtml.includes("6&lt;/b&gt; 次"));
chk("投喂榜区分点菜与随机", feedListHtml.includes("（点菜）") && feedListHtml.includes("（随机）"));
chk("今日次数带上限", feedListHtml.includes("今天 1/1"));
chk("投喂榜第一名有奖牌", feedListHtml.includes("🥇"));
chk(
  "今日汇总进统计格",
  ((feedNodes.feedStats || {}).innerHTML || "").includes("9 次"),
);
chk(
  "说明里点明占日限额度",
  ((feedNodes.feedNote || {}).textContent || "").includes("单日单人上限"),
);

// ── 用量页：回复与后台各成一块，块内再拆来源 ──
const usageNodes = {};
global.document.getElementById = (id) => {
  if (!usageNodes[id]) usageNodes[id] = escEl();
  return usageNodes[id];
};
api.renderUsageSource({
  by_source: {
    reply: { calls: 12, cost: 0.216, tokens: { input_other: 4000, output: 900 } },
    active: { calls: 3, cost: 0.045, tokens: {} },
    learn: { calls: 2, cost: 0.008, tokens: {} },
  },
});
const srcHtml = (usageNodes.usageSource || {}).innerHTML || "";
chk("被叫回复单独一行", srcHtml.includes("被叫回复"));
chk("被叫回复次数与金额", srcHtml.includes("12 次") && srcHtml.includes("¥0.22"));
chk("给出平均每次单价", srcHtml.includes("平均 ¥0.02/次"));
chk("没有记录的来源不渲染", !srcHtml.includes("插话判定"));
chk("回复块汇总是 reply+active", srcHtml.includes("15 次") && srcHtml.includes("¥0.26"));
// 0.008 元小于一分钱，fmtMoney 会用四位小数，别显示成 ¥0.00
chk("后台块单独汇总", srcHtml.includes("🧠 后台") && srcHtml.includes("¥0.0080"));
chk("有占比条与两块颜色", srcHtml.includes("ub-bar") && srcHtml.includes('class="chat"'));
chk("每块标出占今日比例", /占今日 \d+%/.test(srcHtml));

// 后台有、回复没有时，不该留一个 ¥0.00 的回复块
api.renderUsageSource({ by_source: { learn: { calls: 1, cost: 0.004 } } });
const backOnly = (usageNodes.usageSource || {}).innerHTML || "";
chk("回复没数据就不渲染回复块", !backOnly.includes("💬 回复"));
chk("后台仍有数据时正常渲染", backOnly.includes("📚 学习风格"));
global.document.getElementById = origGetById;

console.log("\n" + (ok ? "全部通过" : "有失败项"));
process.exit(ok ? 0 : 1);

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
    " setStatus: (v) => { lastStatus = v; }," +
    " setSticker: (v) => { stickerData = v; }," +
    " setStickerLabel: (v) => { stickerLabel = v; }," +
    " setStickerQuery: (v) => { stickerQuery = v; }," +
    " setStickerSort: (v) => { stickerSortMode = v; }," +
    " setStickerCloud: (v) => { stickerCloud = v; }," +
    " renderTriggerHelp, renderBudgetBar, renderFeatureSwitches, renderBars," +
    " setLastStatus: (v) => { lastStatus = v; }," +
    " sumUsageDays, usageSummaryOf, renderTotals," +
    " setUsageRange: (v) => { usageRange = v; }," +
    " renderStickerGrid, renderSticker, stickerAllItems};",
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
chk("参数页最后一组是管理工具", ordered[ordered.length - 1].id === "admin");
// 参数组按主题拆到各页之后，参数页只剩「不分页的那几个」
chk(
  "风格/事实/表情包/搜索/计价/预算都挂到了各自的页",
  ["style", "facts", "sticker", "search", "price", "budget"].every(
    (id) => (api.SETTING_GROUPS.find((g) => g.id === id) || {}).pane,
  ),
);
chk("参数页不再出现这些组",
  settingsGroups.every((g) => !["style", "facts", "sticker", "search", "price", "budget"].includes(g.id)),
  settingsGroups.map((g) => g.id).join(","));
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
chk("统计里有此刻放出", activeStatsHtml.includes("此刻放出"));
chk("列表里放出额度小于日限时写出来",
  activeHtml.includes("此刻放出") || activeHtml.includes("今日 3/8"));

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

// ── 表情包：全部视图下缩略图必须带分类前缀 ──
// 踩过的坑：key 拼的是 stickerLabel，而「全部」视图它为空串，
// 请求变成 c=&n=xxx，后台判「缺少参数」，整个图库全变「读不出来」
const origGetById2 = document.getElementById;
const boxes = {};
const mkBox = () => {
  const o = el();
  let html = "";
  let cls = "";
  Object.defineProperty(o, "innerHTML", {
    get: () => html,
    set: (v) => { html = String(v == null ? "" : v); },
  });
  Object.defineProperty(o, "textContent", {
    get: () => html,
    set: (v) => { html = String(v == null ? "" : v); },
  });
  Object.defineProperty(o, "className", {
    get: () => cls,
    set: (v) => { cls = String(v == null ? "" : v); },
  });
  // classList 要和 className 联动：renderBars 用 toggle 加 dense，
  // 只实现 add/remove 会让它在测试里直接抛 TypeError
  o.classList = {
    add: (c) => {
      if (!cls.split(/\s+/).includes(c)) cls = (cls + " " + c).trim();
    },
    remove: (c) => {
      cls = cls.split(/\s+/).filter((x) => x && x !== c).join(" ");
    },
    contains: (c) => cls.split(/\s+/).includes(c),
    toggle: (c, on) => {
      const has = cls.split(/\s+/).includes(c);
      const want = on === undefined ? !has : !!on;
      if (want && !has) cls = (cls + " " + c).trim();
      if (!want && has) cls = cls.split(/\s+/).filter((x) => x && x !== c).join(" ");
      return want;
    },
  };
  return o;
};
document.getElementById = (id) => {
  if (!boxes[id]) boxes[id] = mkBox();
  return boxes[id];
};

api.setSticker({
  labels: [
    { key: "laugh", name: "笑", count: 1, items: [
      { name: "a.jpg", size: 100, tags: ["笑死"], added_at: 1700000000, used: 5, mtime: 1700000000 },
    ] },
    { key: "cry", name: "哭", count: 1, items: [
      { name: "b.png", size: 200, tags: [], added_at: 1790000000, used: 0, mtime: 1790000000 },
    ] },
  ],
  total: 2,
});

api.setStickerLabel("");
api.setStickerQuery("");
api.renderStickerGrid();
const allHtml = boxes["stickerGrid"].innerHTML;
chk("全部视图缩略图带分类", allHtml.includes('data-thumb="laugh/a.jpg"'), allHtml.slice(0, 120));
chk("全部视图第二张也带分类", allHtml.includes('data-thumb="cry/b.png"'));
chk("不出现空分类前缀", !allHtml.includes('data-thumb="/'));

api.setStickerLabel("laugh");
api.renderStickerGrid();
const oneHtml = boxes["stickerGrid"].innerHTML;
chk("单分类视图照常", oneHtml.includes('data-thumb="laugh/a.jpg"'));
chk("单分类只显示自己的图", !oneHtml.includes("b.png"));

// 搜索过滤
api.setStickerLabel("");
api.setStickerQuery("笑");
api.renderStickerGrid();
const searchHtml = boxes["stickerGrid"].innerHTML;
chk("按标签搜到对应图", searchHtml.includes("a.jpg"));
chk("搜不到的不渲染", !searchHtml.includes("b.png"));
api.setStickerQuery("");

// ── 图库排序 ──
// 默认「刚收的在前」：跨分类也要按时间，不能按分类顺序拼
api.setStickerSort("added_desc");
api.renderStickerGrid();
const byNew = boxes["stickerGrid"].innerHTML;
chk("默认新收的在前（跨分类）", byNew.indexOf("b.png") < byNew.indexOf("a.jpg"));

api.setStickerSort("added_asc");
api.renderStickerGrid();
const byOld = boxes["stickerGrid"].innerHTML;
chk("最早收的在前", byOld.indexOf("a.jpg") < byOld.indexOf("b.png"));

api.setStickerSort("used_desc");
api.renderStickerGrid();
const byUsed = boxes["stickerGrid"].innerHTML;
chk("按发得最多的排", byUsed.indexOf("a.jpg") < byUsed.indexOf("b.png"), "a 发过 5 次");

api.setStickerSort("used_asc");
api.renderStickerGrid();
chk("按发得最少的排（找该删的）", boxes["stickerGrid"].innerHTML.indexOf("b.png") <
  boxes["stickerGrid"].innerHTML.indexOf("a.jpg"));

api.setStickerSort("size_desc");
api.renderStickerGrid();
chk("按文件大小排", boxes["stickerGrid"].innerHTML.indexOf("b.png") <
  boxes["stickerGrid"].innerHTML.indexOf("a.jpg"));

// 时间在卡片上要能看见
api.setStickerSort("added_desc");
api.renderStickerGrid();
chk("卡片显示相对时间", /(\d+ (分钟|小时|天|个月)前|刚刚|\d{4}-\d{2}-\d{2})/.test(
    boxes["stickerGrid"].innerHTML,
  ),
  boxes["stickerGrid"].innerHTML.match(/sticker-meta">[^<]*/)?.[0] || "");

// ── 词云 ──

api.setStickerLabel("");

api.setStickerQuery("");

api.setStickerCloud(true);

api.renderStickerGrid();

const cloudHtml = boxes["stickerGrid"].innerHTML;

chk("词云渲染出标签", cloudHtml.includes("cloud-tag") && cloudHtml.includes("笑死"), cloudHtml.slice(0, 90));

chk("词云带张数", /<b>\d+<\/b>/.test(cloudHtml));

chk("词云容器换成 cloud 类", String(boxes["stickerGrid"].className).includes("cloud"),

  String(boxes["stickerGrid"].className));

api.setStickerCloud(false);

api.renderStickerGrid();

chk("退出词云回到网格", !boxes["stickerGrid"].innerHTML.includes("cloud-tag"));

chk("退出后 class 复原", boxes["stickerGrid"].className === "sticker-grid",

  String(boxes["stickerGrid"].className));



// ── 停用卡片 ──

api.setSticker({

  labels: [

    { key: "laugh", name: "笑", count: 1, items: [

      { name: "off.jpg", size: 10, tags: ["笑"], added_at: 1700000000, disabled: true },

    ] },

  ],

  total: 1,

});

api.setStickerLabel("");

api.setStickerSort("added_desc");

api.renderStickerGrid();

const offHtml = boxes["stickerGrid"].innerHTML;

chk("停用卡片带 off 类", offHtml.includes('class="sticker off"'), offHtml.slice(0, 70));

chk("停用卡片有角标", offHtml.includes("sticker-off"));

chk("停用卡片仍在（不是删掉）", offHtml.includes("off.jpg"));



// ── 概览：触发清单 ──

api.renderTriggerHelp();

const helpHtml = boxes["triggerHelp"].innerHTML;

chk("清单渲染出分组", helpHtml.includes("help-group") && helpHtml.includes("学习库"));

chk("清单覆盖各页", ["人格与关系", "表情包", "主动回复", "联网搜索", "Token 消费"].every((n) => helpHtml.includes(n)));

chk("清单带跳转锚点", helpHtml.includes('data-goto="sticker"'));

chk("清单标出触发类型", helpHtml.includes('data-k="cmd"') && helpHtml.includes('data-k="auto"'));



// ── Token 消费：预算条 ──

api.renderBudgetBar({ budget: { enabled: false, limit: 2, spent: 0.5, remain: 1.5 } });

chk("预算没开时给提示", boxes["budgetBar"].innerHTML.includes("上限没开"));



api.renderBudgetBar({ budget: { enabled: true, limit: 2, spent: 0.5, remain: 1.5, exceeded: false, over_limit: false, bypassed: false } });

const b1 = boxes["budgetBar"].innerHTML;

chk("预算正常显示已花与上限", b1.includes("0.50") && b1.includes("2.00"));

chk("预算正常时显示剩余", b1.includes("还能花"));

chk("进度条按比例", b1.includes("width:25%"));



api.renderBudgetBar({ budget: { enabled: true, limit: 2, spent: 2.6, remain: 0, exceeded: true, over_limit: true, bypassed: false } });

const b2 = boxes["budgetBar"].innerHTML;

chk("超支时明说收摊", b2.includes("已到上限") && b2.includes("收摊"));

chk("超支进度条封顶且变红", b2.includes("width:100%") && b2.includes('class="over"'));
chk("超支时给「今天先放开」按钮", b2.includes('data-budget="release"'));

api.renderBudgetBar({ budget: { enabled: true, limit: 2, spent: 2.6, remain: 0, exceeded: false, over_limit: true, bypassed: true } });
const b3 = boxes["budgetBar"].innerHTML;
chk("已放开时说清明早恢复", b3.includes("今天已放开") && b3.includes("明早"));
chk("已放开时给「恢复限制」按钮", b3.includes('data-budget="relock"'));
chk("已放开时进度条不再标红", !b3.includes('<i class="over"'));

api.renderBudgetBar({ budget: { enabled: true, limit: 2, spent: 0.5, remain: 1.5, exceeded: false, over_limit: false, bypassed: false } });
chk("没超支时不显示任何按钮", !boxes["budgetBar"].innerHTML.includes("data-budget"));



// ── 功能总开关状态同步 ──

api.renderFeatureSwitches({ active_reply_enabled: true, feed_enabled: false, web_search_enabled: true });

chk("主动回复开关跟着配置", boxes["activeEnabled"].checked === true);

chk("喂食开关跟着配置", boxes["feedEnabled"].checked === false);

chk("搜索开关跟着配置", boxes["searchEnabled"].checked === true);



// ── 消耗范围聚合 ──

const fakeDays = [

  { date: "2026-11-20", cost: 1, calls: 10, search_calls: 1, tokens: { input_cached: 100, input_other: 50, output: 20 } },

  { date: "2026-11-21", cost: 2, calls: 20, search_calls: 2, tokens: { input_cached: 200, input_other: 60, output: 30 } },

  { date: "2026-11-22", cost: 3, calls: 30, search_calls: 3, tokens: { input_cached: 300, input_other: 70, output: 40 } },

];

const summed = api.sumUsageDays(fakeDays);

chk("按天加总金额", summed.cost === 6, String(summed.cost));

chk("按天加总调用", summed.calls === 60);

chk("按天加总搜索", summed.searches === 6);

chk("按天加总 token", summed.tokens.input_cached === 600 && summed.tokens.output === 90);



const fakeUsage = {

  today: { date: "2026-11-22", cost: 3, calls: 30, search_calls: 3, tokens: { input_cached: 300, input_other: 70, output: 40 } },

  days: fakeDays,

  totals: { days: 40, cost: 80, calls: 800, searches: 8 },

};



api.setUsageRange(1);

const rToday = api.usageSummaryOf(fakeUsage);

chk("今天用日文件", rToday.label === "今天" && rToday.cost === 3, String(rToday.cost));



api.setUsageRange(7);

const r7 = api.usageSummaryOf(fakeUsage);

chk("近 7 天聚合全部样本", r7.cost === 6 && r7.days === 3, `${r7.cost}/${r7.days}`);

chk("区间带出首末日期", String(r7.span).includes("2026-11-20") && String(r7.span).includes("2026-11-22"), r7.span);



api.setUsageRange(0);

const rAll = api.usageSummaryOf(fakeUsage);

chk("全部走 totals", rAll.label === "全部" && rAll.cost === 80 && rAll.days === 40, `${rAll.cost}/${rAll.days}`);



api.setUsageRange(1);



// ── 清单折叠与收表情文案 ──

chk("清单超 5 条的组默认折叠", helpHtml.includes("help-more") && helpHtml.includes("展开另外"));

chk("折叠里有具体条数", /展开另外 \d+ 条/.test(helpHtml));

chk("前 5 条不折", (helpHtml.match(/<ul class="help-rows">/g) || []).length >= 11);

chk("收表情说明带用法", helpHtml.includes("/收表情 全部") && helpHtml.includes("/收表情 50 笑"));



// ── 累计消耗面板 ──

api.renderTotals({

  totals: {

    days: 40, cost: 12.5, calls: 900, searches: 30,

    by_source: { learn: 200, reply: 600, active: 50, sticker: 20, profile: 10, search: 30 },

  },

});

const totHtml = boxes["usageTotal"].innerHTML;

chk("累计显示金额", totHtml.includes("12.50"), totHtml.slice(0, 80));

chk("累计显示调用与搜索", totHtml.includes("900") && totHtml.includes("30"));

chk("累计显示记录天数", totHtml.includes("40 天"));

chk("累计显示日均", totHtml.includes("0.31") || totHtml.includes("0.3125"), totHtml);



const totSrc = boxes["usageTotalSource"].innerHTML;

chk("来源条按次数排", totSrc.indexOf("被叫回复") < totSrc.indexOf("学习提炼"), totSrc.slice(0, 120));

chk("来源条有计数", totSrc.includes("600"));

chk("没出现的来源不渲染", !totSrc.includes("插话判定") && !totSrc.includes("图片打标"));



api.renderTotals({ totals: { days: 0, cost: 0, calls: 0, searches: 0, by_source: {} } });

chk("没有记录时给提示", boxes["usageTotalSource"].innerHTML.includes("还没有"));



// ── 柱状图密度 ──

const mkDays = (n) => Array.from({ length: n }, (_, i) => ({

  date: `2026-11-${String((i % 28) + 1).padStart(2, "0")}`, cost: i + 1, calls: i + 1,

}));

api.renderBars(mkDays(7));

chk("7 天用正常间距", !String(boxes["usageBars"].className).includes("dense"),

  String(boxes["usageBars"].className));

const sevenHtml = boxes["usageBars"].innerHTML;

chk("7 天每根都有标签", (sevenHtml.match(/<em>\d\d-\d\d<\/em>/g) || []).length === 7,

  String((sevenHtml.match(/<em>/g) || []).length));



api.renderBars(mkDays(30));

chk("30 天进密集模式", String(boxes["usageBars"].className).includes("dense"),

  String(boxes["usageBars"].className));

const thirtyHtml = boxes["usageBars"].innerHTML;

const labels = (thirtyHtml.match(/<em>\d\d-\d\d<\/em>/g) || []).length;

chk("30 天标签抽稀到 8 个以内", labels <= 8 && labels >= 4, String(labels));

chk("30 天柱子一根不少", (thirtyHtml.match(/class="bar-col"/g) || []).length === 30);



document.getElementById = origGetById2;

// ── 表情包统计条：发图占回复的比例 ──
document.getElementById = (id) => {
  if (!boxes[id]) boxes[id] = mkBox();
  return boxes[id];
};

api.setSticker({
  labels: [{ key: "laugh", name: "笑", count: 1, items: [] }],
  total: 1,
  today_total: 16,
  today_replies: 53,
  reply_ratio: 30,
});
api.renderSticker();
const statsHtml = boxes.stickerStats ? boxes.stickerStats.innerHTML : "";
chk("统计条有占回复比", statsHtml.includes("占回复比"), statsHtml.slice(0, 60));
chk("占比按 16/53 算成 30%", statsHtml.includes("30%"), statsHtml.slice(0, 160));

api.setSticker({
  labels: [{ key: "laugh", name: "笑", count: 1, items: [] }],
  total: 1,
  today_total: 2,
  today_replies: 0,
  reply_ratio: 0,
});
api.renderSticker();
const statsHtml2 = boxes.stickerStats ? boxes.stickerStats.innerHTML : "";
chk("还没回复时占比显示破折号", statsHtml2.includes("—"), statsHtml2.slice(0, 160));
chk("比例关掉时说清楚", statsHtml2.includes("比例闸已关"), statsHtml2.slice(0, 200));

console.log("\n" + (ok ? "全部通过" : "有失败项"));
process.exit(ok ? 0 : 1);

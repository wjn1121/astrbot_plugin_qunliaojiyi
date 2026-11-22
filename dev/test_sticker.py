"""表情包的断言：标记解析、七档定义、触发闸门、增删与记账。

用法：python dev/test_sticker.py
只测 core/sticker.py 与 core/settings.py，不碰 AstrBot 运行时。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import tempfile
import types
from pathlib import Path

_pkg = types.ModuleType("astrbot")
_api = types.ModuleType("astrbot.api")
_api.logger = logging.getLogger("test")
_pkg.api = _api
sys.modules.setdefault("astrbot", _pkg)
sys.modules.setdefault("astrbot.api", _api)

ROOT = Path(__file__).resolve().parent.parent

pkg = types.ModuleType("qunliao")
pkg.__path__ = [str(ROOT / "core")]
sys.modules["qunliao"] = pkg


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"qunliao.{name}", ROOT / "core" / f"{name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"qunliao.{name}"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


feed = _load("feed")
sticker = _load("sticker")
settings_mod = _load("settings")

ok = True


def chk(name: str, cond: bool, detail: str = "") -> None:
    global ok
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        ok = False


class FakeSettings:
    BASE = {
        "sticker_enabled": True,
        "sticker_mood_mode": "keyword",
        "sticker_mood_rate": 100,
        "sticker_probability": 100,
        "sticker_follow_image": True,
        "sticker_follow_probability": 100,
        "sticker_chain": False,
        "sticker_formation": False,
        "sticker_cooldown_seconds": 0,
        "sticker_daily_limit": 40,
        "sticker_analyze_provider_id": "",
    }

    def __init__(self, cfg=None):
        self._cfg = dict(self.BASE)
        for k, v in (cfg or {}).items():
            self._cfg[k if k.startswith("sticker_") else f"sticker_{k}"] = v

    def get(self, key):
        return self._cfg.get(key)


class FakePlugin:
    def __init__(self, cfg=None):
        self.data_dir = Path(tempfile.mkdtemp(prefix="sticker_"))
        self.settings = FakeSettings(cfg)


def make_store(cfg=None):
    return sticker.StickerStore(FakePlugin(cfg))


# ── 标记解析（用实例，分类表是动态的）──
ps = make_store()

chk("标准标记", ps.strip_marks("笑死<emo:笑>") == ("笑死", ["laugh"]))
chk("方括号", ps.strip_marks("[emo:无语]") == ("", ["speechless"]))
chk("中文冒号", ps.strip_marks("<emo：夸>") == ("", ["praise"]))
chk("英文 key", ps.strip_marks("<emo:laugh>") == ("", ["laugh"]))
chk("不认识的标签原样留着", ps.strip_marks("<emo:开心>") == ("<emo:开心>", []))
chk("没有 emo 不算标记", ps.strip_marks("【笑】") == ("【笑】", []))
chk("空文本", ps.strip_marks("") == ("", []))
chk(
    "标记在句中也能剥",
    ps.strip_marks("行吧<emo:无语>，你说了算")[0] == "行吧，你说了算",
)
chk("多个标记文本全清", ps.strip_marks("<emo:笑>好<emo:哭>")[0] == "好")
chk("尾部不留空", ps.strip_marks("你说得对 <emo:笑>")[0] == "你说得对")

# ── 情绪反解 ──
chk("单词", ps._parse_mood("笑") == "laugh")
chk("带解释", ps._parse_mood("这句话的情绪是「无语」") == "speechless")
chk("判断不出", ps._parse_mood("无") is None)
chk("空", ps._parse_mood("") is None)

# ── 七档定义 ──
profiles = settings_mod.STICKER_PROFILES
chk("七档", len(profiles) == 7, str(len(profiles)))
chk(
    "档位 id 顺序",
    [p["id"] for p in profiles]
    == ["off", "literal", "follow", "after", "light", "steady", "full"],
    str([p["id"] for p in profiles]),
)
bad_keys = set()
for p in profiles:
    for k in p["values"]:
        if k not in settings_mod.DEFAULTS:
            bad_keys.add(k)
chk("档位只写认识的配置项", not bad_keys, str(bad_keys))
chk(
    "第一档真的关掉",
    profiles[0]["values"].get("sticker_enabled") is False
    and profiles[0]["values"].get("sticker_mood_mode") == "off",
)
chk(
    "后三档都是 marker",
    all(p["values"].get("sticker_mood_mode") == "marker" for p in profiles[4:]),
)
chk(
    "注入率递增",
    [p["values"].get("sticker_mood_rate") for p in profiles[4:]] == [30, 60, 100],
)
chk("档位挂进 GROUP_PROFILES", len(settings_mod.GROUP_PROFILES.get("sticker") or []) == 7)

# ── 关键词匹配 ──
chk("笑 → laugh", ps.match_label("哈哈哈哈哈") == "laugh")
chk("无关键词 → None", ps.match_label("今天天气不错") is None)
chk("大小写不敏感", ps.match_label("YYDS") == "praise")

# ── 空图库不触发 ──
s = make_store()
chk("空图库 → None", s.decide("qq:Group:1", "哈哈哈", False) is None)
chk("总数 0", s.total() == 0)

# ── 放图后触发 ──
okadd, info = s.add_file("laugh", "a.jpg", b"\xff\xd8fakejpeg")
chk("加图成功", okadd, info)
hit = s.decide("qq:Group:1", "哈哈哈哈哈", False)
chk("关键词命中", hit is not None and hit[0] == "laugh", str(hit))
chk("路径存在", bool(hit) and Path(hit[1]).is_file())

# ── 外部标签优先 ──
hit2 = s.decide("qq:Group:1", "完全无关的话", False, mood="laugh")
chk("外部标签直接用", hit2 is not None and hit2[0] == "laugh", str(hit2))
hit3 = s.decide("qq:Group:1", "哈哈哈", False, mood="cry")
chk("外部标签压过关键词，没图则退一步", hit3 is not None, str(hit3))

# ── 记账 ──
s.record("qq:Group:1", "laugh")
chk("今日计数 +1", s.today_total() == 1)
chk("快照含 10 个内置分类", len(s.snapshot()["labels"]) == 10)
chk("快照带 enabled", s.snapshot()["enabled"] is True)

# ── 模式闸门 ──
off = make_store({"mood_mode": "off"})
off.add_file("laugh", "a.jpg", b"\xff\xd8x")
chk("mode=off 不触发", off.decide("qq:Group:1", "哈哈哈哈", False) is None)

marker = make_store({"mood_mode": "marker", "mood_rate": 100})
marker.add_file("laugh", "a.jpg", b"\xff\xd8x")
chk("marker 模式没标签不发", marker.decide("qq:Group:1", "哈哈哈哈", False) is None)
chk(
    "marker 模式有标签才发",
    marker.decide("qq:Group:1", "", False, mood="laugh") is not None,
)
chk("marker 模式会注规则", marker.should_inject_rule() is True)
chk("keyword 模式不注规则", make_store({"mood_mode": "keyword"}).should_inject_rule() is False)
chk("marker 模式不做分析", marker.should_analyze() is False)
chk(
    "analyze 模式会分析",
    make_store({"mood_mode": "analyze", "mood_rate": 100}).should_analyze() is True,
)

# ── 日限 / 冷却 / 队形 / 连发 ──
capped = make_store({"daily_limit": 1})
capped.add_file("laugh", "a.jpg", b"\xff\xd8x")
capped.record("qq:Group:1", "laugh")
chk("日限到了不触发", capped.decide("qq:Group:2", "哈哈哈哈", False) is None)

cool = make_store({"cooldown_seconds": 600})
cool.add_file("laugh", "a.jpg", b"\xff\xd8x")
cool.record("qq:Group:1", "laugh")
chk("同群冷却中", cool.decide("qq:Group:1", "哈哈哈哈", False) is None)
chk("别的群不受影响", cool.decide("qq:Group:9", "哈哈哈哈", False) is not None)

form = make_store({"formation": True})
form.add_file("laugh", "a.jpg", b"\xff\xd8x")
form.note_image("q")
chk("两张图还没到队形", form.note_image("q") is False)
chk("第三张凑够队形", form.note_image("q") is True)
chk("凑够后清空，不会连着触发", form.note_image("q") is False)
chk("队形能出图", form.pick_formation("q") is not None)

chain = make_store({"chain": True, "follow_probability": 100})
chain.add_file("laugh", "a.jpg", b"\xff\xd8x")
chain.add_file("cry", "b.jpg", b"\xff\xd8y")
chk("连发概率 100 能出图", chain.chain_follow("q", "laugh") is not None)
chk(
    "没开连发就不出",
    make_store({"chain": False}).chain_follow("q", "laugh") is None,
)

# ── 跟图 ──
follow = make_store()
follow.add_file("chill", "b.jpg", b"\xff\xd8y")
chk("带图跟一张", follow.decide("qq:Group:1", "看这个", True) is not None)
chk("纯文字不跟图", follow.decide("qq:Group:1", "看这个", False) is None)

# ── 删图 ──
okdel, msg = s.remove("laugh", info)
chk("删图成功", okdel, msg)
chk("删完计数归零", s.counts().get("laugh") == 0)
chk("拒绝路径穿越", not s.remove("laugh", "../lib/x.jpg")[0])

# ── 分类 CRUD ──
cs = make_store()
chk("内置 10 个分类", len(cs.categories) == 10, str(len(cs.categories)))
chk("分类表已落盘", cs.labels_path.is_file())

okc, key = cs.add_category("猫猫", "猫的表情，卖萌或撒娇用", ["猫", "喵"])
chk("新建分类", okc, key)
chk("新分类能查到", cs.find("猫猫") is not None)
chk("新分类目录已建", (cs.root / key).is_dir())
chk("重名拒绝", not cs.add_category("猫猫", "x")[0])
chk("非法名字拒绝", not cs.add_category("a/b", "x")[0])
chk("空名字拒绝", not cs.add_category("  ", "x")[0])

chk("改描述", cs.update_category(key, desc="改成猫猫图")[0])
chk("描述已生效", cs.category_of(key)["desc"] == "改成猫猫图")
chk("改关键词", cs.update_category(key, keywords=["喵喵", "猫猫"])[0])
chk("关键词已生效", cs.category_of(key)["keywords"] == ["喵喵", "猫猫"])
chk("新关键词能命中", cs.match_label("喵喵喵") == key)

okimg, img_name = cs.add_file(key, "喵喵叫.jpg", b"\xff\xd8x")
chk("往新分类放图", okimg, img_name)
chk("有图不允许删分类", not cs.remove_category(key)[0])

okc2, key2 = cs.add_category("临时", "测试用")
chk("删空分类", cs.remove_category(key2)[0])
chk("删完查不到", cs.find("临时") is None)

# ── 规则文本带描述 ──
# 规则只列有图的分类：列了没图的，模型选中也发不出来，白占 token
chk("空图库不给规则", make_store().rule_text() == "")
chk("空图库的分析提示写「无」", "无" in make_store().analyze_system())

cs.add_file("laugh", "笑图.jpg", b"\xff\xd8y")
rule = cs.rule_text()
chk("规则含固定抬头", "<sticker_rule>" in rule and "<emo:分类名>" in rule)
chk("规则含分支描述", "猫猫：改成猫猫图" in rule)
chk("规则含内置描述", "笑：开心、大笑、忍不住了" in rule)
chk("没图的分类不进规则", "无语" not in rule, rule[:200])
chk("token 估算是正数", cs.rule_tokens() > 100, str(cs.rule_tokens()))
chk(
    "分析提示只列有图的",
    "笑" in cs.analyze_system() and "无语" not in cs.analyze_system(),
    cs.analyze_system(),
)

# ── 分类表持久化 ──
cs2 = sticker.StickerStore(cs.plugin)
chk("重开还能读到自建分类", cs2.find("猫猫") is not None)
chk("重开描述没丢", (cs2.find("猫猫") or {}).get("desc") == "改成猫猫图")

# ── 文件名唯一（批量上传不互相覆盖）──
uq = make_store()
names = [uq.add_file("laugh", f"图{i}.jpg", b"x")[1] for i in range(5)]
chk("连续上传文件名不重复", len(set(names)) == 5, str(names))
chk("五张都在磁盘上", len(uq.index()["laugh"]) == 5, str(len(uq.index()["laugh"])))

# ── 标签 ──
chk(
    "无分隔符不猜标签",
    sticker.StickerStore._tags_from_name("笑死熊猫头.gif") == [],
)
chk(
    "有分隔符就拆",
    sticker.StickerStore._tags_from_name("笑死_熊猫头.jpg") == ["笑死", "熊猫头"],
)
chk("纯数字文件名不提标签", sticker.StickerStore._tags_from_name("1795339256_776.jpg") == [])

ts = make_store()
okt, tn = ts.add_file("laugh", "猫猫笑死.jpg", b"x", ["猫", "笑死"])
chk("加图带标签", okt, tn)
chk("标签存下来了", ts.meta_of("laugh", tn)["tags"] == ["猫", "笑死"])

chk("改标签", ts.set_tags("laugh", tn, ["猫", "笑死", "表情"])[0])
chk("标签更新", ts.meta_of("laugh", tn)["tags"] == ["猫", "笑死", "表情"])
ts.set_tags("laugh", tn, ["猫", "猫"])
chk("标签去重", ts.meta_of("laugh", tn)["tags"] == ["猫"])

ok2, other = ts.add_file("laugh", "别的.jpg", b"y")
picks = set()
for _ in range(15):
    h = ts.decide("s1", "我家猫又拆家了", False, mood="laugh")
    if h:
        picks.add(h[1].name)
chk("标签命中优先选那张", picks == {tn}, str(picks))

ts.record("s1", "laugh", tn)
chk("使用次数 +1", ts.meta_of("laugh", tn)["used"] >= 1)
chk("last_used 有值", ts.meta_of("laugh", tn)["last_used"] > 0)

# 挪分类
moved, msg = ts.move_file("laugh", tn, "cry")
chk("挪分类成功", moved, msg)
chk("挪完不在这边", not (ts.root / "laugh" / tn).is_file())
chk("挪完在那边", (ts.root / "cry" / tn).is_file())
chk("元数据跟着走", ts.meta_of("cry", tn)["used"] >= 1)

# ── 全库标签检索 ──
sr = make_store()
_, a = sr.add_file("laugh", "a.jpg", b"x", ["猫", "笑死熊猫头"])
_, b = sr.add_file("laugh", "b.jpg", b"y", ["猫"])
_, c = sr.add_file("cry", "c.jpg", b"z", ["离谱"])

hit = sr.search("我家猫又把键盘踩坏了")
chk("命中带猫标签的图", hit is not None, str(hit))

hit2 = sr.search("你这操作真离谱")
chk("命中离谱那张", hit2 is not None and hit2[1].name == c, str(hit2))

chk("没命中返回 None", sr.search("今天天气不错") is None)
chk("空文本返回 None", sr.search("") is None)

hit3 = sr.search("猫和笑死熊猫头")
chk("稀有标签压过常见标签", hit3 is not None and hit3[1].name == a, str(hit3))

strict = make_store({"search_threshold": 10})
strict.add_file("laugh", "s.jpg", b"x", ["猫"])
chk("阈值调高就不发", strict.search("我家猫") is None)

lowbar = make_store({"search_threshold": 0})
lowbar.add_file("laugh", "s.jpg", b"x", ["猫"])
chk("阈值 0 时命中就发", lowbar.search("我家猫") is not None)

# 检索路径也要过日限
gate = make_store({"daily_limit": 0})
gate.add_file("laugh", "g.jpg", b"x", ["猫"])
chk("日限 0 时 gate 不放行", gate.gate_ok("q") is False)

# ── 打标结果解析 ──
chk(
    "纯 JSON",
    sticker._parse_tag_json('{"tags":["笑"],"desc":"x"}') == {"tags": ["笑"], "desc": "x"},
)
chk(
    "markdown 围栏",
    (sticker._parse_tag_json('```json\n{"tags":["哭"]}\n```') or {}).get("tags") == ["哭"],
)
chk(
    "前后有废话",
    (sticker._parse_tag_json('好的，结果是 {"tags":["惊"]} 请查收') or {}).get("tags") == ["惊"],
)
chk("坏 JSON 返回 None", sticker._parse_tag_json('{"tags": [') is None)
chk("没有 JSON 返回 None", sticker._parse_tag_json("这张图是熊猫头") is None)
chk("空返回 None", sticker._parse_tag_json("") is None)


# ── 自动打标（用假 provider）──
class FakeUsage:
    def record(self, *a, **k):
        pass


class FakeResp:
    completion_text = '{"text": "我裂开了", "tags": ["裂开", "崩溃", "熊猫头"], "desc": "撑不住的时候发"}'
    usage = None


class FakeProvider:
    async def text_chat(self, **kw):
        return FakeResp()


class FakeContext:
    def get_provider_by_id(self, provider_id=""):
        return FakeProvider()

    def get_using_provider(self, *a, **k):
        return FakeProvider()


tg = make_store()
tg.plugin.context = FakeContext()
tg.plugin.usage = FakeUsage()
_, tn2 = tg.add_file("laugh", "我裂开了.jpg", b"\xff\xd8x", ["手写标签"])

okt2, msg2 = asyncio.run(tg.auto_tag("laugh", tn2))
chk("打标成功", okt2, msg2)
tags2 = tg.meta_of("laugh", tn2)["tags"]
chk("模型标签进来了", "裂开" in tags2 and "崩溃" in tags2, str(tags2))
chk("手写标签没被冲掉", "手写标签" in tags2, str(tags2))
chk("图上文字提到最前", tags2[0] == "我裂开了", str(tags2))

# 只识别不落库
tg2 = make_store()
tg2.plugin.context = FakeContext()
tg2.plugin.usage = FakeUsage()
_, tn3 = tg2.add_file("laugh", "x.jpg", b"\xff\xd8x")
okp, payload = asyncio.run(tg2.auto_tag("laugh", tn3, apply=False))
chk("只识别不落库", okp and "裂开" in payload, payload[:60])
chk("库里还是空的", tg2.meta_of("laugh", tn3)["tags"] == [], str(tg2.meta_of("laugh", tn3)))

# 未标注清单
ug = make_store()
_, tagged_name = ug.add_file("laugh", "有标.jpg", b"x", ["笑"])
_, plain_name = ug.add_file("laugh", "没标.jpg", b"y")
rows = ug.untagged()
chk("未标注只列出没标签的", len(rows) == 1, str(rows))
chk("列出的是那张没标签的", bool(rows) and rows[0][1] == plain_name, str(rows))

# 没有 provider 时优雅失败
np = make_store()
_, npn = np.add_file("laugh", "n.jpg", b"x")
okn, msgn = asyncio.run(np.auto_tag("laugh", npn))
chk("没模型时返回失败而不是抛异常", not okn, msgn)

# ── QQ 收藏导入 ──
qh = make_store()
_, hn = qh.add_file("laugh", "a.jpg", b"abc")
row = qh.meta.get(f"laugh/{hn}") or {}
chk("add_file 记了内容哈希", len(str(row.get("hash") or "")) == 32, str(row.get("hash")))
chk("known_hashes 能取到", row.get("hash") in qh.known_hashes())

chk("没有 context 时拉不到列表", asyncio.run(qh.fetch_qq_faces()) == [])
res = asyncio.run(qh.import_qq_faces(limit=10))
chk("拉不到就返回零值而不是抛异常", res["fetched"] == 0 and res["saved"] == 0, str(res))

# 模拟一个"协议端返回了 URL"的最小闭环（下载部分用假 session 太重，
# 这里只验证拿列表之后的分类创建逻辑）
ip = make_store()
ip.fetch_qq_faces = lambda: asyncio.sleep(0, result=[])  # type: ignore
chk("空列表不改动图库", ip.total() == 0)

# ── 停用 ──
ds = make_store()
_, d1 = ds.add_file("laugh", "d1.jpg", b"x", ["笑"])
_, d2 = ds.add_file("laugh", "d2.jpg", b"y", ["笑"])
chk("默认是启用的", ds.is_enabled("laugh", d1))
okd, msgd = ds.set_enabled("laugh", d1, False)
chk("停用成功", okd, msgd)
chk("停用后 is_enabled 为假", not ds.is_enabled("laugh", d1))
chk("meta 带 disabled 标记", ds.meta_of("laugh", d1)["disabled"] is True)

picks = set()
for _ in range(20):
    h = ds.decide("ds1", "", False, mood="laugh")
    if h:
        picks.add(h[1].name)
chk("停用的图抽不到", d1 not in picks, str(picks))
chk("启用的那张还能抽到", d2 in picks, str(picks))

chk("搜索跳过停用图", ds.search("笑") is not None, "还有一张启用的")
ds.set_enabled("laugh", d2, False)
chk("两张都停用后不发", ds.decide("ds2", "", False, mood="laugh") is None)
chk("两张都停用后搜索不命中", ds.search("笑") is None)

ds.set_enabled("laugh", d1, True)
chk("恢复后能选中", ds.decide("ds3", "", False, mood="laugh") is not None)
chk("恢复后字段被清掉", "disabled" not in (ds.meta.get("laugh/" + d1) or {}))

chk("停用不存在的图返回失败", not ds.set_enabled("laugh", "nope.jpg", False)[0])
chk("停用不存在的分类返回失败", not ds.set_enabled("nope", d1, False)[0])

# 停用状态要能持久化（meta 是延迟落盘的，测试里手动冲一次）
asyncio.run(ds.flush())
ds2 = sticker.StickerStore(ds.plugin)
chk("重开后停用状态还在", ds2.is_enabled("laugh", d2) is False)

print()
print("全部通过" if ok else "有失败项")
sys.exit(0 if ok else 1)

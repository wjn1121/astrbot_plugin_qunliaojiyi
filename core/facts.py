"""事实记忆：记住「关于某个人的具体事」，而不是「这个群怎么说话」。

**和 traits 的分工**——这两件事容易混：

- ``universal.json`` 里的 traits：这个群怎么说话。服务于「像这个群的人」，
  是会话级的，注入时不分是谁在说话。
- 本模块的 facts：这个人身上发生过什么。服务于「记得这个人」，
  是「会话 + 用户」级的，A 的私事绝不能注入到回复 B 的 prompt 里。

**为什么默认关**：它记的是用户私事，属于「默认不该发生」的一类功能。
打开之后才有四道保险，不是裸奔：

1. **观察 2 次才转正**（``activate_count``）。同一条事实被独立抽到两次说明它确实重要，
   只出现一次的多半是闲聊噪音。
2. **必须带原文证据**。候选要附本轮原话，程序做子串校验，对不上就丢——
   防的是模型「编造共同经历」，这是陪伴类 bot 最容易翻车的地方。
3. **敏感信息硬黑名单**。密码、证件号、银行卡、验证码、住址、手机号命中即丢，
   且不写日志（日志本身也会泄露）。
4. **即时可删**。``/记得`` 全量可见带编号，``/忘掉`` 立刻删。

隐私边界写死在抽取 prompt 里：只记对用户有利的（偏好、约定、重要事件、明确边界），
不记评价性内容。这条边界比任何开关都重要——「他今天心情不好」这种不落地。
"""

from __future__ import annotations

import re
import time
from typing import Any

from astrbot.api import logger

# 事实类型。boundary 是「明确说过的边界」，单独列出来是因为它不能衰减：
# 「别叫我老弟」这种事记错一次就够烦了，忘掉比记错更糟。
KIND_FACT = "fact"
KIND_EVENT = "event"
KIND_PREFERENCE = "preference"
KIND_BOUNDARY = "boundary"
KINDS = (KIND_FACT, KIND_EVENT, KIND_PREFERENCE, KIND_BOUNDARY)
KIND_LABEL = {
    KIND_FACT: "事实",
    KIND_EVENT: "事件",
    KIND_PREFERENCE: "偏好",
    KIND_BOUNDARY: "边界",
}

# 待观察 / 生效 / 被纠正过 / 人工否决 / 已归档
STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_SUSPECT = "suspect"
STATUS_REJECTED = "rejected"
# 太久没提到，按遗忘曲线归档。还留在库里可查，但不再注入。
# 不直接删是因为「它明明知道这事」变成「突然什么都不记得」更像事故，
# 而归档是可逆的：用户再提一次，``ingest`` 会把它捞回来。
STATUS_ARCHIVED = "archived"
STATUS_LABEL = {
    STATUS_PENDING: "待观察",
    STATUS_ACTIVE: "已生效",
    STATUS_SUSPECT: "疑似记错",
    STATUS_REJECTED: "已否决",
    STATUS_ARCHIVED: "已归档",
}

# 敏感信息黑名单。命中即丢，连日志都不写——日志本身也是泄露面。
# 宁可错杀：漏记一条偏好无所谓，记住一个验证码是事故。
_DENY_PATTERNS = (
    re.compile(r"密码|口令|passw(?:or)?d", re.I),
    re.compile(r"身份证|证件号|护照号"),
    re.compile(r"银行卡|信用卡|卡号|银行账号"),
    re.compile(r"验证码|校验码|动态码|短信码"),
    re.compile(r"手机号|手机号码|电话号码|联系电话"),
    re.compile(r"住址|家庭地址|详细地址|门牌|小区\d"),
    re.compile(r"sk-[A-Za-z0-9]{8,}"),
    re.compile(r"(?:api[_-]?key|access[_-]?token|secret)\s*[=:：]", re.I),
    re.compile(r"私钥|密钥|助记词"),
)

# 单条事实字数上限。事实是「一句话能说清的事」，写成一段就已经不是事实了。
MAX_FACT_CHARS = 60

# 注入预算：条数与字数双限。这段每轮都会进 prompt，失控一次就是永久账单。
# 这两个是**物理硬顶**，配置和关系档位都只能往下调。
# 1.5.0 把硬顶从 3 条 / 200 字提到 10 条 / 500 字，是因为关系档位最高的
# 「主人」档要给到 10 条 500 字——硬顶不跟着抬，那一档就等于白设。
MAX_INJECT_FACTS = 10
MAX_INJECT_CHARS = 500

# 默认每人保留条数。超出按分数淘汰——不是删，是标成 rejected 留档。
# 每人最多存几条的**基准值**（对应「陌生」档）。
# 实际上限按好感档位缩放，见 ``FACT_LIMIT_FACTORS``。
#
# 为什么基准是 20：事实是按「这个人」存的，不是按群。按 QQ 号合并之后，
# 同一个人在几个群说过的事会汇到一起，话多的人很快顶满，
# 把真正有用的旧事挤掉。
DEFAULT_MAX_PER_USER = 30

# 存储上限按好感档位缩放。**这是「能存多少条」，和「每轮能提几条」
# （bond.MEMORY_TIERS 的注入配额）是两件事**，别混。
#
# 依据：参考同类陪伴产品的分层记忆做法——记忆库按亲密度分层，越亲近容量越大；
# 关系浅的人存太多既没用（不会提），又挤占真正该记的人。
# 以基准 30 计：不待见 7 / 陌生 30 / 熟悉 60 / 亲近 90 / 老友 150。
#
# 原来基准是 20，实测一个陌生人在活跃群里两周就能顶满 16~20 条，
# 然后新事挤掉旧事——被挤掉的可能是「他妈妈住院了」这种真正该记的。
# 翻到 30 是给一倍余量，不是让人无限往里塞（注入配额没变，只是存得下）。
FACT_LIMIT_FACTORS = {
    "hostile": 0.25,
    "stranger": 1.0,
    "familiar": 2.0,
    "close": 3.0,
    "intimate": 5.0,
}

# 主人恒定最高档：基准的 15 倍（默认 300 条）。
# 「养鱼」那类玩法的核心就是「养得越久记得越多」，主人这条线上不该有
# 「记得多了反而被淘汰」这种事——他记得的所有事都会进注入候选，
# 由注入配额（每轮 ≤10 条 / 500 字）兜住 token，存储本身不花 token。
OWNER_FACT_LIMIT_FACTOR = 15.0

# 「一键清理」每个档位保留几条。主人不在清理范围内（见 ``prune_all``）。
#
# 和上限一样按基准缩放的话不好解释，所以直接用绝对值：
# 不待见 2 / 陌生 5 / 熟悉 12 / 亲近 20 / 老友 30。
# 陌生档留 5 条是「还能记得你叫什么、干什么的」的最小值——
# 再少就跟没记一样，那不如直接关掉这个功能。
PRUNE_KEEP = {
    "hostile": 3,
    "stranger": 8,
    "familiar": 18,
    "close": 30,
    "intimate": 45,
}

# 主人单独清理时保留几条（配置项同名字段可覆盖）。
# 上限 450 → 清到 225：砍一半边角料，又保住大半年的记忆量。
# 注意「一键清理」仍然完全跳过主人，这个值只给「单独清理主人」用。
OWNER_PRUNE_KEEP = 225

# ── 记忆分档（核心 / 深层 / 浅层）──────────────────────────────────────
#
# 三档，按**熟练度**自动落位，用户可以手动升一级 / 降一级：
#
# - **核心 core**：反复提到、确认重要。注入时逐条列，不归档，降档最慢
# - **深层 deep**：提到过两次以上，或重要性很高。注入时逐条列，会老但慢
# - **浅层 shallow**：刚记下、只提过一次。注入时按类合并成一行（省 token），
#   浅层天数（默认 15 天）没提就归档
#
# 参考同类陪伴产品的分层做法：核心层（固定档案）半衰期接近一年，
# 互动记忆一个月左右。三档比两档好用的地方是「中间那一层」——
# 两档时一条记忆要么被当核心死记，要么跟新记的一句一样随时会忘，
# 没有「有点印象」这个状态。
#
# 存成 ``tier`` 字段而不是三个列表，是因为三档要一起参与排序和淘汰——
# 分成三个列表就得在三处各写一遍「谁先注入」，早晚会不一致。
TIER_CORE = "core"
TIER_DEEP = "deep"
TIER_SHALLOW = "shallow"
# 从低到高的顺序。手动升/降级按这个数组挪一格
TIER_ORDER = (TIER_SHALLOW, TIER_DEEP, TIER_CORE)
TIER_RANK = {t: i for i, t in enumerate(TIER_ORDER)}
TIER_LABEL = {TIER_CORE: "核心", TIER_DEEP: "深层", TIER_SHALLOW: "浅层"}
# 老数据里是 normal（两档时代），统一当浅层——下一轮 _refresh_tiers
# 会按熟练度把它放回该在的档位
_TIER_ALIAS = {"normal": TIER_SHALLOW, "": TIER_SHALLOW, "none": TIER_SHALLOW}

# 熟练度阈值：≥70 核心、≥52 深层、其余浅层。
# 扣着 52 这个数是因为「只提过一次但重要性 4」落 52.75——那种事
# （生日、忌讳、亲口约定）本来就该比随口一句记得牢；
# 而「只提过一次、重要性 3」是 47.5，仍然留在浅层。
PROF_CORE = 70.0
PROF_DEEP = 52.0

# 核心名额按好感档位给。关系越近，能「写死」的记忆越多。
# 名额是**上限不是目标**：没攒够门槛的条目不会被硬塞进核心。
CORE_QUOTA = {
    "hostile": 0,
    "stranger": 1,
    "familiar": 3,
    "close": 5,
    "intimate": 8,
}
# 主人额外给：他是「养得最久」的那条线，核心记忆本该最多
OWNER_CORE_QUOTA = 15

# 自动升级门槛：被独立提到几次 + 最低重要性。
# 3 次是「不是随口一提」的下限：一次是闲聊，两次是重复，三次才算习惯/事实。
CORE_MIN_SEEN = 3
CORE_MIN_IMPORTANCE = 3

# 多久没提就掉档（**兜底默认值**，面板可调：fact_deep_days / fact_core_days）。
#
# 三档寿命是阶梯式的，默认浅层 15 天 → 深层 30 天 → 核心 60 天：
# 半个月没人提的事先忘（归档），一个月没提的降成浅层，两个月没提的掉出核心。
# 半年前天天说的事，现在不提了，还当核心占着名额没意义。
DEEP_IDLE_DAYS = 30
CORE_IDLE_DAYS = 60


def tier_of(item: dict) -> str:
    """取一条记忆的档位。老数据（normal / 空）一律当浅层。"""
    raw = str((item or {}).get("tier") or "").strip().lower()
    if raw in TIER_LABEL:
        return raw
    return _TIER_ALIAS.get(raw, TIER_SHALLOW)


# 相似判定的阈值。**卡得高是故意的**：把两件事并成一件，比多存一条糟得多
# （用户会看到「它把我说的话记混了」）。0.72 大致是「同一句话换了几个词」，
# 「周三交论文」和「下周三交初稿」过不了——那确实可能是一件事的两个阶段。
SIMILAR_THRESHOLD = 0.72
# 长度差超过这个比例就不比了：长短悬殊的两句，2-gram 相似度容易假高
SIMILAR_LEN_RATIO = 0.5

# 注入时「把同类合并成一行」的最低条数。条数少的时候逐条更清楚，
# 多了才值得压缩——3 条并成 1 行省不了几个字，还把可读性弄丢了。
SUMMARY_MIN_ITEMS = 5

# 档位缺失（关系功能没开 / 数据里没这个人）时按陌生档算。
_STAGE_FALLBACK = "stranger"

# 遗忘周期（天）的**兜底默认值**。面板可调（配置项 fact_shallow_days）。
# 曲线：``保留率 = 1 - (t/T)²``，T 就是这个天数。
# 默认 15 天：7 天 78%、10 天 55%、15 天归零。
#
# 和关系不同的是：事实归零不是「清零数值」，是把状态改成 archived（归档）——
# 还留在库里可查，但不再注入。直接删会让用户「我明明记得它知道这事」变成事故。
DECAY_DAYS = 15

# 归档后的条目保留多久再真删。给一个月反悔期。
ARCHIVE_KEEP_DAYS = 180

# 保留率低于这个值就不再注入。15 天曲线下约 12.5 天没提到。
# 光靠排序权重不够：``_score`` 里新鲜度只占 0.2，一条重要性 5、又命中关键词的
# 旧事照样能挤进前几条——那和「快忘了」是矛盾的。这里加一道硬门槛，
# 让两周以上的旧事真的退场，只留一句「好像有这么回事」的印象。
MIN_INJECT_KEEP = 0.3

# 边界类事实不衰减：它不是「发生过的事」，是「一直有效的约定」。
# 「别叫我老弟」这种话说一次就该一直记得，不该因为三个月没提就忘掉。
DECAY_EXEMPT_KINDS = ("boundary",)

# 纠正窗口：用户说「不对」时，只把这么多秒内注入过的事实标成疑似记错。
# 太短会漏（用户隔一轮才纠正），太长会误伤（把更早那些正确的一起标掉）。
CORRECTION_WINDOW = 900


def is_sensitive(text: str, extra: str = "") -> bool:
    """命中了就直接丢。``extra`` 是配置里追加的黑名单（换行或逗号分隔）。"""
    body = str(text or "")
    if not body:
        return False
    for pattern in _DENY_PATTERNS:
        if pattern.search(body):
            return True
    if extra:
        for token in re.split(r"[,，\s]+", str(extra)):
            token = token.strip()
            if token and token in body:
                return True
    return False


def normalize(text: str) -> str:
    """压平空白、去掉首尾标点和多余空格。"""
    body = re.sub(r"\s+", " ", str(text or "")).strip()
    return body.strip("，。、；：！？,.;:!?~～ \t\"'“”‘’")


# 注入时单条记忆最多占多少字。原文可以长（面板要看、要能改），
# 但 prompt 里只需要「能认出是哪件事」的那部分——
# 一条 60 字的记忆截到 36 字，10 条就省 240 字，每轮都在省。
INJECT_MAX_FACT_CHARS = 36

# 开头的主语。「你记得关于这个人的事」已经把主语交代清楚了，
# 每条再写一遍「他……」纯属重复计费。
_LEAD_SUBJECT = re.compile(
    r"^(?:我们|我|他|她|ta|TA|Ta|这个人|对方|该用户|该群友|此人|用户|群友|主人|你)"
    r"(?:的)?[，,、\s]*"
)
# 结尾的语气尾巴：「…了吧」「…呢」「…啊」这类，去掉不丢信息
_TAIL_PARTICLE = re.compile(r"[，,、；;\s]*(?:了吧|了呢|了啊|的话|吗|呢|吧|啊|呀|哈|嘛|哦|啦|咯|嗯|哇)+$")
# 重复标点、多余空格
_DUP_PUNCT = re.compile(r"([，,。.；;！!？?、])\1+")
_SPACES = re.compile(r"\s{2,}")


def condense(text: str, limit: int = INJECT_MAX_FACT_CHARS) -> str:
    """把一条记忆压成适合塞进 prompt 的短句。

    **纯本地正则，不调模型**。这个插件的总目标是「效果不掉的前提下少花
    token」，而每轮注入是唯一花 token 的地方——在这里省 100 字，
    一天几百轮就是几万字。

    做四件事，都是「删了不丢信息」的：

    1. 去开头主语（「他女朋友在杭州」→「女朋友在杭州」）
    2. 去结尾语气尾巴（「…是浙江人吧」→「…是浙江人」）
    3. 压重复标点和空格
    4. 超长截断：优先在逗号处断，实在没有才硬切

    只删这三类，**时间词、否定词、数字一个都不动**——那些正是记忆的内容。
    """
    body = normalize(text)
    if not body:
        return ""
    stripped = _LEAD_SUBJECT.sub("", body)
    # 剥完变太短说明「他」就是主语本身（如「他在杭州」→「在杭州」还行，
    # 但「他」单独一句会变空），短于 4 字就当没剥
    if len(stripped) >= 4:
        body = stripped
    body = _TAIL_PARTICLE.sub("", body)
    body = _DUP_PUNCT.sub(r"\1", body)
    body = _SPACES.sub(" ", body).strip("，,、；; ")
    if len(body) <= limit:
        return body
    # 截断：在 limit 之前找最后一个断句点，断在 60% 之前就不用（太碎了）
    cut = body[:limit]
    for sep in ("，", "、", "；", "。", ",", ";", " "):
        pos = cut.rfind(sep)
        if pos >= int(limit * 0.6):
            return cut[:pos]
    return cut[: limit - 1] + "…"


def fact_key(text: str) -> str:
    """去重用的归一化键：只留中英文数字，忽略标点和空白。

    「下周三要交论文初稿」和「下周三要交论文初稿。」算同一条；
    但「周三交论文」和「下周三交初稿」不算——那属于措辞不同但确实是进展，
    留给模型自己判断，不做模糊合并（模糊合并容易把两件事并成一件）。
    """
    body = normalize(text).lower()
    return re.sub(r"[\s，。、；：！？,.;:!?~～\"'“”‘’()（）\[\]【】]", "", body)


# 「虚词」表：语气词和程度副词。比对前先剥掉，再算 2-gram。
#
# 为什么需要：中文的 2-gram 对插入词非常敏感——「碧蓝航线练到150级」和
# 「碧蓝航线已经练到150级了」只多两个词，Jaccard 就从 1.0 掉到 0.62，
# 明明是同一句话却被判成两条。
# 只剥语气词和程度副词，**时间词（以前/现在）和否定词（没/不）一个都不动**：
# 那些恰恰是区分「同一件事的两个阶段」的关键。
_FILLER_WORDS = (
    "已经", "正在", "其实", "真的", "确实", "比较", "非常",
    "很", "挺", "太", "更", "最", "都", "也", "还", "又",
    "了", "的", "呢", "吧", "啊", "呀", "嘛", "哦", "呗",
)


def _strip_filler(text: str) -> str:
    body = str(text or "")
    for word in _FILLER_WORDS:
        body = body.replace(word, "")
    return body


def _bigrams(text: str) -> set[str]:
    """字符 2-gram 集合。中文按两字滑窗，英文数字也照切（够用）。"""
    body = re.sub(r"[^\w\u4e00-\u9fff]+", "", _strip_filler(text).lower())
    if not body:
        return set()
    if len(body) < 2:
        return {body}
    return {body[i : i + 2] for i in range(len(body) - 1)}


def similarity(a: str, b: str) -> float:
    """两条记忆的字面相似度，0~1。纯本地计算，不发请求。"""
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def is_same_meaning(a: str, b: str, threshold: float = SIMILAR_THRESHOLD) -> bool:
    """这两条是不是「同一件事的两种说法」。

    **只用本地 2-gram，不调 embedding、不发 LLM 判定。** 参考同类陪伴插件的做法
    （固定档案 + 本地关键词排序，不为了记忆单独调一次向量服务）：省的是每轮的
    真金白银，代价是偶尔漏合并。

    这个取舍是刻意的：漏合并只是多存一条，用户在面板上删掉就行；
    误合并是「它把我说的话记混了」，那比多存一条糟得多。所以阈值卡到 0.72，
    而且长度差太多直接不比——「周三交论文」和「下周三交初稿」过不了这关，
    那本来也可能是一件事的两个阶段，交给用户判断。
    """
    ta = normalize(a)
    tb = normalize(b)
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    # 长短悬殊的不比：2-gram 在这种情况容易假高。
    # 长度按**剥完虚词**的算——否则「已经…了」这种多两个词就被挡在门外
    short, long = sorted((len(_strip_filler(ta)), len(_strip_filler(tb))))
    if short and (long - short) / short > SIMILAR_LEN_RATIO:
        return False
    return similarity(ta, tb) >= threshold


# 只有这几类会跟画像重复。事件/计划（"这周末要改完插件"）本来就是临时的，
# 画像不该写、也不会写，拿它们去比对只会误杀
_PROFILE_OVERLAP_KINDS = (KIND_FACT, KIND_PREFERENCE)


def covered_by_profile(text: str, profile: str, threshold: float = 0.65) -> bool:
    """这条事实是不是已经被画像概括过了。

    为什么需要：画像会把「碧蓝航线练到 150 多级」这类稳定信息写进去，
    事实段里再列一遍就是同一句话付两次钱。这里用 2-gram 包含率判断——
    一条 20 字的事实如果 65% 的二元组都在画像里出现过，基本就是被概括了。

    实测 0.65 的取舍：主人 12 条里判掉 3-5 条（"碧蓝航线150级""英伟达财权"
    "蓝色星原选女角色"这种），留下的是"这周末要改完插件"这类临时事。
    阈值再低就开始误杀具体事了，那些才是接话时最有用的。

    只在**有画像时**调用，判错一条的代价也只是「这条这轮不提」，
    下一轮画像没覆盖它就会回来。
    """
    body = str(text or "").strip()
    head = str(profile or "").strip()
    if not body or not head:
        return False
    bg = _bigrams(body)
    if not bg:
        return False
    return len(bg & _bigrams(head)) / len(bg) >= threshold


def parse_extra_deny(raw: Any) -> str:
    """配置里的追加黑名单，原样传给 ``is_sensitive``。"""
    return str(raw or "").strip()


class FactStore:
    """事实的存取、判重、打分与注入文本。"""

    def __init__(self, plugin):
        self.plugin = plugin
        self.storage = plugin.storage
        self.settings = plugin.settings
        # 归档动作每轮都跑，但只在真的归档了东西时才写盘。
        # 5 秒防抖兜得住，但没必要每次都给写盘任务添一条。
        self._archive_done = 0.0
        try:
            self.migrate_legacy()
        except Exception as e:  # noqa: BLE001 - 迁移失败不该让插件起不来
            logger.warning(f"[群聊记忆] 事实数据迁移失败（本次跳过）: {e}")
        try:
            self.migrate_pending()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[群聊记忆] 待观察补转正失败（本次跳过）: {e}")

    # ── 配置 ────────────────────────────────────────────────────────────

    def _cfg(self) -> dict[str, Any]:
        try:
            return self.settings.group("facts")
        except Exception:  # noqa: BLE001 - 配置读不到就按关闭处理
            return {}

    def enabled(self) -> bool:
        return bool(self._cfg().get("enabled"))

    # ── 迁移与上限 ──────────────────────────────────────────────────────

    def migrate_legacy(self) -> int:
        """把「会话|用户」的旧桶合并成「用户」一个桶。

        合并规则：
        - 同一条事实（``fact_key`` 相同）只留一条，``seen`` 取较大值——
          不用相加：两条各观察过一次的待定事实，加起来会直接转正，
          而它其实只是「同一个人在两个群说了同一件事」，不该算两次确认。
        - ``last_seen`` / ``created`` 取较新的、较早的。
        - 状态取「更靠前」的那个（active > pending > suspect > rejected > archived）：
          在 A 群已经确认过的事，不该因为 B 群那条还在待观察就被降回去。
        - 钉住的只要有一条钉住就算钉住。

        幂等：库里没有带竖线的键就直接返回 0。
        """
        buckets = self.storage.facts
        legacy = [k for k in list(buckets.keys()) if "|" in str(k)]
        if not legacy:
            return 0

        rank = {
            STATUS_ACTIVE: 4, STATUS_PENDING: 3, STATUS_SUSPECT: 2,
            STATUS_REJECTED: 1, STATUS_ARCHIVED: 0,
        }
        moved = 0
        people: set[str] = set()
        for key in legacy:
            raw = buckets.pop(key, None)
            _, _, sender_id = str(key).partition("|")
            if not sender_id or not isinstance(raw, list):
                continue
            target = buckets.get(sender_id)
            merged: dict[str, dict] = {}
            for item in list(target or []) + raw:
                if not isinstance(item, dict):
                    continue
                k = fact_key(str(item.get("text") or ""))
                if not k:
                    continue
                exist = merged.get(k)
                if not exist:
                    merged[k] = dict(item)
                    continue
                exist["seen"] = max(_as_int(exist.get("seen")), _as_int(item.get("seen")))
                exist["last_seen"] = max(
                    _as_float(exist.get("last_seen")), _as_float(item.get("last_seen"))
                )
                created = [
                    v for v in (_as_float(exist.get("created")), _as_float(item.get("created"))) if v
                ]
                if created:
                    exist["created"] = min(created)
                if rank.get(str(item.get("status")), 0) > rank.get(str(exist.get("status")), 0):
                    exist["status"] = item.get("status")
                if item.get("pinned"):
                    exist["pinned"] = True
                if _as_int(item.get("importance"), 3) > _as_int(exist.get("importance"), 3):
                    exist["importance"] = item.get("importance")
            # 编号重排：两边的编号会撞（都从 1 开始），/忘掉 3 会删错条
            items = sorted(
                merged.values(),
                key=lambda i: _as_float(i.get("created"), 0),
            )
            for idx, item in enumerate(items, start=1):
                item["id"] = str(idx)
            buckets[sender_id] = items
            people.add(sender_id)
            moved += 1

        if moved:
            try:
                self.storage._mark("facts")
            except Exception:  # noqa: BLE001
                pass
            logger.info(
                f"[群聊记忆] 事实记忆已合并到 QQ 号：{len(legacy)} 个旧桶 → {len(people)} 个人"
            )
        return moved

    def migrate_pending(self) -> int:
        """生效门槛默认从 2 降到 1，把当时卡在「待观察」的存量补转正。

        这些条目的 ``seen`` 都是 1——它们并不"可疑"，只是当时要求同一件事
        被抽到两次才算数，而群友极少把同一件具体事重复说两遍。
        规则改了，按新规则它们本来第一轮就该生效。

        用户如果把 ``activate_count`` 调回 2 以上，说明他明确要两轮确认，
        这里就不动。幂等：转正过的就不是 pending 了。
        """
        if _as_int(self._cfg().get("activate_count"), 1) > 1:
            return 0
        at = time.time()
        changed = 0
        for key in list(self.storage.facts.keys()):
            items = self.storage.facts.get(key)
            if not isinstance(items, list):
                continue
            touched = False
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("status") != STATUS_PENDING:
                    continue
                if _as_int(item.get("seen"), 1) < 1:
                    continue
                item["status"] = STATUS_ACTIVE
                item["activated_at"] = at
                touched = True
                changed += 1
            if touched:
                self.storage.facts[key] = items
        if changed:
            try:
                self.storage._mark("facts")
            except Exception:  # noqa: BLE001
                pass
            logger.info(
                f"[群聊记忆] 生效门槛改为「抽到即生效」，补转正 {changed} 条待观察事实"
            )
        return changed

    def _is_owner(self, sender_id: str) -> bool:
        """主人判断借关系模块的：识别口径只有一份，不要两处各写一遍。"""
        try:
            bond = getattr(self.plugin, "bond", None)
            return bool(bond and bond.is_owner(sender_id))
        except Exception:  # noqa: BLE001
            return False

    def _stage_of(self, sender_id: str) -> str:
        """这个人当前的好感档位。拿不到就按陌生档。

        关系功能没开 / 这个人还没进 bonds 时都会走兜底——
        存储上限退回基准值，和 1.7.x 的行为一致，不会因为缺档位就崩。
        """
        try:
            bond = getattr(self.plugin, "bond", None)
            if bond is not None:
                st = str(bond.stage_now(sender_id) or "")
                if st in FACT_LIMIT_FACTORS:
                    return st
        except Exception:  # noqa: BLE001
            pass
        return _STAGE_FALLBACK

    def limit_of(self, sender_id: str, *, stage: str | None = None) -> int:
        """这个人最多能存几条事实。按好感档位给，主人恒定最高。

        ``max_per_user`` 是「陌生档」的基准值，其余档按 ``FACT_LIMIT_FACTORS``
        缩放（不待见 ×0.25、熟悉 ×2、亲近 ×3、老友 ×5）。改一个数就能整体
        调松调紧，不用逐档改。

        **这是存储上限，不是注入上限。** 存得多不花 token，注入的那几条才花；
        注入条数由 ``bond.MEMORY_TIERS`` 管。两者分开的好处是：
        关系变淡时先把「提不提」收紧（立刻省 token），
        而「记不记得」可以留着——回头关系回来了，旧事还在。
        """
        cfg = self._cfg()
        base = _as_int(cfg.get("max_per_user"), DEFAULT_MAX_PER_USER)
        if base <= 0:
            base = DEFAULT_MAX_PER_USER
        if self._is_owner(sender_id):
            return max(1, int(round(base * OWNER_FACT_LIMIT_FACTOR)))
        st = stage or self._stage_of(sender_id)
        return max(1, int(round(base * FACT_LIMIT_FACTORS.get(st, 1.0))))

    def keep_target_of(self, sender_id: str, *, stage: str | None = None) -> int:
        """这个人清理后保留几条。

        主人有单独的值（``owner_prune_keep``，默认 150）：他不参与「一键清理」，
        但可以单独点「清理主人记忆」。这两个动作的语义不同——
        一键清理是日常维护，主人不该被动；单独清理是用户主动说「这份太长了」。
        """
        cfg = self._cfg()
        if self._is_owner(sender_id):
            keep = _as_int(cfg.get("owner_prune_keep"), OWNER_PRUNE_KEEP)
            return keep if keep > 0 else OWNER_PRUNE_KEEP
        st = stage or self._stage_of(sender_id)
        return int(PRUNE_KEEP.get(st, PRUNE_KEEP[_STAGE_FALLBACK]))

    # ── 主动清理 ────────────────────────────────────────────────────────

    def _keep_rank(self, item: dict, at: float) -> tuple[int, float]:
        """清理时的保留优先级：先看「上锁」，再按分数。

        排序键是 ``(上锁, 分数)`` 降序：

        1. **钉住 / 边界**无条件排最前。同类产品的做法也是「上锁的核心记忆
           绝对不动」——用户亲手钉的、或亲口说过的边界（「别叫我老弟」），
           一次清理就没了，比记错更伤人。
        2. 其余按 ``_score``（重要性 0.5 + 新鲜度 0.3 + 命中 0.2）从高到低。
           也就是「重要的、最近提过的」留下，「边角料 + 很久没提的」先走。
        """
        locked = 1 if (
            item.get("pinned")
            or str(item.get("kind") or "") in DECAY_EXEMPT_KINDS
        ) else 0
        return (locked, self._score(item, "", at))

    def prune(
        self,
        sender_id: str,
        *,
        keep: int | None = None,
        dry_run: bool = False,
        now: float | None = None,
    ) -> dict[str, Any]:
        """把一个人的记忆瘦身到 ``keep`` 条。返回统计，不改就跑预览。

        超出的标 ``rejected``（留档可恢复），**不删除**。删了用户既看不到
        发生过什么，也没法反悔；标否决之后在面板「已否决」里还在，
        想恢复改个状态就行。
        """
        at = float(now if now is not None else time.time())
        items = self.list_of("", sender_id)
        alive = [i for i in items if i.get("status") != STATUS_REJECTED]
        target = self.keep_target_of(sender_id) if keep is None else int(keep)
        removed: list[dict[str, Any]] = []

        if target > 0 and len(alive) > target:
            ranked = sorted(alive, key=lambda i: self._keep_rank(i, at), reverse=True)
            for item in ranked[target:]:
                removed.append(
                    {
                        "id": item.get("id"),
                        "text": str(item.get("text") or "")[:40],
                        "kind": item.get("kind"),
                        "importance": item.get("importance"),
                    }
                )
                if not dry_run:
                    item["status"] = STATUS_REJECTED
                    item["rejected_at"] = at
                    item["rejected_by"] = "prune"

        if removed and not dry_run:
            self._save("", sender_id, items)
        return {
            "key": str(sender_id),
            "target": target,
            "before": len(alive),
            "after": len(alive) - len(removed),
            "removed": len(removed),
            "items": removed,
        }

    def prune_owner(
        self, *, dry_run: bool = False, now: float | None = None
    ) -> dict[str, Any]:
        """只清理主人的记忆，瘦身到 ``owner_prune_keep`` 条。

        **和一键清理分开的**，不是「顺便带上主人」：

        - 一键清理是日常维护，跳过主人——养得越久记得越多是这类玩法的核心，
          被动清理等于把前面攒的扔掉
        - 这个只在用户**主动**点「清理主人记忆」时跑，语义是
          「我知道主人记了很多，现在确实想收一收」

        保留规则和单人清理完全一致：钉住的、亲口说过的边界先留，
        其余按「重要性 × 新鲜度」。被清的标 rejected 留档。
        """
        at = float(now if now is not None else time.time())
        keep = _as_int(self._cfg().get("owner_prune_keep"), OWNER_PRUNE_KEEP)
        if keep <= 0:
            keep = OWNER_PRUNE_KEEP
        rows: list[dict[str, Any]] = []
        total = 0
        # 扫到几个主人 / 他们现在一共几条。用来区分两种「没清」：
        # 「压根没配主人或没记忆」和「有记忆但没超过保留量」——
        # 只靠 removed=0 分不开，前者会给用户一句误导的提示
        owners_seen = 0
        alive_total = 0
        for key in list(self.storage.facts.keys()):
            sender_id = str(key).strip()
            if not sender_id or "|" in sender_id:
                continue
            if not self._is_owner(sender_id):
                continue
            owners_seen += 1
            alive_total += len(
                [i for i in self.list_of("", sender_id) if i.get("status") != STATUS_REJECTED]
            )
            res = self.prune(sender_id, keep=keep, dry_run=dry_run, now=at)
            total += res["removed"]
            if res["removed"]:
                rows.append(
                    {
                        "key": sender_id,
                        "target": keep,
                        "before": res["before"],
                        "after": res["after"],
                        "removed": res["removed"],
                    }
                )
        if total:
            logger.info(
                f"[群聊记忆] 主人记忆清理：{'预计' if dry_run else '已'}标记否决 {total} 条"
                f"（保留 {keep} 条/人）"
            )
        return {
            "people": len(rows),
            "removed": total,
            "keep": keep,
            "owner_count": owners_seen,
            "alive": alive_total,
            "rows": rows,
            "dry_run": bool(dry_run),
        }

    def purge_archived(
        self, sender_id: str, *, dry_run: bool = False, now: float | None = None
    ) -> int:
        """真正删掉归档太久的条目。返回删了几条。

        归档的条目不注入、不花 token，留着只是给用户一个反悔期。
        过了 ``ARCHIVE_KEEP_DAYS``（180 天）还没被重新提起，就没必要留了——
        再留着只会让 facts.json 越来越大。
        """
        at = float(now if now is not None else time.time())
        items = self.list_of("", sender_id)
        cutoff = at - ARCHIVE_KEEP_DAYS * 86400
        kept = []
        dropped = 0
        for item in items:
            if item.get("status") == STATUS_ARCHIVED:
                when = _as_float(item.get("archived_at"), _as_float(item.get("last_seen")))
                if when and when < cutoff:
                    dropped += 1
                    continue
            kept.append(item)
        if dropped and not dry_run:
            self._save("", sender_id, kept)
        return dropped

    def prune_all(
        self, *, dry_run: bool = False, now: float | None = None
    ) -> dict[str, Any]:
        """全局一键清理：按每个好感档位各自的保留条数瘦身。

        **主人跳过**：他的记忆不清。养得越久记得越多是这类玩法的核心，
        主人这条线上一旦开始「记得多了反而被删」，前面攒的就白费了。
        他的 token 由注入配额兜（每轮 ≤10 条 / 500 字），和存了多少条无关。

        顺手把归档超过 180 天的真删掉——那部分不占 token，只占文件大小。
        """
        at = float(now if now is not None else time.time())
        rows: list[dict[str, Any]] = []
        total_removed = 0
        purged = 0
        skipped_owner = 0

        for key in list(self.storage.facts.keys()):
            items = self.storage.facts.get(key)
            if not isinstance(items, list) or not items:
                continue
            sender_id = str(key).strip()
            if not sender_id or "|" in sender_id:
                # 未迁移的旧桶不动：迁移逻辑会处理，这里插手只会打架
                continue
            if self._is_owner(sender_id):
                skipped_owner += 1
                continue
            stage = self._stage_of(sender_id)
            res = self.prune(sender_id, dry_run=dry_run, now=at)
            gone = self.purge_archived(sender_id, dry_run=dry_run, now=at)
            purged += gone
            total_removed += res["removed"]
            if res["removed"] or gone:
                rows.append(
                    {
                        "key": sender_id,
                        "stage": stage,
                        "target": res["target"],
                        "before": res["before"],
                        "after": res["after"] - gone,
                        "removed": res["removed"],
                        "purged": gone,
                    }
                )

        rows.sort(key=lambda r: -r["removed"])
        if total_removed or purged:
            logger.info(
                f"[群聊记忆] 一键清理：{len(rows)} 人 · 标记否决 {total_removed} 条"
                f" · 删除超期归档 {purged} 条 · 跳过主人 {skipped_owner} 个"
                + ("（预览，未写盘）" if dry_run else "")
            )
        return {
            "people": len(rows),
            "removed": total_removed,
            "purged": purged,
            "skipped_owner": skipped_owner,
            "rows": rows[:40],
            "dry_run": bool(dry_run),
        }

    # ── 手动整理合并 ────────────────────────────────────────────────────

    def merge_duplicates(self, sender_id: str | None = None, dry_run: bool = False) -> dict:
        """把「同一件事的不同说法」合并成一条。``sender_id`` 为空就全库跑。

        入库时其实已经在做相似判定了（``is_same_meaning``），但判定是**逐条
        对历史**做的，而且阈值卡在 0.72 偏保守——漏合并的那些会一直躺在库里。
        这个方法是给用户的一把扫帚：把现在库里所有近似条目两两过一遍。

        合并规则（保守，宁可少合）：

        - 保留「更熟」的那条当主：``seen`` 多的优先，再比长度
        - ``seen`` 相加、``importance`` 取大、``last_seen`` 取新、
          ``variants`` 合并（各留最近 3 条）
        - 被合掉的那条标 ``archived`` + ``merged_into``：不算消失，
          在「已归档」里还能查到，180 天后才清

        ``dry_run`` 只算不写，面板可以先问一句「会合并几条」。
        """
        keys = [sender_id] if sender_id else [k for k in list(self.storage.facts.keys())]
        now = time.time()
        rows: list[dict] = []
        total = 0
        for key in keys:
            if not key or "|" in str(key):
                continue
            items = self.list_of("", key)
            alive = [
                i
                for i in items
                if i.get("status") not in (STATUS_REJECTED, STATUS_ARCHIVED)
                and str(i.get("text") or "").strip()
            ]
            if len(alive) < 2:
                continue
            # 先熟的排前面：熟的那条当主，合并后不会出现「合并完反而不熟了」
            alive.sort(
                key=lambda i: (
                    _as_int(i.get("seen"), 1),
                    _as_int(i.get("importance"), 3),
                    len(str(i.get("text") or "")),
                ),
                reverse=True,
            )
            kept: list[dict] = []
            dropped: list[tuple[dict, dict]] = []
            # 手动整理用的阈值比入库低一档（0.72 → 0.62）：
            # 入库时误合并的代价是「它把我说的话记混了」且用户不知道；
            # 这里是用户主动点的，而且合并后原文进 variants 看得见。
            # 放宽一点才扫得掉「同一件事换个说法」那种真重复。
            loose = max(0.55, SIMILAR_THRESHOLD - 0.10)
            for item in alive:
                text = str(item.get("text") or "")
                hit = None
                for k in kept:
                    if is_same_meaning(text, str(k.get("text") or ""), threshold=loose):
                        hit = k
                        break
                if hit is None:
                    kept.append(item)
                else:
                    dropped.append((item, hit))

            if not dropped:
                continue
            total += len(dropped)
            rows.append({"key": key, "merged": len(dropped)})
            if dry_run:
                continue
            for item, target in dropped:
                target["seen"] = _as_int(target.get("seen"), 1) + _as_int(item.get("seen"), 1)
                target["importance"] = max(
                    _as_int(target.get("importance"), 3), _as_int(item.get("importance"), 3)
                )
                target["last_seen"] = max(
                    _as_float(target.get("last_seen"), 0), _as_float(item.get("last_seen"), 0)
                )
                variants = list(target.get("variants") or [])
                for alt in [item.get("text"), *(item.get("variants") or [])]:
                    alt = str(alt or "").strip()
                    if alt and alt != str(target.get("text") or "") and alt not in variants:
                        variants.append(alt)
                target["variants"] = variants[-3:]
                item["status"] = STATUS_ARCHIVED
                item["archived_at"] = now
                item["merged_into"] = target.get("id")
            self._refresh_tiers(items, key, now)
            self._save("", key, items)

        if total and not dry_run:
            logger.info(f"[群聊记忆] 整理合并：{len(rows)} 人 · 合并 {total} 条")
        return {
            "people": len(rows),
            "merged": total,
            "rows": rows[:40],
            "dry_run": bool(dry_run),
        }

    # ── 遗忘 ────────────────────────────────────────────────────────────

    def _day_setting(self, key: str, fallback: int) -> float:
        """读一个天数配置。读不到/填坏了就用兜底值，不让它把注入搞崩。"""
        try:
            value = float(self.settings.get(key) or fallback)
        except Exception:  # noqa: BLE001
            return float(fallback)
        return max(1.0, value)

    def shallow_days(self) -> float:
        """浅层多久忘光。面板可调（默认 15 天）。"""
        return self._day_setting("fact_shallow_days", DECAY_DAYS)

    def keep_ratio(self, days: float) -> float:
        """保留率 ``1-(t/T)²``，T = 浅层遗忘天数（默认 15）。

        为什么不用指数：``e^(-kt)`` 永远到不了 0，会出现「一年没提还觉得挺熟」。
        平方曲线精确经过「T/2 剩 75%、T 归零」两个点，观感上也是越往后忘得越快。

        为什么读配置而不是常量：群里的事该记多久，是用户自己的口味——
        闲聊群半个月就该翻篇，熟人小群三个月还记得才正常。
        """
        span = self.shallow_days()
        if days <= 0:
            return 1.0
        if days >= span:
            return 0.0
        return max(0.0, 1.0 - (days / span) ** 2)

    def archive_stale(self, sender_id: str, now: float | None = None) -> int:
        """把太久没提到的事实归档。返回归档条数。

        只动 ``active``：待观察/疑似记错的本来就不注入，归档它们没意义，
        还会把「疑似记错」这个待人工确认的状态抹掉。

        boundary 和 pinned 不动：前者是一直有效的约定，后者是用户亲手钉的。
        """
        at = float(now if now is not None else time.time())
        items = self.list_of("", sender_id)
        if not items:
            return 0
        changed = 0
        for item in items:
            if item.get("status") != STATUS_ACTIVE:
                continue
            if item.get("pinned"):
                continue
            if str(item.get("kind") or "") in DECAY_EXEMPT_KINDS:
                continue
            # 核心 / 深层不归档：它们本来就该忘得慢。
            # 遗忘对这两档是**降档**（由 _refresh_tiers 按 idle 天数压下来），
            # 降到浅层之后才轮到这里的「浅层归零 → 归档」。
            # 不然「核心」和「浅层」的区别只剩一个排序系数，等于没做
            if tier_of(item) in (TIER_CORE, TIER_DEEP):
                continue
            last = _as_float(item.get("last_seen"), _as_float(item.get("created"), at))
            if self.keep_ratio(max(0.0, (at - last) / 86400.0)) > 0:
                continue
            item["status"] = STATUS_ARCHIVED
            item["archived_at"] = at
            changed += 1
        if changed:
            self._save("", sender_id, items)
        return changed

    # ── 存取 ────────────────────────────────────────────────────────────

    @staticmethod
    def bucket_of(umo: str, sender_id: str) -> str:
        """桶键 = **只有 QQ 号**，和关系同一套识别口径。

        为什么不再带会话：同一个人在这群说过「周三要交报告」，在那个群也提过，
        分成两个桶之后，在 A 群记住的事到了 B 群就不认识了——用户视角是
        「它怎么又忘了」。事实本来就是关于「这个人」的，不是关于「这个群里的这个人」。

        ``umo`` 参数留着只是为了不改十几处调用点，实际不参与键。
        要按群区分的是风格学习（那边用 learn_allowed 管），不是事实。
        """
        return str(sender_id or "").strip()

    def list_of(self, umo: str, sender_id: str) -> list[dict]:
        """取这个人的全部事实。返回的是副本，改完要调 ``save``。"""
        raw = self.storage.facts.get(self.bucket_of(umo, sender_id))
        if not isinstance(raw, list):
            return []
        return [dict(x) for x in raw if isinstance(x, dict)]

    def _save(self, umo: str, sender_id: str, items: list[dict]) -> None:
        self.storage.facts[self.bucket_of(umo, sender_id)] = items
        self.storage._mark("facts")

    # ── 入库 ────────────────────────────────────────────────────────────

    def ingest(
        self,
        umo: str,
        sender_id: str,
        candidates: list[dict],
        history: list[dict] | None = None,
    ) -> dict[str, int]:
        """把抽取出来的候选并入库。返回各项统计，供日志与面板用。

        任何一条不合格都只是「这一条不记」，不影响其他条，也不抛异常——
        事实记忆是增强项，不能成为学习链路的新故障点。
        """
        stats = {"added": 0, "merged": 0, "activated": 0, "dropped": 0, "sensitive": 0}
        if not self.enabled() or not candidates:
            return stats

        try:
            cfg = self._cfg()
            activate_at = max(1, int(cfg.get("activate_count") or 2))
            extra_deny = parse_extra_deny(cfg.get("deny_patterns"))
            items = self.list_of(umo, sender_id)
            by_key = {fact_key(i.get("text", "")): i for i in items}
            pool = "\n".join(
                str(m.get("content") or "") for m in (history or [])
            )
            now = time.time()

            for cand in candidates:
                if not isinstance(cand, dict):
                    stats["dropped"] += 1
                    continue
                text = normalize(cand.get("text"))
                if not text or len(text) > MAX_FACT_CHARS:
                    stats["dropped"] += 1
                    continue
                # 敏感信息直接丢，不计数也不留痕：连「丢过一条」本身都不该被记下
                if is_sensitive(text, extra_deny):
                    stats["sensitive"] += 1
                    continue
                # 证据校验：候选必须能在本轮原话里找到出处，找不到就是模型编的
                evidence = normalize(cand.get("evidence"))
                if evidence and pool and evidence not in pool:
                    stats["dropped"] += 1
                    continue

                kind = str(cand.get("kind") or KIND_FACT).strip()
                if kind not in KINDS:
                    kind = KIND_FACT
                importance = _as_int(cand.get("importance"), 3)
                importance = max(1, min(5, importance))
                key = fact_key(text)
                exist = by_key.get(key)
                # 精确没命中，再比一次「同一件事的另一种说法」。
                #
                # 只跟「活着的」条目比：已否决/已归档的相似条目不该被复活——
                # 用户删掉的东西，不该因为模型换个说法又冒出来。
                # 阈值见 ``is_same_meaning``：卡在 0.72，宁可漏合并也不误合并。
                if not exist:
                    for old_item in items:
                        if old_item.get("status") in (STATUS_REJECTED, STATUS_ARCHIVED):
                            continue
                        if is_same_meaning(text, old_item.get("text", "")):
                            exist = old_item
                            key = fact_key(old_item.get("text", ""))
                            stats["similar"] = stats.get("similar", 0) + 1
                            break

                if exist:
                    exist["seen"] = _as_int(exist.get("seen"), 1) + 1
                    exist["last_seen"] = now
                    # 同一件事的另一种说法：留档到 variants（最多 3 条），
                    # 并把正文换成信息量更大的那句。
                    #
                    # 为什么留档：模糊合并万一合错了，用户得能看出「原来还说过这个」，
                    # 而不是只看到一句话就认定 bot 记混了。
                    old_text = str(exist.get("text") or "")
                    if text != old_text and old_text:
                        variants = list(exist.get("variants") or [])
                        if old_text not in variants:
                            variants.append(old_text)
                        exist["variants"] = variants[-3:]
                        if len(text) > len(old_text):
                            exist["text"] = text
                    # 被纠正过的条目即便再次出现也不自动复活：
                    # 复活的判据是「人明确否认过」，不是「模型又抽到一次」
                    if (
                        exist.get("status") == STATUS_PENDING
                        and exist["seen"] >= activate_at
                    ):
                        exist["status"] = STATUS_ACTIVE
                        stats["activated"] += 1
                    if importance > _as_int(exist.get("importance"), 3):
                        exist["importance"] = importance
                    stats["merged"] += 1
                    continue

                record = {
                    "id": self._next_id(items),
                    "text": text,
                    "kind": kind,
                    "importance": importance,
                    "evidence": normalize(cand.get("evidence"))[:60],
                    "created": now,
                    "last_seen": now,
                    "seen": 1,
                    "status": STATUS_ACTIVE if activate_at <= 1 else STATUS_PENDING,
                    "last_inject": 0,
                    "hits": 0,
                    "pinned": False,
                    # 新增的一律先当「浅层」，之后由 _refresh_tiers 按熟练度
                    # 往上放——第一次听到的事本来就只是「有点印象」
                    "tier": TIER_SHALLOW,
                    "variants": [],
                }
                if record["status"] == STATUS_ACTIVE:
                    stats["activated"] += 1
                items.append(record)
                by_key[key] = record
                stats["added"] += 1

            if any(stats.values()):
                # 先按门槛重算核心 / 一般，再裁超量条数——
                # 顺序反了会把刚升成核心的条目在下一步裁掉
                self._refresh_tiers(items, sender_id, now)
                self._trim(items, self.limit_of(sender_id))
                self._save(umo, sender_id, items)
            return stats
        except Exception as e:  # noqa: BLE001 - 入库失败不能影响学习
            logger.warning(f"[群聊记忆] 事实入库失败（本次跳过）: {e}")
            return stats

    @staticmethod
    def _next_id(items: list[dict]) -> str:
        """续编号。不重用被删掉的号，避免 /忘掉 按编号删错人。"""
        top = 0
        for item in items:
            raw = str(item.get("id") or "")
            m = re.search(r"(\d+)$", raw)
            if m:
                top = max(top, int(m.group(1)))
        return f"f_{top + 1:04d}"

    # ── 记忆分档 ────────────────────────────────────────────────────────

    def core_quota_of(self, sender_id: str, *, stage: str | None = None) -> int:
        """这个人的核心记忆名额。关系越近越多，主人另算。"""
        if self._is_owner(sender_id):
            return OWNER_CORE_QUOTA
        st = stage or self._stage_of(sender_id)
        return int(CORE_QUOTA.get(st, CORE_QUOTA[_STAGE_FALLBACK]))

    def proficiency(self, item: dict, now: float | None = None) -> float:
        """这条记忆的**熟练度**（0~100）。分档、排序、面板标记都用它。

        三个来源，权重照「复述次数最重要」来分：

        - **提到次数 55%**：``min(seen,4)/4``。1 次 → 13.75 分，4 次 → 55 分。
          封顶在 4 次是因为第 5 次之后再涨已经没有信息量了。
        - **重要性 30%**：模型给的 1~5。5 分的事一次就该比 2 分的记得牢——
          实际数据里「只提过一次但很重要」的事不少（生日、忌讳、口头约定），
          全塞浅层等于让它们跟随口一句同等待遇。
        - **新鲜度 15%**：走遗忘曲线。这一项**开根号**再乘——
          不这么压一下的话，归零那一刻会把所有东西一起拖到浅层，
          而「核心/深层」本该忘得比浅层慢。

        结果对照（都是新鲜时）：
        1 次 + 重要性 3 → 47.5 分（浅层）；1 次 + 重要性 4 → 52.75（深层）；
        2 次 + 重要性 3 → 60.5（深层）；3 次 + 重要性 3 → 74（核心）。
        和两档时代的「seen≥3 升核心」基本对齐，只是中间多了一层。
        """
        at = time.time() if now is None else float(now)
        seen = max(1, _as_int(item.get("seen"), 1))
        importance = max(1, min(5, _as_int(item.get("importance"), 3)))
        kind = str(item.get("kind") or "")
        if kind in DECAY_EXEMPT_KINDS or item.get("pinned"):
            fresh = 1.0
        else:
            days = max(
                0.0,
                (at - _as_float(item.get("last_seen"), _as_float(item.get("created"), at)))
                / 86400.0,
            )
            fresh = self.keep_ratio(days) ** 0.5
        score = 0.55 * (min(seen, 4) / 4.0) + 0.30 * (importance / 5.0) + 0.15 * fresh
        return round(max(0.0, min(1.0, score)) * 100, 1)

    def _auto_tier(self, prof: float) -> str:
        if prof >= PROF_CORE:
            return TIER_CORE
        if prof >= PROF_DEEP:
            return TIER_DEEP
        return TIER_SHALLOW

    def _refresh_tiers(
        self, items: list[dict], sender_id: str, now: float
    ) -> int:
        """按熟练度重算三档。返回变了多少条。

        规则顺序有讲究：

        1. **手动调过的不动**（``tier_manual``）。用户点了升/降一级，
           就不该被下一轮自动逻辑改回去。
        2. **其余一律按熟练度重算**（不看旧值）。这样老数据的 ``normal``
           也会自然归位，不需要单独的迁移脚本。
        3. **核心超名额时按熟练度从低到高降为深层**。手动设的核心不参与
           排序，但占名额——用户亲手点上去的那几条必须留着。

        只在入库和注入前跑，不起定时任务——和衰减同一个思路。
        """
        alive = [
            i
            for i in items
            if i.get("status") not in (STATUS_REJECTED, STATUS_ARCHIVED)
        ]
        if not alive:
            return 0

        changed = 0
        quota = self.core_quota_of(sender_id)
        # 三档寿命都读配置（面板可调，默认 15 / 30 / 60）
        deep_days = self._day_setting("fact_deep_days", DEEP_IDLE_DAYS)
        core_days = self._day_setting("fact_core_days", CORE_IDLE_DAYS)
        if core_days < deep_days:
            core_days = deep_days

        for item in alive:
            if item.get("tier_manual"):
                continue
            prof = self.proficiency(item, now)
            target = self._auto_tier(prof)
            # 掉档上限：熟练度是「提到过几次」的函数，它不会随时间掉，
            # 所以光看熟练度的话，一条 3 次的老记忆能一直挂在核心档。
            # 这里叠一道「多久没提」的硬上限，让三档的寿命有阶梯：
            # 浅层 N 天归零 → 深层 M 天 → 浅层 → 核心 K 天 → 深层。
            last = _as_float(item.get("last_seen"), _as_float(item.get("created"), now))
            idle_days = max(0.0, (now - last) / 86400.0)
            cap = (
                TIER_CORE
                if idle_days <= deep_days
                else TIER_DEEP
                if idle_days <= core_days
                else TIER_SHALLOW
            )
            if TIER_RANK.get(target, 0) > TIER_RANK.get(cap, 0):
                target = cap
            if tier_of(item) != target:
                item["tier"] = target
                changed += 1

        # 超名额降级：手动核心不参与排序，但占名额
        auto = [
            i for i in alive if tier_of(i) == TIER_CORE and not i.get("tier_manual")
        ]
        manual_count = sum(
            1 for i in alive if tier_of(i) == TIER_CORE and i.get("tier_manual")
        )
        room = max(0, quota - manual_count)
        if len(auto) > room:
            auto.sort(
                key=lambda i: (self.proficiency(i, now), _as_float(i.get("last_seen"), 0)),
                reverse=True,
            )
            for item in auto[room:]:
                item["tier"] = TIER_DEEP
                changed += 1
        return changed

    def refresh_all(self, now: float | None = None) -> int:
        """把全库档位按当前熟练度重算一遍。返回变了多少条。

        什么时候需要单独跑：老数据（两档时代的 ``normal``）在没被注入、
        没入库的情况下不会自己归位，面板上就会看到一堆「浅层」但熟练度
        已经够核心的条目。面板打开时跑一次，成本是几千条内存计算，可以忽略。
        """
        at = time.time() if now is None else float(now)
        changed = 0
        for key in list(self.storage.facts.keys()):
            if not key or "|" in str(key):
                continue
            items = self.list_of("", key)
            if not items:
                continue
            n = self._refresh_tiers(items, key, at)
            if n:
                self._save("", key, items)
                changed += n
        return changed

    def set_tier(
        self, sender_id: str, fact_id: str, direction: int = 0
    ) -> dict[str, Any] | None:
        """手动升降一条记忆。

        ``direction``：``+1`` 升一级、``-1`` 降一级、``0`` 交回自动判定。

        手动调过的一律打 ``tier_manual``：之后自动逻辑跳过它。
        否则用户刚升上去，下一轮 ``_refresh_tiers`` 看熟练度不够又降回去，
        用户会觉得按钮没生效。传 0 就是清掉这个锁，让它重新跟着熟练度走。
        """
        items = self.list_of("", sender_id)
        for item in items:
            if str(item.get("id")) != str(fact_id):
                continue
            if direction == 0:
                item.pop("tier_manual", None)
                item["tier"] = self._auto_tier(self.proficiency(item))
            else:
                rank = TIER_RANK.get(tier_of(item), 0)
                rank = max(0, min(len(TIER_ORDER) - 1, rank + (1 if direction > 0 else -1)))
                item["tier"] = TIER_ORDER[rank]
                item["tier_manual"] = True
            item["tier_changed_at"] = time.time()
            self._save("", sender_id, items)
            return item
        return None

    def set_pinned(self, sender_id: str, fact_id: str, pinned: bool) -> dict | None:
        """钉住 / 取消钉住一条事实。

        钉住的意义和核心档不同：核心是「按熟练度自动判定」，钉住是
        **用户亲手说这条必须留着**。所以它绕开自动逻辑——不淘汰（``_trim``）、
        不归档（``archive_stale``）、注入时加分（``_score`` +1.0），
        面板里也排在最前。手动升降档能改档位，但改不掉这个标记。
        """
        items = self.list_of("", sender_id)
        for item in items:
            if str(item.get("id")) != str(fact_id):
                continue
            item["pinned"] = bool(pinned)
            item["pinned_at"] = time.time() if pinned else 0
            self._save("", sender_id, items)
            return item
        return None

    def _trim(self, items: list[dict], limit: int) -> None:
        """超出上限时淘汰低分条目：标 rejected 而不是删除，留档可恢复。"""
        if limit <= 0:
            return
        alive = [i for i in items if i.get("status") != STATUS_REJECTED]
        if len(alive) <= limit:
            return
        now = time.time()
        scored = sorted(alive, key=lambda i: self._score(i, "", now))
        for item in scored[: len(alive) - limit]:
            if item.get("pinned"):
                continue  # 钉住的不淘汰
            # 核心 / 深层也不淘汰：超额时应该由 _refresh_tiers 先按名额和
            # 熟练度把它降档，再参与这里的排序——直接在这里砍会和「核心名额」
            # 互相打架。实际淘汰的永远是浅层里最不熟的那几条
            if tier_of(item) in (TIER_CORE, TIER_DEEP):
                continue
            item["status"] = STATUS_REJECTED
            item["rejected_at"] = now

    # ── 打分与注入 ──────────────────────────────────────────────────────

    def _score(self, fact: dict, query: str, now: float) -> float:
        """排序分。三块：重要性、新鲜度、字面相关；再扣掉刚注入过的。"""
        importance = _as_int(fact.get("importance"), 3) / 5.0
        kind = str(fact.get("kind") or KIND_FACT)

        # 边界类永不衰减：它不是「发生过的事」，是「一直有效的约定」。
        # 其余走同一条平方曲线（T/2 剩 75%、T 天归零，T = 浅层天数）——
        # 原来这里是线性 ``1-days/30``，第 29 天还剩 3%，和「几乎忘了」对不上。
        if kind in DECAY_EXEMPT_KINDS:
            fresh = 1.0
        else:
            days = max(0.0, (now - _as_float(fact.get("last_seen"), now)) / 86400.0)
            fresh = self.keep_ratio(days)

        match = 0.0
        terms = [t for t in re.split(r"[\s，。、；：！？,.;:!?]+", str(query or "")) if len(t) >= 2]
        if terms:
            body = str(fact.get("text") or "")
            hit = sum(1 for t in terms if t in body)
            match = hit / len(terms)

        score = 0.3 * importance + 0.2 * fresh + 0.5 * match
        # 档位加权：核心压过深层、深层压过浅层。
        # 核心 +0.35 相当于「多命中半个关键词」——够把核心顶上去，
        # 又不至于让核心把当轮真正相关的那条挤掉。深层取一半，形成梯度
        rank = TIER_RANK.get(tier_of(fact), 0)
        if rank >= 2:
            score += 0.35
        elif rank == 1:
            score += 0.15
        if kind == KIND_BOUNDARY:
            score += 1.0
        if fact.get("pinned"):
            score += 1.0
        # 刚注入过的事实降权：同一句话连着三轮挂在 prompt 里，模型会当成口头禅
        if now - _as_float(fact.get("last_inject"), 0) < 3600:
            score -= 0.6
        return score

    def _compose_body(
        self, picked: list[dict], budget: int
    ) -> tuple[str, list[dict]]:
        """把选中的记忆拼成注入正文。返回 ``(正文, 实际用到的条目)``。

        三档的读法不一样，拼法也不一样：

        - **核心逐条列**，前面挂一行「熟记的」——这几条是最不能记错的
        - **深层逐条列**，挂「熟悉的」
        - **浅层按类合并**（``- 偏好：A、B、C``）。10 条 30 字的记忆并成 3 行，
          字数少一半，模型也更容易看出「这几条是同一类事」

        只有一档时**不加小标题**：单独一段还挂个标题纯属浪费字。
        浅层条数少于 ``SUMMARY_MIN_ITEMS`` 也不合并——3 条并 1 行省不了几个字，
        还把可读性弄丢了。

        每条正文先过一遍 ``condense``（去主语、去语气尾巴、超长截断）。
        这件事放在**注入时**做而不是入库时做：原文要留着给面板看、给用户改，
        注入的 prompt 只求短。
        """
        if not picked or budget <= 0:
            return "", []
        core = [i for i in picked if tier_of(i) == TIER_CORE]
        deep = [i for i in picked if tier_of(i) == TIER_DEEP]
        shallow = [i for i in picked if tier_of(i) == TIER_SHALLOW]
        try:
            turn_on = bool(self._cfg().get("inject_summary", True))
        except Exception:  # noqa: BLE001
            turn_on = True
        merge = turn_on and len(shallow) >= SUMMARY_MIN_ITEMS
        multi = sum(1 for g in (core, deep, shallow) if g) > 1

        segs: list[tuple[str, list[dict]]] = []

        def push_tier(items: list[dict], title: str) -> None:
            if not items:
                return
            if multi:
                segs.append((f"\n【{title}】", []))
            for fact in items:
                segs.append((f"\n- {condense(fact.get('text'))}", [fact]))

        push_tier(core, "熟记的")
        push_tier(deep, "熟悉的")

        if shallow:
            if merge:
                buckets: dict[str, list[dict]] = {}
                for fact in shallow:
                    kind = str(fact.get("kind") or KIND_FACT)
                    buckets.setdefault(kind, []).append(fact)
                if multi:
                    segs.append(("\n【有点印象的】", []))
                for kind, group in buckets.items():
                    label = KIND_LABEL.get(kind, "事")
                    text = "、".join(condense(f.get("text")) for f in group)
                    segs.append((f"\n- {label}：{text}", group))
            else:
                push_tier(shallow, "有点印象的")

        body = ""
        used: list[dict] = []
        for text, group in segs:
            if len(body) + len(text) <= budget:
                body += text
                used.extend(group)
                continue
            if len(group) > 1:
                # 合并段放不下：退化成逐条，能塞几条算几条——
                # 整段丢掉会一次损失七八条，退化成逐条只损失末尾那几条
                for fact in group:
                    one = f"\n- {condense(fact.get('text'))}"
                    if len(body) + len(one) > budget:
                        break
                    body += one
                    used.append(fact)
                continue
            # 逐条段放不下就停：后面只会更长
            break
        return body, used

    def build_text(
        self,
        umo: str,
        sender_id: str,
        *,
        query: str = "",
        now: float | None = None,
        max_items: int | None = None,
        max_chars: int | None = None,
    ) -> str | None:
        """拼本轮要注入的事实段。没有可注入内容返回 None。

        ``max_items`` / ``max_chars`` 是关系档位传进来的上限（见
        ``BondStore.memory_tier``）：跟陌生人只提一条，跟主人能提十条。
        传 None 就按配置走，等于不分档。两个值都还会被模块级硬顶夹一次——
        档位表是代码里的，配置是用户填的，两边都不能越过硬顶。
        """
        if not self.enabled():
            return None
        try:
            at_pre = float(now if now is not None else time.time())
            # 先重算档位，再结算遗忘——顺序不能反。
            # 反过来的话，一条 130 天没提的核心还挂在核心档，会被
            # 「核心不归档」这条规则一直保护着，永远退不了场。
            # 现在的顺序是：先按 idle 把它压成浅层，再走「浅层归零 → 归档」。
            # 两件事都放在读取路径上（不起定时任务），插件停机那段时间也照样算。
            raw_items = self.list_of(umo, sender_id)
            if self._refresh_tiers(raw_items, sender_id, at_pre):
                self._save(umo, sender_id, raw_items)
            if self.archive_stale(sender_id, at_pre):
                raw_items = self.list_of(umo, sender_id)
            items = []
            for item in raw_items:
                if item.get("status") != STATUS_ACTIVE:
                    continue
                # 边界和钉住的永不因为"久没提"退场，它们的价值不随时间变
                if (
                    item.get("pinned")
                    or str(item.get("kind") or "") in DECAY_EXEMPT_KINDS
                ):
                    items.append(item)
                    continue
                # 核心 / 深层也不受「快忘光」那道硬门槛限制：
                # 它们本来就该忘得慢，退出靠的是上面的 idle 降档
                if tier_of(item) in (TIER_CORE, TIER_DEEP):
                    items.append(item)
                    continue
                last = _as_float(item.get("last_seen"), _as_float(item.get("created"), at_pre))
                if self.keep_ratio(max(0.0, (at_pre - last) / 86400.0)) < MIN_INJECT_KEEP:
                    continue
                items.append(item)
            # 画像独立于事实：事实全归档了，画像还该在。
            # 所以这里不急着 return，先看有没有画像可注
            at = float(now if now is not None else time.time())
            cfg = self._cfg()
            want = max(1, min(MAX_INJECT_FACTS, int(cfg.get("max_inject") or MAX_INJECT_FACTS)))
            limit = min(MAX_INJECT_CHARS, int(cfg.get("inject_max_chars") or MAX_INJECT_CHARS))
            # 档位值优先于配置值：分档就是「按关系给不同的量」，
            # 再让配置压一刀的话，老友档那 8 条永远拿不到。
            # 两者都只受模块级硬顶约束（配置写 99999 也超不过它）。
            if max_items is not None and max_items > 0:
                want = max(1, min(MAX_INJECT_FACTS, int(max_items)))
            if max_chars is not None and max_chars > 0:
                limit = min(MAX_INJECT_CHARS, int(max_chars))

            picked = sorted(items, key=lambda i: self._score(i, query, at), reverse=True)[:want]

            # 画像里已经概括过的事，事实段不再重复列（省一遍字数）。
            # 放在挑完之后做：先按相关度选好，再剔除被画像覆盖的，
            # 这样不会因为剔除而漏掉本来该排在后面的相关条目
            profile_body = ""
            try:
                profile = getattr(self.plugin, "profile", None)
                if profile is not None:
                    profile_body = profile.body_of(sender_id) or ""
            except Exception:  # noqa: BLE001 - 画像坏了不该拖累事实段
                profile_body = ""
            if profile_body:
                covered = [
                    i
                    for i in picked
                    if str(i.get("kind") or KIND_FACT) in _PROFILE_OVERLAP_KINDS
                    and covered_by_profile(i.get("text"), profile_body)
                ]
                if covered:
                    picked = [i for i in picked if i not in covered]
                    # 剔掉之后还有名额，就用后面的补上，别让这轮白白变少
                    rest = [i for i in items if i not in picked and i not in covered]
                    rest.sort(key=lambda i: self._score(i, query, at), reverse=True)
                    for extra in rest[: len(covered)]:
                        if str(extra.get("kind") or KIND_FACT) in _PROFILE_OVERLAP_KINDS and covered_by_profile(
                            extra.get("text"), profile_body
                        ):
                            continue
                        picked.append(extra)

            header = "<facts>\n你记得关于这个人的事："
            footer = "\n</facts>"
            budget = limit - len(header) - len(footer)

            body, used = self._compose_body(picked, budget)

            if not body and not profile_body:
                return None

            self._mark_injected(umo, sender_id, used, at)
            head = header
            if profile_body:
                head += "\n【画像】" + profile_body
            if body:
                if profile_body:
                    head += "\n【具体的事】"
                head += body
            return head + footer
        except Exception as e:  # noqa: BLE001 - 注入失败不能影响回复
            logger.debug(f"[群聊记忆] 构建事实段失败（跳过本段）: {e}")
            return None

    def _mark_injected(
        self, umo: str, sender_id: str, facts: list[dict], now: float
    ) -> None:
        """记下这次注入了哪几条，供纠正检测和降权使用。"""
        if not facts:
            return
        try:
            wanted = {str(f.get("id") or "") for f in facts}
            items = self.list_of(umo, sender_id)
            changed = False
            for item in items:
                if str(item.get("id") or "") in wanted:
                    item["last_inject"] = now
                    item["hits"] = _as_int(item.get("hits"), 0) + 1
                    changed = True
            if changed:
                self._save(umo, sender_id, items)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[群聊记忆] 记录注入痕迹失败（可忽略）: {e}")

    # ── 诚实出口与纠正 ──────────────────────────────────────────────────

    def has_active(self, umo: str, sender_id: str) -> bool:
        return any(
            i.get("status") == STATUS_ACTIVE for i in self.list_of(umo, sender_id)
        )

    def mark_corrected(self, umo: str, sender_id: str, window: float = CORRECTION_WINDOW) -> int:
        """用户说「不对」时，把刚用过的那条事实标成疑似记错。

        **只标一条，且不标 boundary**：

        - 一次「不对」是对某一件事的否认。把刚注入的三条全作废，
          等于用一句话清空这个人的历史，之后 bot 会什么都不记得。
        - boundary 是用户**亲口说过的边界**（"别叫我老弟"）。它比一句
          情绪化的「不对」更可靠，标掉它的后果是 bot 真去叫他老弟——
          这比记错一件事更糟。

        连说几次「不对」会逐条标记：标掉的那条不再是 active，
        下一次就轮到剩下那条。
        """
        try:
            now = time.time()
            items = self.list_of(umo, sender_id)
            candidates = [
                i
                for i in items
                if i.get("status") == STATUS_ACTIVE
                and str(i.get("kind") or "") != KIND_BOUNDARY
                and not i.get("pinned")
                and now - _as_float(i.get("last_inject"), 0) <= window
            ]
            if not candidates:
                return 0
            target = max(candidates, key=lambda i: _as_float(i.get("last_inject"), 0))
            target["status"] = STATUS_SUSPECT
            target["suspect_at"] = now
            self._save(umo, sender_id, items)
            logger.info(
                f"[群聊记忆] 用户纠正，标记为疑似记错：{target.get('text')}"
            )
            return 1
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[群聊记忆] 纠正检测失败（可忽略）: {e}")
            return 0

    # ── 维护 ────────────────────────────────────────────────────────────

    def remember(
        self, umo: str, sender_id: str, text: str, kind: str = KIND_FACT, importance: int = 3
    ) -> dict | None:
        """手动记一条，直接生效。

        不要求观察两次——这是用户明确要求记的，再让他等两轮没有道理。
        但敏感信息过滤照走：手动记一条验证码出来，同样是事故。
        """
        try:
            body = normalize(text)
            if not body or len(body) > MAX_FACT_CHARS:
                return None
            cfg = self._cfg()
            if is_sensitive(body, parse_extra_deny(cfg.get("deny_patterns"))):
                return None
            if kind not in KINDS:
                kind = KIND_FACT
            now = time.time()
            items = self.list_of(umo, sender_id)
            key = fact_key(body)
            for item in items:
                if fact_key(item.get("text", "")) == key:
                    # 已经记过：直接转生效，不重复添加
                    item["status"] = STATUS_ACTIVE
                    item["last_seen"] = now
                    self._save(umo, sender_id, items)
                    return item

            record = {
                "id": self._next_id(items),
                "text": body,
                "kind": kind,
                "importance": max(1, min(5, int(importance or 3))),
                "evidence": "（手动添加）",
                "created": now,
                "last_seen": now,
                "seen": max(1, int(cfg.get("activate_count") or 2)),
                "status": STATUS_ACTIVE,
                "last_inject": 0,
                "hits": 0,
                "pinned": True,  # 手动的默认钉住，不参与自动淘汰
            }
            items.append(record)
            self._trim(items, self.limit_of(sender_id))
            self._save(umo, sender_id, items)
            return record
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[群聊记忆] 手动记事实失败: {e}")
            return None

    def forget(self, umo: str, sender_id: str, keyword: str = "") -> int:
        """按关键词或编号删事实。空关键词等于全清这个人的。"""
        try:
            items = self.list_of(umo, sender_id)
            if not items:
                return 0
            token = normalize(keyword)
            if not token:
                self._save(umo, sender_id, [])
                return len(items)
            kept, removed = [], 0
            for item in items:
                if token == str(item.get("id") or "") or token in str(
                    item.get("text") or ""
                ):
                    removed += 1
                    continue
                kept.append(item)
            if removed:
                self._save(umo, sender_id, kept)
            return removed
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[群聊记忆] 删除事实失败: {e}")
            return 0

    def clear_all(self, umo: str = "") -> int:
        """清空事实。给了 umo 只清这个会话。"""
        try:
            if not umo:
                count = sum(len(v) for v in self.storage.facts.values() if isinstance(v, list))
                if self.storage.facts:
                    self.storage.facts.clear()
                    self.storage._mark("facts")
                return count
            prefix = f"{umo}|"
            keys = [k for k in self.storage.facts if str(k).startswith(prefix)]
            count = 0
            for key in keys:
                value = self.storage.facts.pop(key, None)
                if isinstance(value, list):
                    count += len(value)
            if keys:
                self.storage._mark("facts")
            return count
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[群聊记忆] 清空事实失败: {e}")
            return 0

    def summary(self, umo: str, sender_id: str) -> dict[str, Any]:
        """给 /记得 和面板用的只读视图。"""
        items = self.list_of(umo, sender_id)
        counts = {s: 0 for s in STATUS_LABEL}
        for item in items:
            status = str(item.get("status") or STATUS_PENDING)
            counts[status] = counts.get(status, 0) + 1
        return {
            "enabled": self.enabled(),
            "total": len(items),
            "active": counts.get(STATUS_ACTIVE, 0),
            "pending": counts.get(STATUS_PENDING, 0),
            "suspect": counts.get(STATUS_SUSPECT, 0),
            "rejected": counts.get(STATUS_REJECTED, 0),
            "archived": counts.get(STATUS_ARCHIVED, 0),
            # 这个人最多能存几条，面板上要显示「10 / 20」才知道还剩多少额度
            "limit": self.limit_of(sender_id),
            "items": items,
        }

    @staticmethod
    def format_summary(info: dict[str, Any]) -> str:
        """把 summary 渲染成一条给人看的消息（/记得 用）。"""
        if not info.get("total"):
            return "还没有记住关于你的事。多聊几次，或者直接告诉我「记一下：……」"
        lines = [
            f"关于你的记忆：共 {info['total']} 条"
            f"（生效 {info.get('active', 0)} · 待观察 {info.get('pending', 0)}"
            f" · 疑似记错 {info.get('suspect', 0)}）"
        ]
        for item in info.get("items") or []:
            mark = {
                STATUS_ACTIVE: "·",
                STATUS_PENDING: "…",
                STATUS_SUSPECT: "?",
                STATUS_REJECTED: "×",
            }.get(str(item.get("status") or ""), "·")
            kind = KIND_LABEL.get(str(item.get("kind") or KIND_FACT), "事实")
            lines.append(f"{mark} [{item.get('id')}] {kind}｜{item.get('text')}")
        lines.append("删掉某一条：/忘掉 <编号或关键词>")
        return "\n".join(lines)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

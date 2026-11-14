"""配置读取层。

设计要点：
- AstrBot 的 ``AstrBotConfig`` 在 WebUI 里改配置后是实时可读的，所以这里**不缓存**配置值，
  每次 ``get`` 都现读，天然支持热更新。
- 面板上允许热调的几个运行参数写进 ``runtime.json``，优先级高于插件配置；
  这样调试时不必为了改一个阈值去重启整个 AstrBot，也不会污染正式配置。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from astrbot.api import logger

# 代码里的兜底默认值。schema 缺失、老配置迁移、字段被删时都能安全回退。
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "inject_enabled": True,
    # ── 全局：主人 QQ ────────────────────────────────────────────────────
    # 以前填在 bond.owner_ids 里，但「主人」其实影响四个模块：
    # 关系（不封顶 + 记忆配额最高）、事实（上限 300）、学习（私聊默认开启）、
    # 清理（跳过一键清理）。藏在关系分组里，用户根本不知道还要去那儿找。
    # 现在提到顶层，一处生效。旧的 bond.owner_ids 仍然兼容（见 owners()）。
    "owner_ids": "",
    # ── 学习库：群聊 / 私聊分开管 ────────────────────────────────────────
    # 群聊默认学：进群就学是这个插件的主功能，默认关等于装了个寂寞
    "learn_group_enabled": True,
    # 私聊默认不学：私聊是私域，默认把别人一对一聊天送进模型提炼，边界上不合适。
    # 只有主人自己的私聊例外（见 hooks.learn_allowed），
    # 以及用户手动给某个私聊开过的会话。
    "learn_private_enabled": False,
    "analysis_interval_seconds": 21600,
    # 攒够这么多条新消息才送提炼。10 条太容易学到「嗯」「哈」这类噪音，
    # 而且和 runtime.json 里的实际值（30）不一致——面板显示 10、实际跑 30，
    # 排查问题时最容易在这里绕圈
    "min_history_for_analysis": 30,
    "max_messages_per_learn": 80,
    # ── 学习链路（1.4.0）────────────────────────────────────────────────
    # accumulate = 增量累积（新链路，upsert + 权重 + 时效 + 审核）
    # replace    = 1.3.x 旧行为（每轮整表重写），出问题时一键回滚
    "learn_mode": "accumulate",
    # 新增多少条消息就学一次。比「固定 6 小时一大锅」更接近持续学习，
    # 也只处理新消息，token 更省。
    "learn_increment_messages": 30,
    # 两次学习的最小间隔，防止群里刷屏时高频烧钱
    "learn_min_interval": 1800,
    # 每轮候选上限。只是候选数，不等于库存上限
    "learn_candidate_max": 12,
    # 候选必须给出本轮聊天的原文证据，对不上就丢弃（防 LLM 编造）
    "learn_require_evidence": True,
    # 入库自检：让 LLM 判断候选是不是「可复用的说话习惯」
    "learn_self_check": True,
    # 新条目权重到多少才生效（10 = 被观察到一次，20 = 跨两轮确认）
    "trait_activate_count": 2,
    # 注入的最低有效权重，低于这个不进 prompt
    "trait_inject_min_count": 2,
    # 时效折扣：多少天后权重见底
    "trait_decay_days_full": 30,
    # 多少天未活跃可归档（归档不删数据，面板可恢复）
    "trait_archive_days": 30,
    # 每个会话在库条数上限，超出按有效权重从低到高归档。
    # 原来是 300：但注入每轮只用 5 条，295 条永远进不了 prompt，
    # 只让学习 prompt 和面板列表越来越长。150 条够攒很久了
    "trait_max_total": 150,
    # 注入时按维度分组渲染（语气/节奏/互动/话题/黑话），比一行逗号拼接更像人
    "style_group_render": False,
    # true = 新条目必须人工批准才生效；false = 自检通过 + 权重达标自动生效
    "review_require_manual": False,
    "max_stable_inject": 5,
    # 手动记忆的注入名额，和 max_stable_inject 各算各的：
    # 否则手工加满 5 条后，6 小时一次的学习成果一条都进不了注入。
    "max_manual_inject": 3,
    "enable_situational_inject": True,
    "max_situational_inject": 2,
    "situational_similarity_threshold": 0.4,
    "enable_cross_group": False,
    "max_global_styles": 3,
    "enable_emb_selection": True,
    "embedding_provider_id": "",
    "analysis_provider_id": "",
    "history_limit_per_session": 200,
    # 场景化表达匹配：先按触发词命中，再退回包含判断，最后才考虑向量
    "enable_trigger_matching": True,
    # 向量请求跑在回复链路上，给个硬超时，避免 embedding 服务慢时拖住回复
    "embedding_timeout_seconds": 2.0,
    # 特征数量上限：防止长期运行后 prompt 跟着特征一起膨胀
    "max_stable_traits": 40,
    "max_situational_traits": 20,
    "session_whitelist": [],
    "session_blacklist": [],
    "blocked_users": [],
    "debug_mode": False,
    # 启动后多久允许学习。原来 600 秒，但**每次重启都会重置**——
    # 频繁重启（改配置、更新插件）的部署里，等于永远学不上。
    # 300 秒足够等群消息缓过来，又不至于让重启变成学习黑洞
    "startup_learn_delay_seconds": 300,
    # ── 事实记忆：三档遗忘节奏 ──────────────────────────────────────────
    # 都是「多久没再提到」的天数。三档阶梯：浅层最短、核心最长。
    #
    # 默认 15 / 30 / 60（原来是 30 / 60 / 120）：群聊里的事本来就该忘得快，
    # 半个月没人提的细节留着只会挤占注入名额；真正重要的事会被反复提到，
    # 提到就重置计时，所以「缩短」并不会把该记的忘掉。
    "fact_shallow_days": 15,
    "fact_deep_days": 30,
    "fact_core_days": 60,
    # ── 个人画像（按 QQ 号）─────────────────────────────────────────────
    # 从事实里提炼「这个人是谁」，按好感档位给字数（陌生 60 → 老友 180，主人 260）。
    # 默认开：不待见档直接不生成，陌生人只有 60 字，成本可控；
    # 关掉等于回到「只有事实、没有画像」。
    "profile_enabled": True,
    # 至少攒够这么多条事实才写第一版画像。太少写出来是复述，没有提炼价值
    "profile_min_facts": 3,
    # 两次重写的最小间隔（小时）。画像本来就该稳定，天天改写会来回抖
    "profile_rewrite_hours": 24,
    # 每天最多重写几个人的画像。一次学习可能同时触发好几个人，
    # 这个上限把「一天最坏开销」钉死
    "profile_daily_cap": 6,
    # ── 风格强度与人格让位 ──────────────────────────────────────────────
    # 0 = 完全不注入风格（纯人格）；100 = 全力体现。分档见 injector.scale_quota
    "style_strength": 100,
    # 主动插话默认压到 60 —— 默认阈值 100 下，主动回复永远不会推翻人格
    "active_style_strength": 60,
    # 强度【严格大于】这个值才允许让位。默认 100 配合默认强度 100 等于「开箱不让位」，
    # 保持与 1.2.0 一致：满额注入风格，但冲突时人格优先。想让风格赢就把它调低。
    "style_override_threshold": 100,
    # 让位时把声明同时追加到 system_prompt。效果更硬，但该轮 DeepSeek 前缀缓存必失效
    "style_override_deep": False,
    # ── 回复字数上限 ────────────────────────────────────────────────────
    # 0 = 不限制。被动默认 0 是为了不改变 1.2.0 的现有行为
    "reply_max_chars": 0,
    "active_reply_max_chars": 80,
    # 硬截断默认关：会切坏 markdown、链接和表情占位符，比啰嗦更难看
    "reply_max_chars_hard": False,
    # ── 主动回复 ────────────────────────────────────────────────────────
    "active_reply_enabled": False,
    # probability / rule / llm_judge；后两者更容易触发，靠冷却和日限压制
    "active_reply_mode": "probability",
    "active_reply_chance": 0.15,
    "active_reply_cooldown_seconds": 1800,
    "active_reply_daily_limit": 8,
    "active_reply_quiet_hours": "01:00-08:00",
    "active_reply_min_messages": 3,
    "active_reply_judge_stack": 8,
    "active_reply_backoff_limit": 3,
    "active_reply_keywords": [],
    "active_reply_guidance": (
        "这是一条由你主动发起的群聊回复，没有人向你提问。\n"
        "不要使用「有什么可以帮你」「我在」「已收到」这类服务式或召唤式开头；\n"
        "不要提及自己是主动发言或任何机制；\n"
        "像普通群友一样简短接话；话题不适合接就少说或不说。"
    ),
    "active_reply_provider_id": "",
    # ── 联网搜索 ────────────────────────────────────────────────────────
    # inherit = 完全交给 AstrBot（插件不计数、不限额）；plugin = 插件挂工具并限流；off = 全摘
    "web_search_mode": "plugin",
    "web_search_provider": "deepseek",
    "web_search_api_key": "",
    # 插件 key 为空时自动读 AstrBot 全局同家 key，让 key 只存一处
    "web_search_reuse_global_key": True,
    "web_search_max_results": 5,
    "web_search_timeout": 8,
    # 开了每条结果带整页正文，一次可能几万 token，成本翻十倍
    "web_search_need_content": False,
    # 每群每天的搜索次数上限，跨群独立计算，按日期天然归零
    "web_search_daily_limit": 20,
    "web_search_max_per_turn": 2,
    "web_search_hint": "",
    # ── 管理员 ──────────────────────────────────────────────────────────
    "admin_extra_users": [],
    "admin_tool_enabled": False,
    # ── 计价（元 / 百万 token，DeepSeek 官方牌价）──────────────────────
    "token_price_input_cached": 0.05,
    "token_price_input_miss": 1.5,
    "token_price_output": 4.5,
    "token_price_peak_multiplier": 2.0,
    "token_price_peak_enabled": True,
    "token_stats_keep_days": 7,
    # 求助/说正事的轮次收起玩梗和亲昵称呼。
    # 默认开——它只做减法，不会引入任何新行为，也不会多花 token。
    "serious_mode": True,
}

# ── 分组配置（schema 里 type=object 的那些）──────────────────────────────
#
# 为什么要分组：这些是「一整块功能」的参数，扁平和已有的 60 多项混在一起，
# 面板会变成一条两百行的清单。分组后 WebUI 里是可折叠的一块，
# 默认只占一行，展开才看到细节——既不冗余，每一项又都还能调。
#
# 读法和扁平配置不同：``settings.group("bond")`` 返回合并后的整组 dict，
# 组内字段按这里的默认值做类型兜底。
GROUP_DEFAULTS: dict[str, dict[str, Any]] = {
    "bond": {
        # 关系状态注入。默认关：新功能一律不改变老用户现有行为
        "enabled": False,
        # 每次加多少亲密度（千分制）。群聊里会自动减半，私聊按全额。
        # 注意这只是**基准**，实际增量还要乘两个系数（见下面两项）：
        # 被动轮 ×0.25、指数曲线（越高越难）。
        # 群聊被动一轮在 base=0 时约 1.25 点，比原来的 5 点慢四倍
        "growth_per_turn": 10.0,
        # 被动轮的增长系数。所谓被动轮就是「别人来找 bot，bot 回复」——
        # 群里最常见的交互。0.25 = 只给四分之一，防止刷几句话就熟
        "passive_growth_factor": 0.25,
        # 指数曲线：base 到这个值时增速减半，再翻一倍再减半。
        # 300 意味着 0→300 还算顺，300→600 要两倍轮次，600 以上很难。
        # 设 0 关闭（退回线性增长）
        "growth_curve_scale": 300.0,
        # 好感变化冷却（秒）。同一人在这段时间内只判一次（涨或跌）。
        # 900 = 15 分钟：一个话题聊完算一次交互，而不是每句都算。
        # 注意这只是「多久判一次」，能不能判还取决于 bot 有没有真的回复
        "growth_cooldown": 900,
        # 熟悉 / 亲近 / 老友 三档门槛（亲密度点数）。
        # 只写两个值时第三档按第二档的 1.6 倍自动补，兼容 1.6.x 的老配置
        "level_thresholds": "200,500,800",
        # 群聊封顶在「熟悉」。关掉后群聊也能一路到「老友」——
        # 一个 bot 在群里对五十个人都「很熟」，那是客服不是亲近
        "group_cap_familiar": True,
        # 允许用 /称呼 给 bot 指定叫法
        "nickname_enabled": True,
        # 主人 QQ。**已移到顶层全局配置**（DEFAULTS["owner_ids"]），
        # 这里只作为老配置的兼容回退：顶层为空时还会读这里（见 Settings.owners）。
        # 新配置请改顶层那一项——它同时管关系 / 事实 / 学习 / 清理
        "owner_ids": "",
        # 追加到关系段末尾的自定义句子，留空则不加
        "relationship_note": "",
        # 关系段字数上限。只在「记忆按档位分配」关掉时生效
        "inject_max_chars": 150,
        # 负好感：被明确骂一句会扣分，扣到 0 以下进「不待见」档
        "negative_enabled": True,
        # 每次负向扣多少。群聊减半，下限 -100。
        # 和正向的 10 对齐：骂一句正好抵消一次正常互动，负向才有体感。
        # 原来是 6，扣完一句好话就补回来了
        "negative_per_hit": 10.0,
        # 每天净增上限，0 = 不限。
        #
        # 原来默认 0（不限）有个洞：15 分钟冷却只管「多久一次」，不管「一天多少次」，
        # 24 小时理论上能刷到 480 点——一天从陌生到老友。
        # 默认 80 的来由：参考同类养成插件（dsh-feiyu-affinity 每天 +8/100 = 8%），
        # 折到千分制就是 80 点。折成交互次数：群聊 16 次 / 私聊 8 次，
        # 正常聊天够用，刷屏刷不出好感。
        # 每天最多涨多少（千分制的点数）。这是**最硬的闸门**：
        # 冷却和指数曲线管「涨多快」，这个管「一天最多涨多少」——
        # 防止有人连聊一整天把关系一次刷到老友。
        # 80 点 ≈ 一天最密集也只能从 0 到「熟悉」的四成
        "daily_gain_cap": 80,
        # 按好感档位分配记忆量：越熟注入越多，陌生人只给一点点
        "tiered_memory": True,
    },
    "facts": {
        # 事实记忆。默认关：它记的是用户私事，属于「默认不该发生」的一类
        "enabled": False,
        # 同一条事实被独立观察到几次才转正。1 = 抽到就生效（不建议）
        # 观察几次才生效。默认 1：抽到即生效。
        #
        # 原来是 2（同一件事要被独立抽到两次）。实测下来这个门槛让功能等于没开：
        # 39 条事实里 seen 全是 1——群友聊一件具体事很少重复第二遍，
        # 结果一条都没转正，注入永远是空的。
        #
        # 真正的防编造靠的是另一道闸：候选的 evidence 必须能在本轮原话里找到，
        # 找不到直接丢。两轮确认是叠在这之上的保险，代价大于收益。
        "activate_count": 1,
        # 每人保留条数上限，超出按分数淘汰（标 rejected 留档，不删）。
        # 这是「陌生」档的基准，实际按好感档位缩放（主人 ×15 = 300）
        "max_per_user": 20,
        # 主人单独清理时保留几条。上限 300 → 清到 150，正好砍一半边角料，
        # 又不至于把聊了半年的记忆清空（一天记 1~2 条的话，150 条约等于 3~5 个月）。
        # 「一键清理」仍然完全跳过主人，这个值只给「单独清理主人」用
        "owner_prune_keep": 150,
        # 每轮注入几条、总共多少字
        "max_inject": 3,
        "inject_max_chars": 200,
        # 追加的敏感词黑名单，逗号或换行分隔
        "deny_patterns": "",
        # 注入时把同类一般记忆合并成一行（「- 偏好：A、B、C」）。
        # 条数 ≥5 才生效，3 条并成 1 行省不了字还把可读性弄丢了。
        # 纯本地拼接，不调 LLM、不发请求——省的是「逐条平铺」的那部分字数
        "inject_summary": True,
        # 诚实出口：对方明确在问「你还记得吗」但确实没记忆时，
        # 注入一句「没有相关记忆，不要编」，而不是让模型自由发挥
        "honest_fallback": True,
    },
    "tone": {
        # 去模型腔：删「作为一个 AI」「希望这对你有帮助」，破折号换逗号。
        # 只做删除和标点替换，所以默认开
        "clean": True,
        # 压连续标点（！！！→！）。会改变表达力度，默认关
        "squeeze": False,
        # 去 markdown 标记（**加粗** → 加粗）。代码块和链接不受影响
        "markdown": False,
        # 长回复拆成多条。依赖宿主「分段回复」开关，默认关
        "segment": False,
        # 单条目标字数
        "segment_chars": 60,
        # 最多拆几条，多出来的合并到最后一条，不丢内容
        "segment_parts": 4,
    },
}

# ── 主动回复的五档预设 ──────────────────────────────────────────────────
#
# 一键把 5 个参数写成一整套。原因：这几个参数是**互相咬合**的——
# 只把概率从 0.15 拉到 0.35，冷却还是 30 分钟、日限还是 8 次，
# 实际一句都不会多；反过来只放宽冷却、概率不动，也只是多掷几次骰子。
# 用户要的是「活跃一点」，不是「把某个数改大」，所以给整档切换。
#
# 数值依据（参考同类插件 + 群聊节奏实测）：
# - 安静：1 小时一次、每天 3 次。人多的技术群 / 工作群
# - 平衡：30 分钟一次、每天 8 次。默认值，活跃群大约一小时冒一次泡
# - 活跃：10 分钟一次、每天 20 次。小群、熟人局
# - 高频：5 分钟一次、每天 40 次。话多的群，bot 是常驻成员之一
# - 常驻：3 分钟一次、每天 70 次。熟人小群 / 一对一陪聊群，几乎每次空档都会接
#
# 概率现在只是**基准**：真实概率还会被群热度压低、被接话黏性抬高
# （见 active.py 的动态参与度），所以后面两档在刷屏时也不会连续插话，
# 而且日限永远是最硬的闸门——写得再高也超不过那个数。
ACTIVE_PROFILES: list[dict[str, Any]] = [
    {
        "id": "quiet",
        "label": "🛡 安静",
        "desc": "一小时最多试一次，每天 3 次。人多的群里几乎不打扰",
        "values": {
            "active_reply_chance": 0.05,
            "active_reply_cooldown_seconds": 3600,
            "active_reply_daily_limit": 3,
            "active_reply_min_messages": 5,
            "active_reply_quiet_hours": "01:00-08:00",
        },
    },
    {
        "id": "balanced",
        "label": "⚖️ 平衡",
        "desc": "半小时一次，每天 8 次。默认值，像群里一个不太话多的人",
        "values": {
            "active_reply_chance": 0.15,
            "active_reply_cooldown_seconds": 1800,
            "active_reply_daily_limit": 8,
            "active_reply_min_messages": 3,
            "active_reply_quiet_hours": "01:00-08:00",
        },
    },
    {
        "id": "lively",
        "label": "🔥 活跃",
        "desc": "十分钟一次，每天 20 次。小群里像常驻群友",
        "values": {
            "active_reply_chance": 0.35,
            "active_reply_cooldown_seconds": 600,
            "active_reply_daily_limit": 20,
            "active_reply_min_messages": 2,
            "active_reply_quiet_hours": "02:00-07:00",
        },
    },
    {
        "id": "high",
        "label": "⚡ 高频",
        "desc": "五分钟一次，每天 40 次。话多的群里 bot 是常驻成员（约 6 万 token/天）",
        "values": {
            "active_reply_chance": 0.55,
            "active_reply_cooldown_seconds": 300,
            "active_reply_daily_limit": 40,
            "active_reply_min_messages": 2,
            "active_reply_quiet_hours": "02:00-07:00",
        },
    },
    {
        "id": "resident",
        "label": "🚀 常驻",
        "desc": "三分钟一次，每天 70 次。熟人小群 / 陪聊群（约 10 万 token/天）",
        "values": {
            "active_reply_chance": 0.80,
            "active_reply_cooldown_seconds": 180,
            "active_reply_daily_limit": 70,
            "active_reply_min_messages": 1,
            "active_reply_quiet_hours": "03:00-07:00",
        },
    },
]

# 允许在面板热调、且会写进 runtime.json 的字段。
RUNTIME_KEYS = {
    "enabled",
    "inject_enabled",
    # 事实三档遗忘天数（面板可调，改完下一轮就按新曲线算）
    "fact_shallow_days",
    "fact_deep_days",
    "fact_core_days",
    # 个人画像：整组都能热调（改完下一轮生效，不用重启）
    "profile_enabled",
    "profile_min_facts",
    "profile_rewrite_hours",
    "profile_daily_cap",
    # 全局主人 + 学习库分开关：面板改完即时生效，不用重启
    "owner_ids",
    "learn_group_enabled",
    "learn_private_enabled",
    "analysis_interval_seconds",
    "min_history_for_analysis",
    "max_messages_per_learn",
    "max_stable_inject",
    "max_manual_inject",
    "enable_situational_inject",
    "max_situational_inject",
    "situational_similarity_threshold",
    "enable_cross_group",
    "max_global_styles",
    "enable_trigger_matching",
    "embedding_timeout_seconds",
    "max_stable_traits",
    "max_situational_traits",
    "debug_mode",
    # 学习链路（1.4.0）
    "learn_mode",
    "learn_increment_messages",
    "learn_min_interval",
    "learn_candidate_max",
    "learn_require_evidence",
    "learn_self_check",
    "trait_activate_count",
    "trait_inject_min_count",
    "trait_decay_days_full",
    "trait_archive_days",
    "trait_max_total",
    "style_group_render",
    "review_require_manual",
    # 风格强度与人格让位
    "style_strength",
    "active_style_strength",
    "style_override_threshold",
    "style_override_deep",
    # 字数上限
    "reply_max_chars",
    "active_reply_max_chars",
    "reply_max_chars_hard",
    # 主动回复
    "active_reply_enabled",
    "active_reply_mode",
    "active_reply_chance",
    "active_reply_cooldown_seconds",
    "active_reply_daily_limit",
    "active_reply_quiet_hours",
    "active_reply_min_messages",
    "active_reply_judge_stack",
    "active_reply_backoff_limit",
    "active_reply_keywords",
    "active_reply_guidance",
    "active_reply_provider_id",
    # 联网搜索
    "web_search_mode",
    "web_search_provider",
    "web_search_api_key",
    "web_search_reuse_global_key",
    "web_search_max_results",
    "web_search_timeout",
    "web_search_need_content",
    "web_search_daily_limit",
    "web_search_max_per_turn",
    "web_search_hint",
    # 管理员
    "admin_extra_users",
    "admin_tool_enabled",
    # 计价
    "token_price_input_cached",
    "token_price_input_miss",
    "token_price_output",
    "token_price_peak_multiplier",
    "token_price_peak_enabled",
    "token_stats_keep_days",
}

_BOOL_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, bool)}
_INT_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, int) and not isinstance(v, bool)}
_FLOAT_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, float)}
_LIST_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, list)}
_STR_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, str)}


class Settings:
    """配置读取 + 运行期覆盖。"""

    def __init__(self, config: Any, runtime_path: Path):
        self._config = config
        self._runtime_path = Path(runtime_path)
        self._runtime: dict[str, Any] = self._load_runtime()

    # ── 运行期覆盖 ──────────────────────────────────────────────────────

    def _load_runtime(self) -> dict[str, Any]:
        try:
            if self._runtime_path.exists():
                data = json.loads(self._runtime_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return {k: v for k, v in data.items() if k in RUNTIME_KEYS}
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"[群聊记忆] 读取 runtime.json 失败，忽略: {e}")
        return {}

    async def set_runtime(self, key: str, value: Any) -> Any:
        """热调一个参数，返回规范化后的值。"""
        if key not in RUNTIME_KEYS:
            raise KeyError(f"不支持热调的配置项: {key}")
        value = self._coerce(key, value)
        self._runtime[key] = value
        try:
            self._runtime_path.parent.mkdir(parents=True, exist_ok=True)
            self._runtime_path.write_text(
                json.dumps(self._runtime, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            logger.error(f"[群聊记忆] 保存 runtime.json 失败: {e}")
        return value

    def clear_runtime(self) -> None:
        self._runtime.clear()
        try:
            self._runtime_path.unlink(missing_ok=True)
        except OSError:
            pass

    # ── 读取 ────────────────────────────────────────────────────────────

    def get(self, key: str) -> Any:
        if key in self._runtime:
            return self._runtime[key]
        return self._coerce(key, self._raw_config(key))

    # ── 主人 ────────────────────────────────────────────────────────────

    def owners(self) -> set[str]:
        """所有主人 QQ。**跨功能生效**：关系、事实、学习、清理都用这一份。

        顶层 ``owner_ids`` 优先；为空时回退到 ``bond.owner_ids``——
        1.7.x 之前「主人 QQ」填在关系分组里，老配置不用手改也能继续用。

        逗号、中文逗号、空格都能当分隔符：用户从别处粘贴过来时，
        中英文标点混用是常态，为一个分隔符报「没生效」不值得。
        """
        raw = str(self.get("owner_ids") or "").strip()
        if not raw:
            try:
                raw = str(self.group("bond").get("owner_ids") or "").strip()
            except Exception:  # noqa: BLE001
                raw = ""
        if not raw:
            return set()
        return {x.strip() for x in re.split(r"[,，;；\s]+", raw) if x.strip()}

    # ── 分组配置 ────────────────────────────────────────────────────────

    def group(self, name: str) -> dict[str, Any]:
        """读一整组分组配置，缺失字段用 ``GROUP_DEFAULTS`` 兜底。

        不缓存：WebUI 改完下一轮就是新值，和扁平配置的热更新行为一致。
        这样也能保证「组里新增字段」时，老配置文件不用迁移就能拿到默认值。
        """
        defaults = GROUP_DEFAULTS.get(name)
        if defaults is None:
            return {}
        base = dict(defaults)
        raw = self._raw_group(name)
        for key, default in defaults.items():
            if key in raw and raw[key] is not None:
                base[key] = self._coerce_like(default, raw[key])
        return base

    def _raw_group(self, name: str) -> dict[str, Any]:
        try:
            # AstrBotConfig 是 dict 子类；分组在配置里就是嵌套 dict
            value = self._config.get(name)
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _coerce_like(default: Any, value: Any) -> Any:
        """按默认值的类型转一次，转不动就回退默认值。

        不复用 ``_coerce``：那个靠扁平的 _BOOL_KEYS / _INT_KEYS 判断类型，
        分组字段不在这些集合里，会一路落到原样返回。
        """
        if isinstance(default, bool):
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on", "是"}
            return bool(value)
        if isinstance(default, int):
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                return default
        if isinstance(default, float):
            try:
                return float(value)
            except (TypeError, ValueError):
                return default
        if isinstance(default, list):
            if isinstance(value, str):
                return [x.strip() for x in value.split(",") if x.strip()]
            return list(value or [])
        if isinstance(default, str):
            return "" if value is None else str(value)
        return value

    def groups_snapshot(self) -> dict[str, dict[str, Any]]:
        """全部分组配置的当前值，面板和排查用。"""
        return {name: self.group(name) for name in GROUP_DEFAULTS}

    def _raw_config(self, key: str) -> Any:
        try:
            # AstrBotConfig 是 dict 子类，get 可能返回 None 而不是默认值
            value = self._config.get(key)
        except Exception:
            value = None
        return DEFAULTS[key] if value is None else value

    def _coerce(self, key: str, value: Any) -> Any:
        default = DEFAULTS.get(key)
        try:
            if key in _BOOL_KEYS:
                if isinstance(value, str):
                    return value.strip().lower() in {"1", "true", "yes", "on", "是"}
                return bool(value)
            if key in _INT_KEYS:
                return max(0, int(value))
            if key in _FLOAT_KEYS:
                return float(value)
            if key in _LIST_KEYS:
                if isinstance(value, str):
                    return [x.strip() for x in value.split(",") if x.strip()]
                return list(value or [])
            if key in _STR_KEYS:
                return "" if value is None else str(value)
        except (TypeError, ValueError):
            return default
        return value

    def snapshot(self) -> dict[str, Any]:
        """当前全部生效配置（面板展示用）。"""
        data = {k: self.get(k) for k in DEFAULTS}
        data["_runtime_overrides"] = {k: v for k, v in self._runtime.items()}
        return data

    def is_debug(self) -> bool:
        return bool(self.get("debug_mode"))

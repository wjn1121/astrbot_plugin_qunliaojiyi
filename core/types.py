"""核心数据结构定义。

用 TypedDict 而不是裸 dict，是为了让「特征到底有哪些字段」在代码里有唯一出处：
- 拼错字段名时静态检查能发现（配合 mypy），运行时也不会静默丢数据；
- 面板、学习器、注入器共用同一套定义，不会各写各的；
- 仍是普通 dict，JSON 序列化和旧数据兼容不受影响。
"""

from __future__ import annotations

from typing import Literal, TypedDict

Category = Literal["stable", "situational"]

# 特征生命周期状态：
# pending  新入库，权重还没攒够或还没通过人工审核，不进注入
# active   已生效，参与注入
# archived 权重触底或长期未活跃，保留但不再注入，可人工恢复
# rejected 自检或人工否决，保留原样以便翻案，永不注入
TraitStatus = Literal["pending", "active", "archived", "rejected"]

# 最后一次改动来源：人工改过之后，自动自检不再覆盖
ModifiedBy = Literal["ai", "manual"]

# 特征来源：learned 来自定时学习，manual 由面板手动添加（存在另一个文件里）
# 该字段只做运行期标记，不落库——落库位置本身就是区分依据。
Source = Literal["learned", "manual"]


class Trait(TypedDict, total=False):
    """一条风格特征。

    total=False：旧版数据没有 triggers 字段，读取时按空列表处理。

    1.4.0 起字段语义对齐 MaiBot 的 Expression：
    - ``content`` 对应 style（怎么说），``context`` 对应 situation（什么时候说）
    - ``context`` 为空 = 跨场景稳定风格；非空 = 有触发场景的表达
    - ``proficiency`` 是**原始权重**（只增不减）。实际排序用 ``effective_score``，
      按 ``last_seen`` 做时效折扣，所以不需要后台定期跑衰减任务。
    """

    content: str
    category: Category
    context: str
    triggers: list[str]
    proficiency: int
    confirmed_rounds: int
    last_seen: float
    last_updated: float
    source: Source
    # ── 1.4.0 新增 ──────────────────────────────────────────────────────
    status: TraitStatus
    seen: int
    alias: list[str]
    evidence: list[dict]
    pinned: bool
    dimension: str
    modified_by: ModifiedBy
    first_seen: float


class Evidence(TypedDict, total=False):
    """一条特征的来源原文，面板上点开就能看到"这句话从哪儿学来的"。"""

    text: str
    at: float
    sender: str


class ChatMessage(TypedDict, total=False):
    """待学习的一条聊天记录。"""

    sender: str
    content: str
    timestamp: float


class LearnState(TypedDict, total=False):
    """单个会话的学习元信息（面板展示用）。"""

    last_learn_at: float
    last_ok: bool
    last_stable: int
    last_situational: int
    last_elapsed: float
    last_message: str
    last_message_count: int
    last_chars: int
    total_learns: int
    fail_count: int


class Metrics(TypedDict, total=False):
    """全局累计统计，只用于展示，不参与任何逻辑。"""

    learn_calls: int
    learn_ok: int
    learn_fail: int
    messages_analyzed: int
    chars_analyzed: int
    elapsed_total: float


class ParsedTraits(TypedDict):
    """一次提炼的解析结果。"""

    universal: list[str]
    situational: list[dict]

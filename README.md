# 🧠 群聊记忆 — AstrBot QQ 群聊插件

> 群友聊天时默默记下这个群的说话风格，每 6 小时整批提炼一次，回复时把风格作为临时参考注入——不改系统提示词、不写会话历史，几乎不花钱。

[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.29.1-blue)](https://github.com/AstrBotDevs/AstrBot)
[![Platform](https://img.shields.io/badge/platform-QQ_(aiocqhttp)-green)](#)
[![Version](https://img.shields.io/badge/version-1.1.0-orange)](metadata.yaml)
[![License](https://img.shields.io/badge/license-AGPL--3.0-9cf)](LICENSE)
[![Author](https://img.shields.io/badge/author-wjn1121-lightgrey)](https://github.com/wjn1121)

---

## ⚡ 快速开始

1. **安装插件**：AstrBot WebUI 插件市场搜「群聊记忆」一键安装，或把目录放进 `AstrBot/data/plugins/`
2. **等待积累**：群里正常聊天，插件会把消息攒进缓冲（默认上限 200 条/会话）
3. **自动学习**：启动 10 分钟后首轮检查，之后每 6 小时对攒够 10 条的会话提炼一次
4. **查看效果**：后台 → 插件 → 群聊记忆，能看到每个群学到了什么、这轮会注入什么

无需 API Key，无需额外 pip 依赖，无需修改 AstrBot 配置。

---

## ✨ 功能特性

- 🧠 **低频批量学习**：每 6 小时（可调）对攒够消息的会话做一次整批提炼，攒不够就跳过
- 💬 **零网络注入**：风格走 `extra_user_content_parts` + `mark_as_temp()`，不发请求、不污染提示词、不写历史
- 🎯 **触发词匹配**：学习时为每条梗产出 1–3 个触发词，注入先做本地字符串匹配，命中即不请求向量
- 🛡️ **数字梗护栏**：666 / 233 / 刷屏用语自动降级到场景化通道，不会让模型每句话都硬塞
- 📉 **特征数量上限**：稳定 40 条 / 场景化 20 条，超出按熟练度淘汰，防 prompt 膨胀
- ⏱️ **向量请求硬超时**：默认 2 秒，超时直接降级关键词匹配，embedding 慢不会拖住回复
- 📊 **累计成本可见**：学习次数、成功/失败、分析消息数、字符数、耗时，面板上一目了然
- 🎚️ **会话级开关**：面板里单独关掉某个群的学习或注入，不用去改配置里的会话列表
- ✏️ **特征可编辑**：学到不准的表达可以直接在面板改内容、场景和触发词
- 🕒 **学习记录时间线**：每个群保留最近 20 次学习记录，排查"为什么没学到"更直接
- 🖥️ **可视化面板**：搜索、分页、注入预览、16 项参数热调，改完即时生效
- 🔌 **数据迁移**：支持导入旧格式的 `universal.json` / `chat_history.json`，也能一键导出全部数据
- 📦 **纯本地存储**：5 个 JSON 文件，随时可备份、可清空、可删除

---

## 🎬 效果演示

**注入预览（面板内置，输入一句话就能看这轮会注入什么）：**

```
群友消息：笑死我了 666

注入给模型的内容：
<style_guidelines>
以下是从群聊记录里提炼的说话风格参考：
本群风格：短句连发一句一气泡，语气直白爱下判断、频繁@点名群友互动，
互怼调侃不破防、爱用游戏黑话和二次元代称，圈内梗密度高、常用反问起哄
使用要求：
1. 风格只是参考，不是复读指令；不要为了「像群聊」而每句话都强行加 666、233 或语气词。
2. 只在语境自然合适时使用，优先回应当前用户消息，风格不得覆盖或干扰当前话题。
3. 不要机械堆砌，也不要每条回复都刻意体现全部风格。
</style_guidelines>
```

**查看当前会话学到了什么：**

```
群友: /风格状态
Bot:  当前会话风格：
      稳定风格 6 条
      场景化表达 3 条
      · 短句连发一句一气泡
      · 语气直白爱下判断
      · 互怼调侃不破防
```

**面板总览：**

| 区域 | 内容 |
|------|------|
| 运行状态 | 会话数 / 特征数 / 待学习消息 / 下次学习时间 |
| 累计成本 | 学习次数 / 成功失败 / 分析消息数 / 字符数 / 耗时 |
| 注入预览 | 选会话 + 输入消息 → 直接看注入文本 |
| 会话与特征 | 搜索、分页、逐条编辑/删除、清空、立即学习 |
| 会话开关 | 单独关掉某个群的学习或注入 |
| 学习记录 | 每会话最近 20 次学习结果与耗时 |
| 数据导出 | 一键下载全部数据为 JSON |

---

## 📥 安装方式

### 方式一：插件市场（推荐）

AstrBot WebUI → 插件市场 → 搜索「**群聊记忆**」→ 一键安装

### 方式二：手动安装

```bash
cd AstrBot/data/plugins/
git clone https://github.com/wjn1121/astrbot_plugin_qunliaojiyi.git
```

重载插件即可生效，无需重启 AstrBot。

---

## 🎯 使用方式

### 命令

| 命令 | 作用 |
|------|------|
| `/风格状态` | 查看当前会话学到的特征 |
| `/学习风格` | 立即对当前会话做一次学习（需攒够 10 条） |
| `/清空风格` | 清空当前会话的特征与待学习记录 |

### 自动学习流程

```
群消息 → 攒进缓冲（过滤自己/命令/黑名单）
   ↓
每 6 小时检查一次
   ↓
该会话 ≥ 10 条？ ──否──→ 跳过，不花钱
   ↓ 是
整批提炼（一次 LLM 请求）→ 解析 JSON → 写回特征 → 清空缓冲
   ↓
之后每条回复注入风格文本（零网络请求）
```

---

## ⚙️ 配置项

在 AstrBot WebUI → 插件配置面板中可调：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enabled` | 布尔 | `true` | 总开关，关闭后不收集、不学习、不注入 |
| `inject_enabled` | 布尔 | `true` | 是否把学到的风格注入回复 |
| `analysis_interval_seconds` | 整数 | `21600` | 学习间隔（秒），默认 6 小时 |
| `min_history_for_analysis` | 整数 | `10` | 攒够多少条才学 |
| `max_messages_per_learn` | 整数 | `80` | 单次最多带多少条消息 |
| `max_stable_inject` | 整数 | `5` | 最多注入几条稳定风格 |
| `max_stable_traits` | 整数 | `40` | 每会话稳定风格存储上限 |
| `enable_situational_inject` | 布尔 | `true` | 是否注入场景化表达 |
| `max_situational_inject` | 整数 | `2` | 每轮最多几条场景化表达 |
| `max_situational_traits` | 整数 | `20` | 每会话场景化表达存储上限 |
| `enable_trigger_matching` | 布尔 | `true` | 触发词本地匹配（命中不发向量请求） |
| `situational_similarity_threshold` | 浮点 | `0.4` | 场景化相似度阈值 |
| `embedding_timeout_seconds` | 浮点 | `2` | 向量请求超时，超时降级关键词 |
| `enable_emb_selection` | 布尔 | `true` | 是否用向量辅助匹配场景化表达 |
| `embedding_provider_id` | 字符串 | 空 | 向量 Provider，留空自动取第一个可用 |
| `enable_cross_group` | 布尔 | `false` | 是否参考其他群的稳定风格 |
| `max_global_styles` | 整数 | `3` | 跨群开启时最多注入几条 |
| `analysis_provider_id` | 字符串 | 空 | 学习用 Provider，留空用当前会话默认 |
| `history_limit_per_session` | 整数 | `200` | 每会话缓冲上限 |
| `session_whitelist` | 列表 | `[]` | 只在这些会话学习，留空=全部 |
| `session_blacklist` | 列表 | `[]` | 这些会话不学习 |
| `blocked_users` | 列表 | `[]` | 这些用户的消息不进入学习素材 |
| `debug_mode` | 布尔 | `false` | 日志输出注入/学习全文 |
| `startup_learn_delay_seconds` | 整数 | `600` | 启动后首轮学习延迟 |

> 💡 面板里还能热调 16 个常用参数，写入 `runtime.json`，不改动配置文件，可一键恢复。

---

## 💡 为什么它几乎不花钱

| 环节 | 本插件 | 逐条判断的实现 |
|------|--------|----------------|
| 回复时选风格 | 读本地 JSON，**0 网络请求** | 每条消息调一次 LLM |
| 学习时机 | 每 6 小时整批一次 | 每 10 分钟多任务轮询 |
| 最小触发量 | 攒够 10 条才发请求 | 门槛更低、任务更多 |
| 失败处理 | 不自动重试，等下一轮 | 会重发整包 |
| 场景化匹配 | 触发词本地命中，命中即不请求向量 | 每次请求 embedding |

正常情况下一**天最多 4 次学习请求**，其余时间零消耗。

---

## 📁 目录结构

```
astrbot_plugin_qunliaojiyi/
├── main.py               # 入口：装配、生命周期、命令
├── api.py                # Web API：状态/管理/预览/导入
├── hooks.py              # 事件钩子：消息收集、风格注入
├── utils.py              # 纯工具：prompt 清理、会话名解析
├── constants.py          # 插件常量（名称/作者/版本）
├── metadata.yaml         # 插件元信息（市场显示用）
├── _conf_schema.json     # WebUI 可视化配置定义
├── requirements.txt      # 无额外依赖
├── README.md             # 本文件
├── core/
│   ├── types.py          # 数据结构（TypedDict）
│   ├── settings.py       # 配置读取 + 热调覆盖
│   ├── storage.py        # 持久化：特征/缓冲/学习状态/指标
│   ├── learner.py        # 整批提炼 + JSON 容错 + 梗护栏
│   ├── injector.py       # 稳定风格选择 + 触发词/向量匹配
│   └── manager.py        # 低频调度
└── pages/panel/          # 调试面板（HTML/CSS/JS）
```

---

## 💾 数据说明

数据目录：`AstrBot/data/plugin_data/astrbot_plugin_qunliaojiyi/`

| 文件 | 内容 |
|------|------|
| `universal.json` | 每个会话学到的特征（纯文本，不含原文） |
| `chat_history.json` | 待学习的聊天缓冲（含原文，学完即清空） |
| `learn_state.json` | 每会话学习元信息与最近 20 次记录，删掉可自动重建 |
| `metrics.json` | 全局累计统计 |
| `overrides.json` | 会话级开关（单独关闭的群） |
| `runtime.json` | 面板热调的参数覆盖 |

> ⚠️ `chat_history.json` 会保留未学习的聊天原文，`learn_state.json` 含群号。
> 两者都只在本地，插件不会外传；需要归档时建议加密。

历史数据建议**单独存放在仓库之外**，不要在插件目录里堆积。需要导入时再复制到
`AstrBot/data/plugins/astrbot_plugin_qunliaojiyi/backup/<目录名>/`，导入完即可移除。

---

## 🆕 更新日志

### v1.1.0

- 🎚️ **会话级开关**：每个群可单独关闭「学习」或「注入」，状态存 `overrides.json`
- ✏️ **特征可编辑**：面板直接改内容、场景描述、触发词（改名时自动查重）
- 🕒 **学习记录时间线**：每会话保留最近 20 次学习结果、消息量、耗时
- 📤 **数据导出**：面板一键下载全部数据为 JSON，和导入对称
- 🗂️ 导出接口 `GET /export`，管理接口新增 `set_session_override` / `clear_override` / `update_trait`

### v1.0.0

- 🎉 首个正式版本
- 🧠 低频批量学习：默认 6 小时一次，攒够 10 条才触发
- 💬 零网络注入：`extra_user_content_parts` + `mark_as_temp()`
- 🎯 触发词匹配：命中即不请求向量
- 🛡️ 数字梗护栏：自动降级到场景化通道
- 📉 特征上限：稳定 40 / 场景化 20，按熟练度淘汰
- ⏱️ 向量请求硬超时：2 秒降级关键词
- 📊 累计成本面板
- 🖥️ 面板支持搜索、分页、注入预览、16 项参数热调
- 🔌 支持导入旧格式数据

---

## 🔧 技术栈

- **零外部依赖**：仅使用 AstrBot SDK 内置模块，无需 `pip install`
- **纯本地存储**：JSON 文件，无数据库、无外部 API
- **注入方式**：`extra_user_content_parts` + `mark_as_temp()`，保护 LLM 前缀缓存
- **容错设计**：LLM 输出 JSON 修复回退、embedding 超时降级、单会话失败不影响其他会话
- **代码分层**：入口 / 接口 / 钩子 / 工具 / 数据与算法，`TypedDict` 定义数据结构

---

## 🙋 FAQ

**Q: 为什么 6 小时才学一次，会不会太慢？**
A: 这是刻意的。群聊风格变化很慢，频繁学习只是重复烧钱。觉得慢可以在面板把 `analysis_interval_seconds` 调小，或直接 `/学习风格` 手动触发。

**Q: 面板「累计成本」一直是 0？**
A: 因为还没跑过学习。启动后首轮检查需要等 600 秒，或先在群里发 `/学习风格` 手动触发一次。

**Q: 怎么导入自己的历史数据？**
A: 把 `universal.json` / `chat_history.json` 放进 `backup/<目录名>/`，在面板点「导入备份」即可合并（同名特征保留更高熟练度）。

**Q: 会保存聊天原文吗？**
A: 会，但只保存**尚未学习**的部分（默认每会话最多 200 条），学习完成后立即清空。已学到的只有提炼后的风格描述，不含原文。

**Q: 支持私聊吗？**
A: 代码上支持（按 `unified_msg_origin` 区分会话），但语义上更适合群聊。私聊人少，学不到「群风格」。

**Q: 为什么注入的内容里有「不要强行加 666」？**
A: 这是防「学步」约束。模型容易把学到的梗当成指令，每条回复都硬塞；加约束后只在语境合适时自然接梗。

---

## 🔗 参考资源

| 资源 | 链接 |
|------|------|
| AstrBot 插件开发指南 | https://docs.astrbot.app/dev/star/plugin-new.html |
| 消息事件监听 | https://docs.astrbot.app/dev/star/guides/listen-message-event.html |
| AstrBot 主仓库 | https://github.com/AstrBotDevs/AstrBot |
| 参考项目 astrbot_plugin_oven_multi | https://github.com/huntersxy/astrbot_plugin_oven_multi |

---

## 📞 联系作者

- **QQ 群**：1038882918
- **GitHub**：[@wjn1121](https://github.com/wjn1121)

---

<p align="center">如果这个插件帮你省下了 token 钱，请点个 ⭐ Star 支持一下～</p>

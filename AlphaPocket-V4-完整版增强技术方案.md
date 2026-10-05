# AlphaPocket · 完整版增强技术方案（V4：云端 + IDE 本地双运行时）

| 项 | 内容 |
| --- | --- |
| 产品代号 | **AlphaPocket / 掌上投研** |
| 文档定位 | 在本 task 既有交付（V3 完整设计方案、V3.1 多市场原型、数据层与因子库架构、引擎 A 实施计划）基础上，响应**完整版增强需求（一）**与**本地原型需求（二）**，输出 V4 增强方案：云端 + IDE 本地双运行时 |
| 两大增量 | **（一）云端增强**：响应式 Web/H5、双因子鉴权、DSH 内核多 agent 协作、IDE 本地跑数据（复用 IDE 内嵌大模型 token）、数据版本/检查点/远端同步、云端部署（阿里云/腾讯云）、sandbox 验证。**（二）本地原型**：IDE 内单次运行（computer use 第三方调用/认证/取数）、sandbox 内运行、运行后输出组合/策略/因子完整报告与变更/执行/风险建议 |
| 方法 | 多 agent 协作部分**先调研后评估**（调研结论见 §6.1，来源见附录）；其余按既有架构文档延伸 |
| 关联文档 | 《个人投资引擎-完整设计方案（V3）》《数据层与因子库架构设计》《个人版投资分析引擎-移动端设计方案》《SeekAlpha拆解与个人投资引擎再设计》《中长期因子挖掘与回测-实施计划》 |

> **一句话定位**：云端是"随身投研台"（看盘/复核/确认），IDE 本地是"深度研究所"（挖因子/跑回测/长任务）——两者共享同一套**版本化数据资产**与**agent 工具协议（MCP）**，通过 clone ↔ push 同步。

---

# 第 0 部分 · 回溯：既有资产与 V4 增量映射

## 0.1 既有资产盘点（本 task 已交付）

| 资产 | 内容 | V4 中的角色 |
| --- | --- | --- |
| 《完整设计方案 V3》 | 数据层选型（AKShare 主/Baostock 校验/yfinance 辅）、双引擎（A 中长期因子 + B 20日信号）、多市场组合（A/H/US/基金/多币种）、五维评价多市场适配 | 业务域权威口径，V4 不改动业务语义，只做**运行时与交付形态**增强 |
| 《数据层与因子库架构设计》 | 六面四层架构、Factor Registry、DSL 三引擎、Strategy DSL、接口契约、P0–P3 优先级 | 后端核心蓝本；V4 新增**本地运行时适配层**与**版本同步面** |
| prototype-v2.html（v3.1） | 多市场高保真原型（分市场市值条、引擎 B 信号卡、基金体检卡、A/H/US 评级切换） | V4 响应式 Web 的**移动端视觉基线**，向 PC 断点扩展 |
| 《引擎 A 实施计划》 | M0–M7 里程碑（PIT 数据底座→注册中心→检验→治理→回测） | 本地运行时承载的**核心计算负载**，M0–M2 可直接在 IDE sandbox 内先行 |

## 0.2 需求映射表（（一）（二）→ 本文章节）

| # | 需求 | 章节 |
| --- | --- | --- |
| 一.1 | Web 版 + H5，自适应手机/PC | §2、§8 |
| 一.2 | 手机登录/鉴权 + 双因子 | §3、§9 |
| 一.3 | 研究所多 agent（DSH 内核），协作先调研再评估 | §6（产品）+ §11（技术） |
| 一.4 | IDE（Trae/opencode/codex）本地跑数据，复用 IDE 内嵌大模型 token，单次 + 自动化定制任务 | §5、§12 |
| 一.5 | 组合/策略/因子版本 + checkpoint，远端管理，clone 到本地执行后同步回远端 | §5.3、§13 |
| 一.6 | 服务端（数据库/缓存等），部署阿里云/腾讯云 | §14 |
| 一.7 | 本地运行于 IDE sandbox 内运行和验证 | §15 |
| 二.1 | 本地单次运行组合/策略/数据，computer use 调用第三方（认证/取数） | §7、§15.3 |
| 二.2 | 本地运行时在 IDE sandbox 内 | §15 |
| 二.3 | 单次运行后输出完整报告 + 变更/执行/风险建议 | §7.3 |

---

# 第一部分 · 产品设计

## 1. 产品总览：三端一体

```
┌────────────────────────────────────────────────────────────────────┐
│                        AlphaPocket 产品矩阵                          │
├──────────────┬──────────────────┬──────────────────────────────────┤
│              │  云端 Web/H5      │  IDE 本地（Trae/opencode/Codex）  │
│              │  （响应式一套代码） │  + ap CLI / ap-mcp               │
├──────────────┼──────────────────┼──────────────────────────────────┤
│ 定位         │ 随身投研台        │ 深度研究所                        │
│ 典型时长     │ 碎片 3–10 分钟    │ 整块 30 分钟–数小时               │
│ 核心动作     │ 看（净值/信号/    │ 跑（因子检验/回测/                │
│              │ 晨报）→ 确认      │ 数据回补）→ 沉淀                 │
│              │ （拟调仓/评估单）  │ （新因子/新策略/报告）             │
│ 大模型消耗   │ 云端 DSH token    │ IDE 内嵌大模型 token              │
│              │ （服务端账号）    │ （用户已有订阅，零边际成本）        │
│ 数据位置     │ 云端（OSS/RDS）   │ 本地 sandbox（clone 缓存）        │
├──────────────┴──────────────────┴──────────────────────────────────┤
│ 共享资产：因子注册中心 · 策略 DSL · 组合快照 · 检验/回测报告（版本化） │
│ 同步协议：ap clone（远→本） · ap push（本→远，PR 式审核） · MCP 工具面 │
└────────────────────────────────────────────────────────────────────┘
```

**设计原则**

1. **一套数据模型，两种运行时**：因子/策略/组合的业务语义只在注册中心定义一次；云端与本地是同一 DSL 的两个执行环境。
2. **重活下沉本地，轻确认上云**：分钟级以上的计算（全量因子检验、参数网格、数据回补）默认本地跑；云端只做日常批处理（盘后 DAG）与移动端服务。
3. **token 经济学**：IDE 内嵌大模型（用户订阅额度）承担 agent 推理；云端 DSH token 只服务移动端对话与轻量任务，成本可控。
4. **本地是沙箱、远端是真相**：本地任何产物必须过验证（golden test + 纪律官审查）才能 push 回远端；远端 main 分支 = 可信状态。

## 2. 响应式 Web/H5（一.1）

### 2.1 一套代码、双断点增强

- **技术**：Vue 3 + Vite + TypeScript + UnoCSS；组件级响应式（`useBreakpoint()`），不做 m.xxx.com 双站。
- **断点策略**：

| 断点 | 布局 | 增强点 |
| --- | --- | --- |
| `< 768px`（手机 H5） | 单列纵向流 + 底部 tabbar（沿用 prototype-v3.1 信息架构：今日/持仓/挖掘/研究所/我的） | 微信/QQ 内置浏览器适配、JS-SDK 分享、下拉刷新 |
| `768–1024px`（平板） | 单列 + 可选双栏（列表 + 详情） | — |
| `≥ 1024px`（PC Web） | **三栏工作台**：左侧导航树（组合/策略/因子/研究所）+ 中间主内容 + 右侧上下文栏（ lineage / 待办 / agent 会话） | 快捷键、多标签页、表格密度切换（紧凑/舒适）、ECharts 大图模式 |

- **视觉基线**：沿用 v3.1 原型的纸质档案 + 印章红体系；PC 端扩展为"档案桌面"隐喻（左抽屉 = 档案柜）。
- **条件能力**：微信 OAuth 登录仅移动端呈现；PC 端突出 CLI/MCP 配置入口。

### 2.2 路由与页面清单

| 域 | 路由 | 移动端 | PC 端 |
| --- | --- | --- | --- |
| 今日 | `/today` | ✅ 原型既有 | 同 + 晨报全文 |
| 组合 | `/portfolio/:id` | ✅ 原型既有 | + 持仓全表、净值大图、归因下钻 |
| 策略工坊 | `/strategy/:id` | 配置表单 | + DSL 编辑器（Monaco）、回测对照并排 |
| 因子注册中心 | `/factors` | 列表 + 状态徽章 | + 检验报告、IC 时序图、版本树 |
| 研究所 | `/desk` | ✅ 多 agent 对话 | + 会话分屏（任务图可视化）、算力预算 |
| **版本控制** | `/versions` | 变更历史流 | + checkpoint 对比、PR 审核队列（§5.3） |
| **本地运行中心** | `/runs` | 运行状态卡 | + 运行日志流、报告预览、clone/push 操作台 |

## 3. 账号与双因子（一.2）

### 3.1 产品规则

| 场景 | 第一因子 | 第二因子 | 说明 |
| --- | --- | --- | --- |
| 手机 H5 首次登录 | 手机号 + 短信验证码 | **TOTP 动态码**（Authenticator 扫码绑定） | 绑定后展示 10 个备份码 |
| 微信快捷登录 | 微信 OAuth | 首次仍需绑手机 + TOTP | 已信任设备免二因 |
| PC Web 登录 | 手机号/邮箱 + 密码 | TOTP | 密码登录失败 5 次锁定 15 分钟 |
| 敏感操作（step-up） | — | **强制再验 TOTP** | 适用：API/MCP 凭证重置、组合数据导出、拟调仓确认 > 20% 权重、删除 checkpoint |

**信任设备**：设备指纹（UA + 画布指纹 + 安全芯片可用时）登记，30 天免二因；"我的 → 设备管理"可远程注销任意设备。

### 3.2 会话策略

- Access Token（JWT，15 分钟）+ Refresh Token（7 天，轮转式，旧 token 即刻失效）。
- 移动端 refresh 绑定设备指纹；刷新失败 → 重新走双因子。
- MCP/CLI 凭证独立于会话：`ap auth login` 走 **Device Code Flow**（云端显示 6 位码 + 二维码 → 手机 App/H5 确认 + TOTP → 颁发 PAT，scope 最小化：read:portfolio / write:runs / sync:repo）。

## 4. 研究所：多 agent 产品设计（一.3，产品侧）

### 4.1 角色与交互模型（产品不变量，源自 SeekAlpha 拆解）

沿用"调度官 + 专家组"六角色（投资总管/基本面/技术面/宏观流动性/风险官/纪律官），V4 增补两个**运行时角色**：

| 新角色 | 职责 | 运行位置 |
| --- | --- | --- |
| **本地执行员（Runner）** | 接收任务图中的计算节点（因子检验/回测/回补），在 IDE sandbox 内执行，回报进度与产物 | IDE 本地 |
| **数据管家（Data Steward）** | 管理数据源健康、computer use 取数任务、契约校验结果分流 | 云端 + 本地 |

### 4.2 用户可感知的协作形态

1. **任务图可视化**：研究所会话中，调度官拆解出的 DAG 以缩略图呈现在会话顶部（PC 端可展开），每个节点 = 一个专家/工具，点击看该节点的输入输出与证据等级（E0–E3）。对应"可追溯、可复现"纪律。
2. **分歧呈现**：多专家结论冲突时（如基本面 A- vs 技术面 C+），会话内并列展示理由链，由投资总管标注分歧点并请用户裁决——**分歧是信息，不是错误**。
3. **任务下发到本地**：云端会话中用户说"把 val_ep_ttm 在 2019–2026 全样本重检一遍"→ 调度官生成任务包 → 推送到在线的本地运行中心 → IDE 内 agent（用 IDE token）执行 → 结果回传云端会话归档。
4. **算力预算双轨**：云端 token 预算（月度）与本地 IDE token 用量分别计量展示，本地任务不消耗云端预算。

## 5. IDE 本地任务与数据版本（一.4 + 一.5，产品侧）

### 5.1 本地任务两种模式

| 模式 | 触发 | 形态 | 示例 |
| --- | --- | --- | --- |
| **单次任务** | 用户在 IDE 对话/终端手动发起 | `ap run <task.yaml>` 或 IDE agent 对话调用 ap-mcp 工具 | "跑一次全球成长组合的月度复盘，出报告" |
| **自动化定制任务** | cron / 事件（新财报披露、因子降级告警）/ webhook | 任务包 + 触发器登记，由 IDE agent 的非交互模式执行（`opencode -p` / `codex exec`） | "每个交易日 20:30 拉最新净值，若引擎 B 触发卖出预警则生成评估单草稿" |

任务包 = Task YAML（DSL）：

```yaml
id: nightly_review_global
trigger: {cron: "30 20 * * 1-5", tz: Asia/Shanghai}
runtime: {sandbox: docker, profile: quant-std}
steps:
  - {tool: data.sync, args: {datasets: [dwd_daily_bar, dwd_fx_rate]}}
  - {tool: factor.compute, args: {as_of: today}}
  - {tool: portfolio.review, args: {portfolio: global, sections: [health, attribution, signals]}}
  - {tool: report.generate, args: {template: monthly_review, out: [md, html]}}
notify: {on: [risk_warn, signal_fired], channel: [cloud_inbox]}
```

### 5.2 三 IDE 适配矩阵（产品视角）

| IDE/Agent | 用户怎么用 | 内嵌 token 复用 |
| --- | --- | --- |
| **Trae** | 装 ap-mcp（MCP 插件）→ 研究所对话里直接说需求 → Trae 内嵌模型编排 ap 工具 | ✅ Trae 订阅模型额度 |
| **opencode** | 工作区内置 `.opencode/agent/investigator.md`（自定义 agent 角色）+ ap CLI；TUI 交互或 `opencode -p "…"` 非交互 | ✅ opencode 已配置的模型 |
| **Codex** | 仓库即 alphapocket 项目；`codex exec "ap run …"` 非交互；AGENTS.md 声明工具边界 | ✅ Codex 订阅额度 |

### 5.3 版本与检查点工作流（用户视角）

**类 Git 心智模型，四个动词**：

```
云端 main ──┐
            │ ap clone（首次/同步：拉策略/因子定义/组合快照/数据缓存清单）
            ▼
本地 research/* 分支 ──跑（sandbox 内）──► checkpoint（本地）
            │ ap push（提交 Change Request：diff + 运行证据 + 报告）
            ▼
云端 PR 队列 ──纪律官自动审查（PIT 审计/golden test/回测对照）──► 人工确认 ──► merge 进 main
```

- **什么被版本化**：因子定义 YAML（含 version）、策略 DSL、组合状态快照（持仓+成本+多币种）、检验/回测报告、任务包。数据集用**版本指针**（dataset@commit）而非全量入 git。
- **checkpoint 语义**：一次可复现运行的全部上下文 = `(code_version, factor_versions, strategy_version, portfolio_state@as_of, dataset_commits, params)` + 产物（报告/净值/归因）。任何人（含未来的自己）可用 checkpoint 一键重放。
- **手机端呈现**：`/versions` 页 = 时间线流（每次 push/merge/巡检），checkpoint 卡片可"对比"（两期五维/持仓 diff）。

## 6. 多 agent 协作：调研与评估（一.3，先调研再评估）

> 本节先给调研结论（公开资料，来源见附录 A），再给评估与选型。**调研是别人的经验，评估才是我们的决策。**

### 6.1 调研结论（2026-10 检索）

**(1) 主流协作拓扑四型**

| 拓扑 | 机制 | 适用 | 风险 |
| --- | --- | --- | --- |
| Supervisor / Orchestrator-Worker | 中央调度官拆解任务、分派专家、汇总 | 任务可结构化拆解、需强控 | 调度官成为瓶颈/单点 |
| Pipeline（顺序流水线） | 上一个 agent 输出 = 下一个输入 | 流程固定的阶段性任务 | 错误级联传播 |
| Group Chat / Debate | agent 间自由对话、辩论收敛 | 开放性研判 | token 消耗大、发散、不可控 |
| Blackboard（共享黑板） | 各 agent 读写共享状态，无中心 | 状态复杂、多专家并发观察 | 一致性/写冲突治理难 |

**(2) 框架横评要点（业界共识）**

- **LangGraph**：状态图编排，生产环境稳定性口碑最好；显式图结构天然可追溯。
- **OpenAI Agents SDK**：轻量，Handoff（移交）+ Agent-as-Tool（agent 当工具调）双范式。
- **CrewAI**：角色 + 任务导向，上手快，多 agent 协作场景成熟。
- **AutoGen**：对话式，快速原型友好。
- 共性趋势：**生产级系统都收敛到"显式编排（图/DAG）+ 受控移交"**，自由对话型用于探索而非生产。

**(3) 协议层**

- **MCP**（垂直）：agent ↔ 工具/外部世界的事实标准——本研究项目已内定（架构文档 §26.2）。
- **A2A**（水平，Google 推开放标准）：跨厂商/跨运行时 agent 互通（任务分配、能力发现、状态回报）。与本项目的"云端 DSH agent ↔ IDE 本地 agent"跨运行时协作场景高度契合，但生态尚在早期。

**(4) IDE agent 非交互能力（一.4 依赖）**

- opencode：TUI + `opencode -p "<文本>"` 非交互单次执行；
- Codex：`codex exec` 非交互模式，多 surface（CLI/IDE/云）共享底层 agent；
- 结论：**主流 CLI agent 均具备 headless 模式**，可被 cron/触发器驱动，支撑"自动化定制任务"。

**(5) Computer Use / Browser Use / Sandbox 取舍（二.1 依赖）**

业界"最小权限门"共识：**Browser Use（只动浏览器）< Computer Use（动整桌面）< Sandbox（有 shell/文件系统）**。生产建议：能用 API/数据接口就不用浏览器；用浏览器优先 Browser Use；全程容器隔离 + 凭据不入模型上下文。

### 6.2 评估：投研场景的约束条件

| 约束 | 来自 | 对协作设计的含义 |
| --- | --- | --- |
| 可追溯、可复现是决策前置条件 | SeekAlpha 拆解 · 理念三 | 自由对话型（Group Chat）排除出生产链路；显式任务图胜出 |
| 任务天然是 DAG | 取数→计算→检验→汇总，依赖单向 | Supervisor + DAG 是自然映射 |
| 专家间"交接"多于"讨论" | 分析师产出结构化结论（E 标注），非开放辩论 | 移交契约（handoff schema）> 会话接力 |
| token 双轨经济 | 云端预算有限、IDE token 充裕 | 编排在哪、推理就在哪——跨运行时任务只传任务包与结果，不传对话历史 |
| DSH 已有 Skill/Tool 注册、事件溯源、记忆 | 移动端设计方案 §3 | 不引重型框架，避免双运行时（云/本地）都被框架绑定 |

### 6.3 选型决策（评估产出）

```
┌───────────────────────────────────────────────────────────────┐
│ 协作内核：DSH 自有编排层（不引入 LangGraph/CrewAI 整框架）        │
│   = Supervisor 状态机（LangGraph 思想）+ 任务 DAG + 移交契约      │
│   局部采用 Blackboard：共享工作区（scratchpad）存中间事实          │
├───────────────────────────────────────────────────────────────┤
│ 工具协议：MCP（唯一工具面，云端/本地同构）                        │
│   ap-mcp 工具集 = 数据/因子/回测/组合/报告/同步 六组               │
├───────────────────────────────────────────────────────────────┤
│ 跨运行时：短期 = 任务包（Task YAML）+ 回调；中期 = A2A 协议对接    │
│   （云端 agent 与 IDE agent 互为 A2A 端点，能力发现 + 状态回报）   │
├───────────────────────────────────────────────────────────────┤
│ 可解释性：任务图即审计对象——每个节点记录 (输入指纹, 输出指纹,       │
│   证据等级, token 消耗, 耗时)，事件溯源日志 = 会话回放依据         │
└───────────────────────────────────────────────────────────────┘
```

**为什么不整框架引入**：① 双运行时要求编排层可嵌入 CLI（本地无服务常驻）——自研薄编排（<2k 行）比框架裁剪更轻；② 我们不需要框架的通用对话编排，需要的是"任务图 + 工具调用 + 事件溯源"，DSH 内核已有后两者；③ A/B 可演进：编排层接口对齐 LangGraph 的图语义（nodes/edges/checkpoint），未来若需要可平移。

## 7. 本地原型设计（二，产品侧）

### 7.1 单次运行的用户旅程（IDE 内）

```
用户（IDE 对话/终端）
  │ "复盘全球成长组合，重点看美股拖累和汇率"
  ▼
IDE agent（Trae/opencode/Codex，消耗 IDE token）
  │ 解析意图 → 调 ap-mcp 工具（非自由发挥，工具即边界）
  ▼
ap run（sandbox 容器内）
  ├─ data.sync     云端拉增量 + 缺口直连 AKShare 回补
  ├─ [computer use] 若需券商对账单：Browser Use 登录下载（2FA 人工介入）
  ├─ factor.compute + portfolio.review + backtest.incremental
  └─ report.generate
  ▼
产物落本地 repo：report.html / report.md + checkpoint
  ▼
agent 读取报告 → 会话内给出解读 + 建议（变更/执行/风险）
  ▼
用户决定：ap push（提 PR 沉淀远端）或 仅留本地
```

### 7.2 computer use 的产品边界（二.1）

| 允许 | 禁止 |
| --- | --- |
| 券商网站登录、对账单/交割单下载 | 任何下单/撤单/转账页面操作 |
| 数据源（Wind 试用页/交易所披露页）认证与导出 | 凭据写入代码/日志/模型上下文 |
| 验证码/2FA 弹窗 → **暂停并请人工接管**（human-in-the-loop） | 绕过频控、伪造 UA 批量抓取 |

凭据存储：OS keychain（macOS Keychain / Windows Credential Manager），sandbox 内进程按需短期借取，任务结束即收回。

### 7.3 运行报告规格（二.3）

单次运行输出**一份完整报告 + 三类建议**（模板固定，禁止自由发挥）：

| 报告章节 | 内容 | 数据来源 |
| --- | --- | --- |
| 组合体检 | 健康度、五维雷达（穿透后）、分市场市值、汇率贡献 | portfolio.review |
| 持仓明细 | 逐标的：市场徽章、五维/基金体检、AH 溢价角标、数据陈旧度 | factor + 基金体检 |
| 策略表现 | 净值 vs 复合基准、归因四层（市场/行业/个股/汇率）、换手与成本 | backtest.incremental |
| 引擎 B 信号 | 当日触发信号列表、评估单草稿、仓位系数 | signal 层 |
| 因子健康 | 持仓相关因子的巡检状态（healthy/weakening）、失效环境标签 | lifecycle |
| **变更建议** | 拟调仓 diff（目标权重 vs 当前）、每条附理由链与证据等级 | RebalancePlan |
| **执行建议** | 引擎 A 调仓单的执行择时输入（引擎 B）、分批建议、成本预估 | §7.4 模式① |
| **风险建议** | 集中度/风格暴露/汇率敞口/拥挤度预警、短板维度描红、能力圈外操作提示 | 风险官规则 |

报告头部固定四元组（`run_id + code_version + dataset_commits + as_of`）+ 免责声明；HTML 版可本地浏览器打开，MD 版供 agent 解读与会话引用。

---

# 第二部分 · 技术架构

## 8. 总体架构

```
╔═════════════════════════════ 云端（阿里云/腾讯云，§14）═════════════════════════════╗
║                                                                                     ║
║  Web/H5 前端（Vue3 响应式，OSS+CDN）      微信/QQ 浏览器 · PC 浏览器                  ║
║                    │ HTTPS(REST) + SSE + WSS                                         ║
║  ┌─────────────────▼──────────────────┐                                              ║
║  │ BFF / API Gateway（NestJS）          │  鉴权(JWT+2FA) · 限流 · DTO 裁剪            ║
║  └───────┬───────────────────┬─────────┘                                              ║
║          │                   │                                                        ║
║  ┌───────▼───────┐   ┌───────▼────────────────────────────┐                         ║
║  │ DSH Agent      │   │ Quant Core（FastAPI）               │                        ║
║  │ Runtime        │◄──┤ 组合/策略/回测/因子/信号/分析         │                        ║
║  │ · 编排层(Supervisor│  └───────┬────────────────────────────┘                         ║
║  │   +DAG+移交契约) │          │                                                    ║
║  │ · Skill/Tool    │   ┌───────▼────────┐   ┌──────────────────┐                    ║
║  │ · 事件溯源/记忆  │   │ DataHub         │   │ Version/Sync 服务 │                    ║
║  │ · MCP Server(云)│   │ 采集DAG·契约校验 │   │ 对象库·checkpoint │                    ║
║  └───────┬────────┘   │ ·血缘·汇率       │   │ PR 审核队列       │                    ║
║          │            └───────┬────────┘   └────────┬─────────┘                    ║
║          │                    │                     │                               ║
║  ┌───────▼────────────────────▼─────────────────────▼─────────┐                     ║
║  │ 存储：PostgreSQL(RDS) · Redis · OSS(MinIO兼容) · DuckDB宽表  │                     ║
║  └────────────────────────────────────────────────────────────┘                     ║
╚══════════════════════════════════╤═══════════════════════════════════════════════════╝
                                   │  HTTPS · MCP(流式) · 任务包推送(PAT 授权)
╔══════════════════════════════════▼═══════════════════════════════════════════════════╗
║                           IDE 本地运行时（§12–§15）                                    ║
║  ┌─────────────────────────────┐      ┌────────────────────────────────────┐          ║
║  │ IDE Agent 宿主（任选其一）    │      │ ap CLI（核心执行器）                 │          ║
║  │ · Trae + ap-mcp 插件         │      │  ap clone / run / push / auth       │          ║
║  │ · opencode（TUI / -p headless）│    │  ap-mcp（本地 MCP Server，stdio）    │          ║
║  │ · Codex（codex exec）         │      │  任务调度（cron/事件触发器）           │          ║
║  │   [IDE 内嵌大模型 token]      │      └───────────────┬────────────────────┘          ║
║  └──────────────┬──────────────┘                      │                               ║
║                 │ MCP(stdio) 调用工具                   │                               ║
║  ┌──────────────▼──────────────────────────────────────▼─────────┐                    ║
║  │ Sandbox（Docker 容器：quant-std 镜像）                          │                    ║
║  │  Python3.11 · DuckDB · polars · backtrader/vectorbt · qlib      │                   ║
║  │  本地 repo（clone 的工作区） · 数据缓存（Parquet） · 产物区        │                   ║
║  │  [Browser Use 隔离容器：第三方认证/取数（二.1）]                  │                    ║
║  └────────────────────────────────────────────────────────────────┘                    ║
╚════════════════════════════════════════════════════════════════════════════════════════╝
```

## 9. 认证与双因子实现（一.2）

```
登录流：凭证核验(短信/密码/OAuth) → TOTP 校验(RFC 6238, 30s 窗口±1) → 设备指纹登记(信任30天)
        → 颁发 JWT(access 15m) + refresh(7d 轮转)
Step-up：敏感操作 API 中间件强制 TOTP 复验（前端弹码，10 分钟内免重复）
PAT：ap auth login → Device Code Flow（云端出码 → H5 确认+TOTP → PAT(scope 最小化, 90天轮转)）
```

| 组件 | 选型 | 说明 |
| --- | --- | --- |
| 短信 | 阿里云 SMS / 腾讯云 SMS | 与云厂商同生态；频控 1/min、5/h；模板备案 |
| TOTP | otplib（服务端）+ 用户自选 App | 密钥仅在绑定时展示一次；备份码 10 个单次有效 |
| 密码 | argon2id | 不存明文；5 次失败锁 15 分钟（Redis 计数） |
| JWT | RS256 非对称 | BFF 校验公钥，签名私钥仅 auth 服务持有 |
| 设备指纹 | UA+canvas+语言哈希 | 仅作信任标记，不作为认证因子 |

## 10. 前端工程（一.1）

- **构建**：Vite + vue-router + Pinia；ECharts 按需引入（移动端 `useECharts(dimension)` 禁 3D/大图）。
- **性能预算**：H5 首屏 JS ≤ 300KB(gzip)；列表游标分页；SSE 流式渲染 agent 会话。
- **PC 增强**：Monaco 编辑器（DSL/SQL 高亮，复用注册中心语法）、任务图可视化（自绘 SVG DAG，复用原型 evidence 组件视觉）。
- **同构 UI 组件**：五维徽章/市场徽章/体检卡等直接从 prototype-v3.1 的 CSS 提炼为组件库 `@ap/ui`。

## 11. 多 agent 协作技术方案（一.3，技术侧）

### 11.1 编排层内核（DSH 内嵌，双运行时同构）

```python
# 编排层核心抽象（示意，实现于 dsh/orchestrator/）
class TaskGraph:            # 显式 DAG：节点=agent或工具，边=数据依赖
    nodes: dict[str, Node]  # Node(agent, tool, handoff_schema, evidence_level)
    edges: list[tuple[str, str, Mapping]]

class Supervisor:           # 调度官状态机（LangGraph 语义子集）
    states: [PLAN, DISPATCH, WAIT, AGGREGATE, ESCALATE, DONE]
    def plan(question) -> TaskGraph          # 意图→任务图（工具白名单约束）
    def dispatch(graph)                      # 节点就绪即派发（无依赖并行）
    def aggregate(results) -> Conclusion     # 汇总+分歧标注
    def escalate(conflict) -> HumanDecision # 分歧→用户裁决

class Scratchpad:           # Blackboard 局部采用：跨节点共享的中间事实
    facts: dict[fact_id, Fact(value, source, evidence, lineage_id)]
```

**移交契约（handoff schema）**：专家间不传自由文本，传结构化结论：

```json
{
  "from": "fundamental_analyst",
  "claims": [
    {"text": "ROE TTM 连续4季>15%", "evidence": "E2", "lineage_id": "lg_..."},
    {"text": "毛利率拐点确认", "evidence": "E2", "confidence": 0.82}
  ],
  "outputs": {"factor_snapshot": "fs_20261004_001"},
  "open_questions": []
}
```

### 11.2 事件溯源与回放

- 每次 agent 会话/任务执行 = 一个 `session_id`；所有节点输入输出、token 消耗、工具调用落 `agent_event_log`（append-only）。
- **回放**：给定 session_id 重放任务图（工具结果可从产物缓存命中，不重烧 token）——回测可复现纪律在 agent 层的对应物。
- 与 E0–E3 证据分级联动：每个 claim 必须带 evidence，调度官汇总时低于 E2 的结论不得进入"可执行建议"区。

### 11.3 跨运行时协作（云 ↔ 本地）

| 阶段 | 机制 |
| --- | --- |
| **P0（随 V4 首版）** | 任务包推送：云端生成 Task YAML → 推送队列 → 本地 `ap` 长轮询拉取 → 执行 → 回调上报（结果摘要 + 产物哈希） |
| **P1** | A2A 对接：本地 ap-mcp 暴露 Agent Card（能力：factor.compute/backtest.run/…），云端 DSH 通过 A2A 协议做能力发现、任务分派、长任务状态订阅 |
| **P2** | 本地 agent 反向调用云端专家（如本地跑完回测后调云端风险官做合规复核）——A2A 双向 |

### 11.4 IDE token 复用机制（一.4 关键）

- **原则**：ap/ap-mcp 自身**不调用任何大模型 API**——纯工具执行器；所有推理由宿主 IDE agent 完成（其 token 归属用户订阅）。
- **接入方式**：ap-mcp 以 stdio MCP Server 注册进 IDE；工具描述即行为边界（IDE 模型只能通过工具干活，不能"绕过工具直接改数据"）。
- **工具集（ap-mcp v1，24 个）**：

| 组 | 工具 | 说明 |
| --- | --- | --- |
| data | `data.sync` `data.query` `data.lineage` `data.backfill` | 增量同步/点查/血缘/回补 |
| factor | `factor.list` `factor.compute` `factor.evaluate` `factor.register(draft)` | 计算与检验；注册只产 draft，激活须走远端 PR |
| backtest | `backtest.run` `backtest.compare` `backtest.ab_test` | 含引擎 B 对照协议 |
| portfolio | `portfolio.snapshot` `portfolio.review` `portfolio.rebalance_plan` | 复盘与拟调仓 |
| report | `report.generate` `report.read` | 模板化生成与读取（供 agent 解读） |
| sync | `repo.clone` `repo.status` `repo.push` `checkpoint.list` `checkpoint.restore` | 版本面（§13） |

## 12. ap CLI 与任务执行（一.4）

```
ap auth login                     # PAT（Device Code Flow + TOTP）
ap clone <remote> ./my-ap         # 首clone：定义+快照+数据缓存清单
ap run <task.yaml> [--as-of DATE] # 单次任务（sandbox 内执行）
ap schedule add/ls/rm/enable/disable/run-now/logs   # 自动化定制任务
ap daemon [--once|--foreground]   # 调度守护进程（install/uninstall 注册常驻）
ap push -m "..." [--pr]           # 提交变更 → 远端 CR/PR
ap checkpoint ls/restore/diff     # 检查点管理
ap serve                          # 启动 ap-mcp（stdio，供 IDE 挂载）
```

**调度器**：本地轻量守护进程（`ap daemon`）——cron 触发 `opencode -p` / `codex exec` 执行任务包；事件触发器（webhook 回调 + 财报披露日历轮询）。**执行仍全程在 sandbox 容器内**，daemon 只负责拉起与上报。

### 12.1 统一任务模型：单次与自动化同构

单次任务 = 无 `trigger`（或 `trigger: {on: manual}`）的任务包；自动化任务 = 带触发器的**同一份 Task YAML**。二者共用同一 `run_task()` 执行内核、同一 sandbox 镜像、同一 checkpoint 与验证流水线——**差异只在"谁把它拉起来"**，避免两套执行路径产生行为漂移。

Task YAML schema v1（`trigger` 与三组 `policy` 字段为 V4-RC 新增，其余 α 已落地）：

```yaml
id: nightly_review_global          # 全局唯一
version: 3
trigger:                           # 缺省 = manual（单次）
  cron: "30 20 * * 1-5"            # 五段式；与 event 二选一或并存（或关系）
  tz: Asia/Shanghai
  calendar: cn_trading_day         # 交易日历过滤器：仅在开市日触发（§数据层日历）
  catch_up: last                   # 休眠补偿：all | last | none
# event 型示例：event: {on: earnings_filed, filter: {market: [US, HK]}}
# webhook 型示例：webhook: {path: /hooks/broker_stmt, secret_ref: keychain:wh_broker}
runtime: {sandbox: docker, profile: quant-std, executor: auto}   # executor: auto|ap|agent
params: {portfolio: global, as_of: auto}
steps:
  - {tool: data.sync, args: {datasets: [dwd_daily_bar, dwd_fx_rate]}}
  - {tool: factor.compute, args: {as_of: auto}}
  - {tool: portfolio.review, args: {portfolio: global}}
  - {tool: report.generate, args: {template: full_review, out: [md, html]}}
verify: [pit_audit, golden_test]   # 全绿才允许 push（§15.2）
policy:
  concurrency: single              # single（同 task 单飞）| queue | parallel
  retry: {max: 2, backoff: exponential, on: [network, rate_limit]}  # 验证失败不重试
  timeout: 45m                      # wall-clock 超时 → kill 容器 → failed
  retention: {keep_runs: 90}        # ledger + 产物保留策略
notify: {on: [risk_warn, signal_fired], channel: [cloud_inbox], quiet: "22:00-08:00"}
allowed_tools: [data.*, factor.*, portfolio.*, report.*]   # 工具白名单，超出即拒
```

### 12.2 触发器类型与登记

| 类型 | 语法 | 语义 | 依赖 |
| --- | --- | --- | --- |
| **cron** | `cron + tz + calendar` | 定时触发，按交易日历过滤（节假日不跑） | 内置 cron 解析 + 交易日历 |
| **event** | `{on, filter}` | 订阅本地/云端事件：数据水位更新、因子健康降级、财报披露、引擎 B 信号触发 | 事件总线（§12.3） |
| **webhook** | `{path, secret_ref}` | 外部回调：券商对账单到达、云端任务下发、CI 完成 | 本地只监听 127.0.0.1 + HMAC 校验 |
| **chained** | `{after, on_status}` | 上游任务完成/失败触发（如 backfill 成功后跑重检） | Run Ledger |

登记方式二选一，**以 repo 内 `tasks/*.yaml` 为唯一事实源**：文件即登记（`enabled: true/false` 字段控制开关），`.ap/schedule.db` 仅存运行态（下次触发时间、上次结果）。这样任务定义随 `ap clone/push` 版本化，符合 §13 的"定义与产物同源"纪律。

### 12.3 调度器架构（ap daemon）

```
ap daemon（单进程，无强服务端依赖；崩了不影响已归档产物）
├─ Trigger Registry   读 tasks/*.yaml + .ap/schedule.db，产出"任务→触发器"清单
├─ Scheduler Core     cron 解析 / 下次触发时刻计算 / 交易日历过滤 / 去重
├─ Local Event Bus    水位文件变更、告警订阅（inotify + 轮询兜底）
├─ Webhook Listener   127.0.0.1:9711，HMAC 校验，外部不可直连
├─ Executor Adapter   任务包 → 宿主 headless 命令（见下表）
├─ Run Ledger         SQLite/duckdb：run_id/trigger/status/timing/token/artifacts
├─ Notifier           cloud_inbox / 桌面 / 邮件（分级 + 静默窗口）
└─ Recovery/Reaper    超时 kill、僵尸清理、missed-run 补偿
```

**双通道执行策略**（一.4 复用 IDE token 的关键）：确定性步骤走 `ap run`（纯工具，**零 token**）；需要语言推理或取数决策的步骤走**宿主 IDE agent 的 headless 模式**（复用用户订阅额度，§11.4 原则：ap 自身不调任何大模型 API）。

| Executor | headless 命令 | 边界声明 |
| --- | --- | --- |
| direct（`ap`） | `ap run tasks/x.yaml --as-of DATE` | 工具白名单即边界，不烧 token |
| opencode | `opencode -p "执行 tasks/x.yaml"` | `.opencode/agent/*.md` 声明角色与工具 |
| Codex | `codex exec "ap run tasks/x.yaml"` | 仓库 `AGENTS.md` 声明边界 |
| Trae | `ap` 直执行 + agent 仅解读报告 | ap-mcp 工具面即边界 |

`executor: auto` 判定规则：任务步骤全是 ap-mcp 工具调用 → 走 direct；含 `computer_use` / 自由研判步骤 → 走 agent 通道。

### 12.4 单次触发时序

```
daemon tick / event 到达
  └─► 构造幂等键 {task_id}@{scheduled_time 或 as_of} → 查 Ledger 去重
        └─► 组装 run context（as_of、scope 最小化 PAT、allowed_tools、资源配额）
              └─► 拉起 sandbox 容器（quant-std，§15.1）
                    └─► ap run 执行 steps（逐工具落事件日志）
                          └─► verify 流水线（pit_audit / golden_test）
                                ├─ 全绿 → checkpoint（§13.1）→ 产物归档 → push 候选
                                └─ 任一红 → 仅存本地 + 告警，禁止 push
              └─► Notifier 按级别分发（静默窗口内合并）
        └─► Ledger 写回（status/timing/token/artifacts/verify）
```

### 12.5 幂等、并发与补偿（无人值守的可靠性核心）

| 机制 | 策略 |
| --- | --- |
| **幂等** | 幂等键 `{task_id}@{scheduled_time}`；重复触发直接命中 Ledger 返回既有 run_id，不重复执行 |
| **并发** | 同 task 默认 `single`（单飞：运行中则跳过并记 `skipped`）；跨 task 由 sandbox 信号量限流（CPU/内存配额），防夜间多任务挤爆 |
| **补偿** | daemon 启动/唤醒时比对 Ledger 与 cron 计划，按 `catch_up` 补跑：`all` 全补 / `last` 只补最近一次 / `none` 不补 |
| **重试** | 仅对可重试错误（`network`/`rate_limit`）退避重试；**验证失败（PIT/golden）不自动重试**，转人工 |
| **超时/僵尸** | `timeout` 到点 kill 容器并标 `failed`；Reaper 周期性清理孤儿容器与锁 |
| **断点续跑** | 大任务（长回测）失败后从最近 checkpoint restore 续跑（与 §13 checkpoint 语义联动） |

### 12.6 安全边界（无人值守前提下）

- **PAT 最小 scope**：自动化任务默认 `read:* + write:runs`，**禁止 `merge`**——daemon 永不执行 `ap push --approve`；成果只能成为 CR 候选人，由人在 H5 确认（§13.2）。
- **沙箱强制**：daemon 不得在宿主直接跑任务步骤，一律容器内；无 docker socket；网络白名单沿用 §15.1。
- **human-in-the-loop 断点**：涉及资金/敏感动作（computer use 登录券商、2FA/验证码）→ 暂停 + 通知，等待人工接管（复用 `browser_waiting_for_user_interaction` 模式）；无人值守任务不得静默跳过。
- **密钥**：PAT/凭据存 OS keychain，任务期按需短期借取，结束即收回；不落日志、不进模型上下文；webhook 走 HMAC。
- **工具白名单**：`allowed_tools` 之外的工具调用直接拒绝，防任务包被篡改后越权。

### 12.7 观测、通知与计量

Run Ledger 关键字段：`run_id, task_id, version, trigger_type, scheduled_at, started_at, ended_at, status(success|failed|skipped|timeout|paused_need_human), executor, token_used, verify{}, checkpoint_id, artifacts[], error`。

- **通知分级**：`info`（例行完成）/ `action_required`（需人类裁决，如分歧/2FA）/ `risk_warn`（风险规则命中）；`quiet` 窗口内降噪合并为一条日报，避免夜间轰炸。
- **端侧呈现**：本地 `ap schedule logs <id> -f`；手机端复用 `/versions` 时间线与告警页（§5.3）。
- **token 双轨计量**（§4.2）：direct 通道记 0；agent 通道上报 IDE token 用量（可读则记，不可读则标记 `unknown`），与云端预算分账。

### 12.8 CLI / MCP 契约

```
ap schedule add <task.yaml>       # 校验 schema → 落 tasks/ → 写 schedule.db
ap schedule ls [--json]           # id / cron / next_run / last_status / enabled
ap schedule enable|disable <id>
ap schedule rm <id>
ap schedule run-now <id> [--as-of DATE]   # 忽略计划，立即执行（走同一执行内核）
ap schedule logs <id> [-f]        # 读 Run Ledger
ap daemon [--once] [--foreground] [--health-port 9711]
ap daemon install|uninstall       # systemd --user / launchd / Windows 计划任务
```

新增 MCP 工具（ap-mcp v1 由 24 → 28）：`schedule.register` / `schedule.list` / `schedule.run_now` / `daemon.status`——使**云端 DSH 或 IDE agent 可直接登记与查看自动化任务**，无需手改文件。

### 12.9 部署形态

| 形态 | 做法 | 适用 |
| --- | --- | --- |
| 本地常驻 | `ap daemon install`（systemd --user / launchd / 计划任务），或 compose `restart: always` | 日常夜间复盘 |
| 云端下发 | 云端生成 Task YAML → 推送队列 → 本地 daemon 长轮询拉取 → 执行 → 回调上报（§11.3 P0） | 手机端登记、跨机执行 |
| 睡眠/离线 | 笔记本休眠 → `catch_up` 补偿；关键任务可托管云端常驻 runner（同一镜像） | 稳定性优先场景 |
| 多机 | 一个 PAT 登记多运行中心，任务带 `runtime_target` 约束，Ledger 上报云端去重 | 多设备用户 |

### 12.10 落地节奏与验收

| 子阶段 | 内容 | 判据 |
| --- | --- | --- |
| **α（现状）** | `ap schedule ls` 打桩 + Task YAML 增加 `trigger` 字段解析（不执行） | 能列出 cron 计划与下次触发时刻 |
| **β** | `ap daemon --once` + Run Ledger + direct 通道（ap run） | 到点自动跑通 `review_global_once`，产物入 checkpoint |
| **RC** | agent headless 通道（`opencode -p`/`codex exec`）+ event/webhook 触发器 + 重试/补偿/单飞 + 通知分级 | **夜间复盘连续 2 周无人值守稳定运行**（§16 判据） |
| **GA** | 云端下发 + 多机 + 监控告警 + MCP schedule 工具面 | 云端登记任务 → 本地执行 → 结果回传闭环 |

> 与 §16 路线图的关系：本表是"自动化定制任务"这一条能力线的独立里程碑；β 与 V4-β（云端最小闭环）并行，RC 与 V4-RC 合流并通过同一验收判据。

## 13. 数据版本 · 检查点 · 远端同步（一.5）

### 13.1 存储模型

```
对象库（内容寻址，sha256）                    元数据库（PostgreSQL）
  blob: strategy.yaml@sha256                  repo_ref(branch, head_commit)
  blob: factor_def.yaml@sha256                commit(parent, tree, message, sig)
  blob: portfolio_state@as_of                 checkpoint(run_id, code_ver, factor_vers,
  blob: report.md/html@sha256                          strategy_ver, portfolio_ref,
  tree/dataset 指针: dataset@commit                     dataset_commits, params, artifacts)
```

- 数据集（行情/财务宽表）不入对象库，用 **dataset commit 指针**（数据集版本 = 采集 DAG 的 watermark + 校验哈希），clone 时按清单增量拉 Parquet 块。
- 云端对象库落 OSS（版本化开启）；本地落 `.ap/objects`（与 git 同构，可直接 `ap diff`）。

### 13.2 同步与冲突

| 操作 | 协议 | 冲突策略 |
| --- | --- | --- |
| clone | HTTPS + PAT；按 commit 链增量（数据走 Pre-Signed URL 直拉 OSS） | 本地未提交改动保留（stash） |
| push | commit → 远端创建 CR（Change Request） | **不自动 merge**：纪律官审查 + 人工确认；冲突走三方合并（tree 级） |
| 回滚 | checkpoint.restore | 只前滚不覆盖历史（append-only 纪律） |

### 13.3 PR 审查自动化（纪律官 pipeline）

```
push → CR 创建
  ├─ ① PIT 审计：新增/变更因子的取值 SQL 强制 announce_date<=as_of（CI 红线）
  ├─ ② golden test：注册中心黄金样例重跑，diff 必须=0
  ├─ ③ 回测对照：同名策略新旧版本 A/B（同因子同参数同成本），绩效恶化>阈值须人工说明
  ├─ ④ 因子注册门禁：③④⑤⑥ 检验报告齐备才允许 status→active
  └─ ⑤ 报告附件：run 产物哈希入 CR（可追溯）
全部通过 → 通知用户确认 → merge → 云端注册中心/组合服务热更新
```

## 14. 云端部署（一.6）

### 14.1 厂商选型

| 维度 | 阿里云 | 腾讯云 | 结论 |
| --- | --- | --- | --- |
| 计算 | ECS / 轻量应用服务器 | CVM / 轻量 | 同档价格近似 |
| 数据库 | RDS PostgreSQL（成熟） | CDB PostgreSQL | 阿里云 PG 生态略优 |
| 对象存储 | OSS | COS | 等价；MinIO 兼容两者 |
| 短信/生态 | SMS+备案流程成熟 | 同 | 均可 |
| 个人开发者活动 | 新人折扣常驻 | 同 | 按当期比价 |

**推荐**：默认**阿里云**（RDS PG + OSS + SMS 一站、与 SMS 生态成熟度）；组件全部容器化，**可无损迁移腾讯云**（镜像 + RDS 逻辑备份 + COS 工具同步）。

### 14.2 部署拓扑（个人版起步）

```
轻量应用服务器 4C8G（Docker Compose 单机）
  ├─ nginx(HTTPS, certbot) ─ 前端静态(OSS+CDN 可选直挂)
  ├─ bff / quant-core / dsh-runtime / version-svc（各 1–2 容器）
  ├─ airflow(scheduler+web)      # 采集与计算 DAG
  ├─ redis / minIO               # 缓存 / 对象存储（后期切 OSS）
  └─ rds-postgresql 4C8G(基础版, 自动备份7天)  # 独立实例
带宽: 按流量 5Mbps 峰值；域名备案: 必须（国内节点）
```

**成本预算（月，2026 价位级）**：轻量 4C8G ≈ ¥70–120 + RDS 基础版 ≈ ¥80–150 + OSS/流量 ≈ ¥10–30 + SMS ≈ ¥5–20 ≈ **¥165–320/月**。峰值（多用户注册）再升配。

**安全基线**：全链路 HTTPS；RDS/Redis 不公网暴露（内网 VPC）；OSS 桶私有 + Pre-Signed；WAF 按需（个人版可先免）；每日 RDS 快照 + OSS 版本化双保险。

## 15. 本地 Sandbox 与验证（一.7 + 二.2）

### 15.1 容器隔离（quant-std 镜像）

```dockerfile
FROM python:3.11-slim
# 预装：duckdb polars backtrader vectorbt qlib alphalens-reloaded akshare
# 非 root 运行；只读挂载代码区；产物区可写；网络白名单（云端API+数据源域）
# cgroup 限额：CPU 4 / MEM 8G；无 docker socket；seccomp 默认+自定义
```

- IDE 侧：Trae sandbox / opencode 容器模式 / Codex 沙箱模式最终都落到该镜像——**一份运行时定义，三个宿主**。
- 网络出口白名单：`*.aliyuncs.com`（同步）、数据源域（AKShare 上游）、券商域（仅 Browser Use 容器放行）。

### 15.2 验证流水线（本地 → 可 push）

```
sandbox 内跑完 →
  ① 单元/黄金样例（随 repo 携带）      ─┐
  ② PIT 审计 SQL                        ├─ 全绿 → 允许 ap push
  ③ 报告产物哈希登记（checkpoint）      ─┘
  任一红 → 仅存本地 + 告警说明，禁止 push
```

### 15.3 Browser Use 集成（二.1）

- 独立轻容器（`browser-use` 镜像：Playwright + browser-use 框架），与 quant 容器网络隔离，仅共享产物目录（只写）。
- 任务由 ap 以**受限脚本**驱动：目标站点、允许动作（登录/导航/下载）白名单化；遇 2FA/验证码 → 暂停 + 桌面通知人工接管（复用 browser_waiting_for_user_interaction 模式）。
- 下载产物（对账单 CSV/PDF）→ 解析容器（无网络）→ 入本地缓存 → 血缘标注 `source=computer_use, confidence=medium`。

## 16. 实施路线图

| 阶段 | 内容 | 交付判据 |
| --- | --- | --- |
| **V4-α（原型验证，本（二）范围）** | ✅ 已交付（2026-10-04，真实数据版）：ap CLI（fetch/clone/run/push/checkpoint/serve）+ ap-mcp 14 工具（含 factor.compute_alpha）+ AKShare 真实数据接入（新浪 qfq/raw 行情、腾讯交叉源、逐期财务、分红、交易日历 + provenance 溯源）+ 引擎 A 中长期因子（PIT 阶梯 TTM/五年分位/五维评分）+ 引擎 B 20日信号 + 六项验证流水线（PIT 审计/golden 双实现重算/跨源对账/日历校验/复权校验/哈希溯源）+ cn4_review 报告模板（中长期因子评估 + 短期20日评估 + 下一交易日指引）；2026-10-05 增补 **multi5 多资产组合**（沪深300ETF/南方原油LOF/黄金ETF/纳指ETF/十年国债ETF，等权20%×5）——新浪 fund_etf_hist_sina 接入（ETF/LOF 不复权，腾讯交叉源）、份额折算检测（513100 2022-01 拆分日 pct 置 None）、停牌/无行情日豁免分类（跨源一致才豁免）、基金无财报→五维综合不适用 | cn4 与 multi5 双组合复盘端到端全绿；六项验证 pass（golden 99 项对账·cross_source 90 点·adjust 5531 点·停牌豁免3日透明标注）；报告经浏览器渲染核验；2026-10-05 增补**调仓执行单**（ap exec-plan：目标权重→金额→整手委托+费用/T+0·T+1/限价参考带，¥1M 基准 2 卖单·费用 ¥40.01，恒为待人工确认）与**组合回测**（ap backtest：等权月度再平衡含成本 vs 沪深300ETF 买入持有，multi5 2021-01-04~2026-09-30 累计 +105.6% · CAGR 13.9% · MDD -9.6% · 夏普 1.25，超额 +122.5pp，1393 交易日/69 次再平衡），ap-mcp 扩至 **16 工具** |
| **V4-β（Web 系统闭环）** | 实施书已定稿：**《AlphaPocket-V4β-Web系统实施方案.md》**（2026-10-05）——选型不变（Vue3 + NestJS BFF + FastAPI core 复用 applib 计算内核 + PG/Redis/MinIO/DuckDB + Docker），docker-compose 单机可运行；范围：新建组合/策略（股票+ETF/LOF 混合、权重归一）、AKShare 在线数据层（bootstrap/增量/刷新 + 六项验证自动门禁）、评估复盘、参数化回测（频率/基准/区间）、调仓全链（评估单→执行单→人工确认→成交登记，全程审计，引擎永不直接动仓位）；拆解为 32 个可独立验收任务（8 波次）供多模型并行实施；原 β 的 TOTP/PAT/RS256/PR 审查队列顺延 RC | `make up && make seed && make test-e2e` 全绿；浏览器完成「新建 multi5 复刻组合→数据同步（六验证全绿）→评估→回测（锚点断言）→执行单人工确认」全流程（实施书 §2.3 DoD） |
| **V4-RC（双运行时贯通）** | 任务包推送 + `ap daemon` + opencode/codex headless 自动化任务 + Browser Use 容器 + A2A 调研性 PoC | 自动化定制任务（夜间复盘）连续 2 周无人值守稳定运行 |
| **V4-GA** | 云端部署（阿里云）+ 备案 + 监控告警 + RDS/OSS 生产化 + 与引擎 A 实施计划 M0–M2 合流 | 生产域名可用；引擎 A M0 数据底座在 sandbox 内可复现 |

> 与《引擎 A 实施计划》的衔接：M0–M2（数据底座/注册中心/首批因子）**直接在 V4-α 的 sandbox 环境中实施**——本地先跑通，再随 V4-β 上云，避免"先建云再跑数"的空转。

---

# 附录 A · 调研来源（§6.1）

- [Multi Agent框架对比——Magentic-One、AutoGen…（CSDN）](https://blog.csdn.net/m0_59164520/article/details/145141543)
- [LangGraph、AutoGen 与 CrewAI：三大 Agent 框架对比（CSDN）](https://blog.csdn.net/weixin_53902256/article/details/158235302)
- [Agent Orchestrator：LangGraph vs CrewAI vs OpenAI Agents（CSDN）](https://blog.csdn.net/gedonshen/article/details/165078434)
- [2026年Agent框架横评：LangGraph、CrewAI、OpenAI…（CSDN）](https://blog.csdn.net/m0_50709695/article/details/163670481)
- [多智能体框架调研：OpenAI Agents SDK 与 LangGraph（CSDN）](https://blog.csdn.net/FENGQIYUNRAN/article/details/166692426)
- [详细介绍 Agent to Agent（CSDN）](https://blog.csdn.net/m0_66858441/article/details/155357756)
- [A2A vs MCP：两大Agent协议如何构建AI协作生态（CSDN）](https://blog.csdn.net/EnjoyEDU/article/details/155377428)
- [一张「神图」秒懂：Agent、A2A Protocol、MCP](https://fishc.com.cn/thread-249833-1-1.html)
- [五分钟搞清楚 Agent、A2A、MCP 和 Skills 的概念与区别](http://m.toutiao.com/group/7600706861050790442/)
- [OpenCode AI 编程助手使用教程（CSDN）](https://blog.csdn.net/love___me/article/details/158889249)
- [OpenAI Codex CLI 完全指南（博客园）](https://www.cnblogs.com/ljbguanli/p/19439518)
- [OpenAI Codex 完整指南（多 surface）](http://m.toutiao.com/group/7648850959435334179/)
- [Browser Use、Computer Use 还是 Sandbox？（CSDN）](https://blog.csdn.net/gitblog_00093/article/details/161097881)
- [BrowserUse + AgentRun Sandbox 最佳实践（博客园）](https://www.cnblogs.com/alisystemsoftware/p/19533518)
- [agent-sandbox：E2B 兼容的企业级 AI Agent 沙箱](http://m.toutiao.com/group/7634400797560570420/)
- [MLflow 实验管理（版本/checkpoint 模式参照）](https://www.cnblogs.com/pass-ion/p/19322135)

# 附录 B · 免责与合规

本方案为技术设计文档；所有涉及数据获取的自动化（含 computer use）以**遵守目标站点服务条款与robots 声明**为前置；产品输出为数据分析与工具，不构成投资建议；券商连接仅只读取数，不触碰交易通道。

# AlphaPocket · V4-α 优化建议报告

| 项 | 内容 |
| --- | --- |
| 评估对象 | V4-α 原型验证交付（2026-10-04 真实数据版）：ap CLI + ap-mcp 14 工具 + AKShare 数据接入 + 引擎 A/B + 六项验证 + cn4_review 报告 |
| 评估方法 | ① 全量源码走查（applib 11 模块）② 产物核验（run_20261004_082556_93dd6d 等 6 次 run 的报告/日志/manifest）③ 与《AlphaPocket-V4-完整版增强技术方案》§7.3/§11.4/§12/§13/§15/§16 及《引擎 A 实施计划》M0–M3 逐条对照 |
| 总体结论 | **α 阶段判据全部达成**（端到端全绿、六项验证 pass、真实数据可复现）。优化空间集中在四类：**纪律执行靠约定不靠代码**（1 处红线漏洞）、**数据时效与口径精度**（PIT 推算/单源/股本变动）、**因子只有快照没有序列**（M2 检验与 M3 回测的地基缺失）、**工程化欠账**（测试/依赖/CI/日志）。共 26 项建议，按 P0（4）/P1（7）/P2（8）/P3（7）分级 |

> 阅读方式：P0 = 正确性/纪律红线，建议立即修复；P1 = 数据质量，β 前完成；P2 = 对齐方案承诺的能力补全；P3 = 工程化，持续进行。每项均给出代码证据与可执行动作。

---

## 1. 总体评价：α 判据达成情况

| §16 V4-α 判据 | 现状 | 证据 |
| --- | --- | --- |
| cn4 组合复盘端到端全绿 | ✅ 达成 | run_20261004_082556_93dd6d：6 步全 ok，elapsed 28.17s，status=succeeded |
| 六项验证 pass | ✅ 达成 | golden 52 项 · cross_source 40 点 · adjust 5531 点（最大偏差 0.2339%）· calendar 末根=2026-09-30 · provenance 5 数据集 · pit_audit pass |
| 报告经浏览器渲染核验 | ✅ 达成 | HTML/MD 双格式 + review.json/rebalance_plan.json 落 artifacts |
| AKShare 真实数据 + provenance | ✅ 达成 | 新浪 qfq/raw + 腾讯交叉源 + 逐期财务 + 分红 + 交易日历，sha256 溯源登记 |
| PIT 阶梯 TTM / 五年分位 / 五维评分 | ✅ 达成 | factors.compute_alpha 全链路，报告 §③ 完整呈现 |

**亮点**（值得保持并固化为惯例）：双实现 golden 对账、跨源交叉验证、复权一致性校验、报告头部可复现四元组、"增强不决策"边界在代码与文案中双重贯彻。

---

## 2. P0 · 正确性与纪律红线（建议立即修复，粒度：天）

### P0-1 「全绿才允许 push」未在代码强制 —— 纪律漏洞

- **证据**：`applib/cli.py` `cmd_push` 取最近 checkpoint 附带 verify 后无条件创建 CR；`applib/repo.py` `repo_push` 在**无 run_manifest 时硬编码** `review = {"pit_audit": "pass", "golden_test": "pass", ...}` 字面量——即从未跑过验证也能 push 出一份"全 pass"的 CR。verify 含 FAIL 时 CR 照常创建，仅状态 pending_human。
- **风险**：直接违反方案 §15.2（任一红→禁止 push）与 §12.6（PAT 最小 scope 纪律）的设计意图；演示场景之外的真实使用中，verify_failed 的产物可能进入 PR 队列。
- **建议**：`repo_push` 入口强制检查：① 存在 run_manifest；② `manifest.status == "succeeded"`；③ verify 全 pass——三者缺一即拒绝并返回原因。同时删除字面量 pass，加一条单测覆盖"verify_failed → push 被拒"。

### P0-2 组合净值合成对停牌/缺 K 线不健壮（潜在崩溃）

- **证据**：`applib/tools.py` `portfolio_review` 中 `rs = [next(r["pct"] for r in members[s] if r["dt"] == d) for s in members]` —— `next()` **无 default**。日期轴取第一个标的的全量日期，任一标的在某日缺 K 线（停牌、上市晚）即抛 `StopIteration`，整个 review 崩溃。下一行的 `if r is not None` 过滤说明本意是容错，但 next 本身先炸。
- **建议**：`next(..., None)` 兜底；日期轴改为四标的日期**交集**；停牌日按"收益 0、权重冻结"口径处理并在 methods 注明。

### P0-3 `max_dd_1y` 名不副实

- **证据**：`applib/tools.py` 中 `cum/peak/mdd` 循环对**全部历史**净值序列计算回撤（数据从 2021 年起），字段名与报告展示却是一年最大回撤。
- **建议**：限 252 交易日窗口重算；或改名 `max_dd_full` 并在报告注明窗口。当前值 -33.6% 是 5 年口径，与"近一年"并列展示会误导决策。

### P0-4 golden_test 覆盖不全（52/64）且与生产实现同构

- **证据**：`applib/runner.py` `_golden_test` 的 cmp 列表覆盖 13 因子×4 标的=52 项，**qua_margin、grow_np_yoy、val_pb_pct 三个因子未对账**。更深层问题：TTM 拼接逻辑（`ttm_at` vs `factors.ttm_eps`）是复制粘贴级同构代码，**同源算法错误无法检出**（common-mode failure）。
- **建议**：① 补齐 16 因子全覆盖（4 因子直接透传 dwd 字段，对账成本极低）；② 增加少量**手工核算的固定样例**（构造 3–5 期报告 + 价格，人工算好 PE-TTM/分位期望值）作为第二层 golden，防同源错误——这是"双实现交叉"纪律的完整形态。

---

## 3. P1 · 数据质量与 PIT 精度（β 前完成，粒度：周）

### P1-5 PIT 公告日是推算而非事实，年报滞后最坏 4 个月

- **证据**：`applib/datasource.py` `PIT_LAG_DAYS = {3:30, 6:62, 9:31, 12:120}` 按监管截止日兜底。FY+120 天 = 4-30 才可用，而 A 股年报大多 3 月底前披露；1–4 月期间 TTM 只能靠上年三季报拼接，PE/ROE 时效性系统性打折。
- **建议**：接入真实披露日（AKShare `stock_report_disclosure` 或东财公告接口）作 `announce_date` 事实值，监管截止日仅作 fallback，并在 provenance 增加 `announce_source` 字段区分 fact/estimated。**这是 α 数据面性价比最高的一项升级**。

### P1-6 全量重建式同步 + fetch 清空版本历史

- **证据**：`datasource.fetch_all` 每次 `shutil.rmtree(repo)` 整体重建，且**删除 refs.json/crs.json**（远端 commit 链与 CR 历史一并清零，违背 append-only 纪律）；`data.data_sync` 每次 DROP+CREATE 全量重插。
- **建议**：① 引入水位（watermark）：行情按 `max(dt)` 增量拉取；② `data.sync` 改 upsert；③ refs/crs 与数据重置**解耦**——数据集换版走新 dataset commit，版本历史永不清空（对齐方案 §13.1 "dataset@commit 指针"）。

### P1-7 JSON 明细文件承担数据集角色，不可扩展

- **证据**：daily_bars/daily_bars_raw/financial 等 5 个大 JSON（合计约 1.4 万行）全程"读全量→插 DuckDB→golden 再读全量"。4 标的×5 年尚可，扩到 50 标的或全市场即内存/耗时爆炸，且与方案 §13.1 "Parquet 块" 自述不符。
- **建议**：行情/财务落 **Parquet 分区文件**（按 secid 或年分区）+ 清单（dataset@commit）；JSON 仅保留维度表与小配置。golden_test 改读 Parquet/polars，顺带解决逐日 `pd.Timestamp→str` 的性能热点（当前 28s 中 golden 占大头）。

### P1-8 财务数据单源，无交叉验证

- **证据**：行情有新浪/腾讯双源对账（cross_source），但 eps/bps/roe 等财务字段仅新浪 `stock_financial_analysis_indicator` 一个来源——而财务恰恰是 PIT 审计与五维评分的根基。
- **建议**：引入第二实现（如东财 `stock_financial_abstract`）对关键字段（eps_ytd/bps/rev_g）做逐报告期抽样对账，纳入第七项验证 `financial_cross_check`；不一致率超阈值降级为 warning 并在报告标注。

### P1-9 除权日集合不完备，adjust_check 阈值余量不足

- **证据**：`runner._adjust_check` 的排除集合 `ex` 仅来自**分红记录**（`fund[secid]["dividends"]` 的 ex_date），纯送股/转增的除权日若与派息不同日则漏排；实测最大偏差 **0.2339%**，距 0.5% 阈值余量仅一半，一次送转即可击穿产生误报。
- **建议**：排除集合改为"除权除息日"全集（含 song/zhuang 记录）；阈值分级：纯派息日 0.5%、含送转日 2%；偏差落在 0.5%–2% 的点输出明细供人工复核而非直接 FAIL。

### P1-10 股息率 TTM 用 365 自然日窗口，跨年节奏漂移

- **证据**：`factors.compute_alpha` 取 `[as_of-365d, as_of]` 闭区间除权除息派息合计。分红节奏变化（如某年 5 月、次年 6 月）时漏计一期或重计一期，永新 7.3%、分众 8.2% 这类高股息读数对窗口口径敏感。
- **建议**：改为"最近 4 个完整报告期宣告分红"或 252 交易日窗口；至少在 provenance.methods 中把当前口径写明（现已部分写明，补窗口敏感性说明）。

### P1-11 TTM EPS 未校正股本变动，直接污染 PE 及五年分位

- **证据**：`ttm_eps = 上年FY + 本年YTD − 上年同期YTD`（EPS 直拼），报告已自注"未逐笔校正股本变动"。送转/增发年份 YTD 相减失真——**永新股份 PE 五年分位 99% + 股息率 7.3% 并存**这类极端组合读数，值得先复核是否口径问题再当作投资信号。
- **建议**：改用"归母净利润 TTM ÷ 最新总股本"口径重算（净利润不受股本变动影响的拆分失真），或按股本变动日重述历史 EPS；在报告 §③ 注明口径变更并跑一次前后对照（旧口径值保留一版供 diff）。

---

## 4. P2 · 能力补全：对齐方案承诺（随 β 推进）

### P2-12 因子历史面板缺失 —— M2/M3 的地基，本报告最高优先的结构性建议

- **证据**：`tools.factor_compute_alpha` 对 `factor_value_daily` 做 **DELETE+INSERT 单日快照**——表里永远只有 as_of 一行的 16 因子值。而 `compute_alpha` 内部明明已算出 pe_s/pb_s **全历史 PIT 序列**却只取末值丢弃。
- **影响**：引擎 A 实施计划 M2（IC/分层检验）、M3（回测）、因子降级告警（§12.2 event 触发器）全部无数据基础；"五年分位"每次全窗重算也源于此。
- **建议**：compute_alpha 增量落 **PIT 因子面板** `(dt, secid, factor_id, value, used_period)` 全历史截面（一次性回填 + 此后每日增量）；表上加 (factor_id, dt) 索引。分位、IC、回测从此全部变为面板上的确定性查询。**这是 α→β 最值得投入的一步。**

### P2-13 报告缺「策略表现」章节（方案 §7.3 明确承诺）

- **证据**：§7.3 报告规格含"净值 vs 复合基准、归因四层（市场/行业/个股/汇率）、换手与成本"；α 报告②组合体检无任何基准对照（沪深300 都没有），归因与超额收益缺失。
- **建议**：AKShare `stock_zh_index_daily`/`index_zh_a_hist` 拉 沪深300 同窗净值，报告②并列组合 vs 基准累计曲线与超额；归因四层可后置，但基准对照应随 β 报告模板补齐。

### P2-14 信号冷却期无状态，notify 落地后将被放大为骚扰

- **证据**：`factors.compute_beta` 生成的信号自带 `cooldown: "10 交易日"` 字段，但无任何存储与检查——赛轮当前 below 状态意味着**连续 10+ 个交易日每天重复触发同一预警**。
- **建议**：signals 状态表（signal_id, secid, first_fired_at, last_fired_at, cooldown_until），review 时读表去重；状态表随 checkpoint 版本化。

### P2-15 拟调仓规则硬编码，未与引擎 A 联动

- **证据**：`tools.portfolio_rebalance_plan` 中 below → `weight × 0.6` 系数写死，所有标的同一规则；引擎 A 五维/综合评级未参与（方案中风险官规则要求联动）。
- **建议**：规则参数化进 strategy 配置（对齐 Strategy DSL 方向）；示例联动：综合 D 且 below → ×0.5，综合 B 且 below → ×0.75，综合 A 且 below → 仅观察。每条 trade 的理由链已具备（保持），补规则版本号。

### P2-16 MCP 工具 inputSchema 过弱，IDE agent 只能猜参数

- **证据**：`mcp_server._tool_desc` 所有工具 schema 均为 `{"args": {"type": "object"}}`，且调用需包一层 `arguments.args`。宿主 agent 无法得知 as_of/portfolio/factors 的取值含义，传错率高。
- **建议**：逐工具写具体 JSON Schema（as_of 格式、portfolio 枚举、factors 列表引用 registry id）；参数平铺去掉 args 包装（保留一版兼容期）。工具数 14 → 方案 §11.4 的 24（缺 data.backfill/backtest.* 等）列入 β/RC 待办即可。

### P2-17 notify 声明了但未实现

- **证据**：`tasks/review_cn4.yaml` 声明 `notify: {on: [risk_warn, signal_fired], channel: [cloud_inbox]}`，`runner.run_task` 完全未读取该字段。当前赛轮卖出预警只能靠人主动看报告。
- **建议**：α 内先落本地通知（signals 落盘 + 桌面通知 notify-send/osascript + `ap status` 汇总），`cloud_inbox` 通道留 β。通知分级（info/action_required/risk_warn）随 §12.7 一并定义。

### P2-18 checkpoint 仍是"重放说明"，四元组两处不实

- **证据**：① `checkpoint_restore` 仅回显重放命令字符串；② `ctx.CODE_VERSION = "v4-alpha-real+r2a4e91c"` 是**手写常量**而非 git rev-parse；③ manifest 中 `factor_versions: "见 registry"` 为占位。
- **建议**：code_version 改 `git rev-parse HEAD`（无 git 环境则构建时注入）；manifest 记录 registry 全量 (factor_id→version) 快照 + task yaml 哈希；restore 至少校验当前 dataset sha256 与 checkpoint 记录一致后再给重放指令。

---

## 5. P3 · 工程化（持续）

| # | 问题 | 证据 | 建议 |
| --- | --- | --- | --- |
| P3-19 | 无单元测试/CI | 六项验证是运行时验证，不是回归测试；重构因子口径（如 P1-11）无安全网 | pytest 覆盖：PIT 阶梯边界、TTM 拼接（含股本变动样例）、分位/评分锚点、_grade/_interp 边界、P0-1 的 push 门禁；CI 一条 `make test` 即可 |
| P3-20 | 无依赖声明与版本锁定 | akshare/duckdb/pandas/pyyaml 无 requirements.txt；akshare 上游接口易变 | pyproject + 锁版本；akshare 升级走独立 PR 并重跑六项验证作回归 |
| P3-21 | 模拟数据与真实数据边界脆弱 | `repo_clone` 仍调 `seed_remote`，仅靠 provenance.json 存在性判断是否注入模拟数据 | clone 加显式 `--seed` 开关，默认拒绝注入；seed 路径仅用于 demo 脚本 |
| P3-22 | 性能：净值合成 O(N²)、golden 逐日重扫 | `portfolio_review` 每日期×每标的线性扫描；`_golden_test` 逐日 `pd.Timestamp→str` 转换 | 净值合成改 pivot/groupby 一次对齐；golden 改向量化（依赖 P1-7 Parquet + P2-12 面板后自然消解） |
| P3-23 | Python 版本漂移 | `__pycache__` 为 cpython-314，方案 §15.1 镜像为 python:3.11 | 统一 3.11 并写入 quant-std profile；CI 用同版本 |
| P3-24 | 无日志分级 | 全链路 print；MCP server 无请求日志，IDE 侧调试全靠猜 | logging 分级（DEBUG/INFO/WARN/ERROR）；MCP 每请求记 (method, tool, elapsed, ok) 一行 |
| P3-25 | 报告小项 | ① HTML 每次全量内嵌 CSS；② MD/HTML 均无五维雷达图（v3.1 原型有）；③ `report.read` 截前 4000 字符，agent 解读不完整 | CSS 提取共享常量；雷达图以内联 SVG 生成（无需外部库）；report.read 按 run_id+章节参数化读取 |

---

## 6. α→β 桥接清单（与 §16 路线图逐项对齐）

| 方案承诺 | α 现状 | 缺口动作 | 建议归属 |
| --- | --- | --- | --- |
| §15.2 验证全绿才允许 push | 靠自觉，代码放行 | P0-1 门禁 + 单测 | **立即** |
| §13.1 dataset@commit / Parquet | JSON 全量 + fetch 清史 | P1-6/P1-7 水位增量 + Parquet | β 前 |
| §13.3 PR 审查自动化 | review 为字面量/透传 | P0-1 修复后，CR 审查改为重跑 verify 套件 | β |
| §11.4 24 工具（backtest.*/data.backfill） | 14 工具 | P2-12 面板就绪后 backtest.* 才有意义 | β/RC |
| §7.3 报告含策略表现/归因 | 无基准对照 | P2-13 沪深300 对照先行，归因四层后置 | β |
| §12.2 event 触发器（因子降级告警） | 无因子历史，无从降级 | P2-12 面板 + IC 巡检规则 | RC |
| §12.6 PAT 最小 scope / 密钥 | auth 纯模拟（α 预期内） | Device Code Flow 随云端一起做 | β |
| §16 M0–M2 与 sandbox 合流 | M0 数据底座已具雏形 | P1-5 真实公告日 + P2-12 面板 = M0 完整形态 | β 前 |

## 7. 建议执行顺序

```
第 1 周   P0-1 push 门禁 → P0-2 净值健壮性 → P0-3 回撤口径 → P0-4 golden 补全
          （+ P3-19 顺势补首批单测：这四项各配一条测试）
第 2 周   P1-5 真实披露日（性价比最高） → P1-9/P1-10 除权与股息口径 → P1-11 EPS 口径重算（带前后对照）
第 3–4 周 P1-6/P1-7 增量同步 + Parquet → P2-12 PIT 因子面板（M2 地基，最大单项投入）
          → P2-13 基准对照 → P2-14 信号冷却
随 β 并行 P2-16 MCP schema → P2-17 本地通知 → P2-18 checkpoint 实质化 → P3 工程化持续
```

> 排序原则：先堵纪律漏洞（否则后续一切验证都无强制力），再修数据口径（否则因子面板会带着错误口径固化为历史），最后扩能力——**P2-12 因子面板务必在 P1 口径修复之后落库**，避免污染的面板需要整体重算。

---

## 附录 · 证据索引（关键代码位置）

| 建议项 | 代码位置 |
| --- | --- |
| P0-1 | `applib/cli.py` cmd_push · `applib/repo.py` repo_push（字面量 review） |
| P0-2 / P0-3 | `applib/tools.py` portfolio_review（next 无 default · mdd 全历史循环） |
| P0-4 | `applib/runner.py` _golden_test（cmp 覆盖列表） |
| P1-5 | `applib/datasource.py` PIT_LAG_DAYS / _announce_date |
| P1-6 | `applib/datasource.py` fetch_all（rmtree + refs 清空） · `applib/data.py` data_sync（DROP+CREATE） |
| P1-9 / P1-10 / P1-11 | `applib/runner.py` _adjust_check（ex 集合） · `applib/factors.py` div_yield 窗口 / ttm_eps |
| P2-12 | `applib/tools.py` factor_compute_alpha（DELETE+INSERT 单日） · `applib/factors.py` compute_alpha（pe_s/pb_s 弃用） |
| P2-14 / P2-15 | `applib/factors.py` compute_beta（cooldown 无状态） · `applib/tools.py` portfolio_rebalance_plan（×0.6 硬编码） |
| P2-16 / P2-18 | `applib/mcp_server.py` _tool_desc · `applib/ctx.py` CODE_VERSION 常量 |
| 运行产物 | `alphapocket/sandbox/artifacts/run_20261004_082556_93dd6d/`（report.md · review.json · rebalance_plan.json）· `sandbox/logs/` 同名日志 |

*本报告为技术评估文档，不构成投资建议；所有问题定位基于 2026-10-04 交付快照（code v4-alpha-real+r2a4e91c）。*

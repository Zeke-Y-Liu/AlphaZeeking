# 方案：单一公司分析（Company Analysis）

> 需求来源：用户 Goal（2026-10-21）。状态：方案定稿，任务拆解见文末——每个任务可直接投喂 flash 模型独立执行。

## 0. 需求

1. 单一公司四大分析：基本面、行业趋势（须有表达行业现状的数据）、短线是否超卖、最新消息
2. 单一公司分析实体可增删改查、支持回测
3. 单一公司相关消息（来自数据平台）

## 1. 现状盘点（复用而非重建）

| 能力 | 现状 | 复用方式 |
|---|---|---|
| 标的注册/检索 | `securities` 表 + search/register API | 分析档案关联 secid，不重建标的 |
| 行情 | `market/daily_bars.json`（qfq，25 标的全绿） | 技术指标、回测直接读 |
| 基本面 | `factors/fundamental_values.json`（reports 序列） | 基本面模块直读 |
| 行业归属 | `dim/industry_sw.json`（申万 PIT 变迁史，altdata-sync） | 行业模块定位标的行业 |
| 行业现状数据 | **无**（缺行业指数/资金流） | 需新增数据集 T1.1 |
| 个股新闻 | **无** | 需新增数据集 T1.2 |
| 技术指标 | factors.py 仅 rev_20d/rev_60d/mom 等 | 需补 RSI/KDJ 纯函数 T2.3 |
| 回测 | 仅按 portfolio（`POST /portfolios/{pid}/backtest`） | 影子组合方案零改动复用 |
| 组合 CRUD | 完整（create/update/delete 软删 archived） | 分析档案挂影子组合 |
| job 框架 | `jobs.submit(type=...)`，已有 5 种 type | 新增 type="analysis" |
| 前端 | 12 个 views，router/api client 模式成熟 | 新增 2 页面 |
| bff | `@All('api/*')` 通配透传 | **无需改动** |

## 2. 架构设计

```
web (CompanyList / CompanyDetail)          ← 新增 2 页面
  └─ bff（通配透传，零改动）
      └─ core /api/v1/analysis/*            ← 新增 API 模块
          ├─ CRUD：analysis_profiles 表 + 影子组合生命周期
          ├─ run → job(type="analysis") → analysis_svc 四模块 → runs+artifacts
          ├─ news：读 stock_news 数据集 + 手动刷新
          └─ backtest：复用影子组合 → 现有 backtest 全链路（零改动）
applib（α 库，数据与计算）
  ├─ 新数据集：industry_index（行业现状）/ stock_news（个股新闻）
  └─ technical.py：RSI/KDJ/超卖评分纯函数
```

### 2.1 核心决策：影子组合（Shadow Portfolio）

AnalysisProfile 创建时自动建一个**单标的 100% 组合**，strategy 元数据标记 `{"kind": "analysis_shadow", "aid": <aid>}`：

- 回测、评估（review）、调仓全部**零改动复用**现有链路
- 组合列表 API 与前端默认按 strategy.kind 过滤影子组合（分析页单独展示）
- 删除档案 → 级联归档影子组合（status="archived"）
- 审计双落：entity="analysis_profile" + 组合侧自动继承

### 2.2 数据模型

```python
# core/app/models/analysis.py —— AnalysisProfile
id, aid(str unique), secid(str, FK 语义指 securities), name, note,
modules: dict  # {"fundamental": true, "industry": true, "technical": true, "news": true}
status: str    # active / archived
created_at, updated_at
```

aid 生成：用户可指定，缺省 `c{4位序号}`（svc.suggest_aid，参考 suggest_pid）。

### 2.3 四大分析模块（analysis_svc，job type="analysis"）

| 模块 | 输入（已有） | 输出 |
|---|---|---|
| 基本面 | fundamental_values.reports（近 4 期）+ dim_market_cap | ROE/营收同比/净利同比/毛利率趋势 + 市值 |
| 行业趋势 | **industry_index（新）** + industry_sw（PIT 行业） | 行业指数近 20/60 日收益、行业成交额趋势、标的 20 日收益在行业内的分位 |
| 短线超卖 | daily_bars + **technical.py（新）** | RSI(14)、KDJ(9,3,3)、rev_20d、距 20 日高点回撤 → 超卖判定（RSI<30 或 J<0 或 回撤>15% → oversold=true） |
| 最新消息 | **stock_news（新）** | 最近 10 条：时间/标题/来源/链接 |

产物：`runs` 记录（type="analysis"）+ MinIO artifact `analysis_report.json`。评估不设门禁阻断（同 review 的 data_risk 提示模式）。

### 2.4 新数据集

**industry_index**（行业现状，表达"行业现状的数据"）
- path：`industry/industry_index.json`；结构 `{板块名: [{dt, close, pct, amount}, ...]}`
- merge_policy：`overwrite`（板块数固定、窗口截尾 250 日，全量小）
- source：akshare 东财 `stock_board_industry_hist_em`（universe.py 已用 `stock_board_industry_name_em` 验证过板块名可用性）
- 覆盖板块：universe 内标的所属行业（由 industry_sw/静态 industry 推导的板块名并集）

**stock_news**（个股消息，来自数据平台）
- path：`news/stock_news.json`；结构 `{secid: [{dt, title, content, source, url}, ...]}`
- merge_policy：`overwrite`（每标的保留最新 30 条快照）
- source：akshare `stock_news_em(symbol=<code>)`（东财个股新闻）
- 拉取时机：data_sync 增量顺带；另提供 `POST /analysis/{aid}/news/refresh` 单独小 job

### 2.5 API 契约

```
GET    /api/v1/analysis                 # 列表（active，含最近一次 run 摘要）
POST   /api/v1/analysis                 # 创建（secid 必填；同步建影子组合）
GET    /api/v1/analysis/{aid}            # 详情（含 modules/影子组合 pid）
PUT    /api/v1/analysis/{aid}            # 更新（name/note/modules）
DELETE /api/v1/analysis/{aid}            # 软删 + 级联归档影子组合
POST   /api/v1/analysis/{aid}/run       # 触发分析 job → {job_id}
GET    /api/v1/analysis/{aid}/news      # 最近消息（直读数据集，limit 默认 10）
POST   /api/v1/analysis/{aid}/news/refresh  # 手动刷新该标的新闻（小 job）
POST   /api/v1/analysis/{aid}/backtest  # = 转发现有 /portfolios/{shadow_pid}/backtest
```

### 2.6 前端

- `/companies` → CompaniesView：档案卡片列表、新建（内嵌 securities 搜索选择）、删除
- `/companies/:aid` → CompanyDetailView：四模块 tab（基本面/行业/技术/消息）+ 「运行分析」按钮（轮询 job）+ 「回测」跳转（带 shadow_pid 进 BacktestView）
- 组合列表页过滤 strategy.kind=="analysis_shadow"

## 3. 任务拆解（flash 模型可执行规格）

> 每个任务自包含：给出精确文件路径、函数/字段名、验收命令。执行顺序：T1.1 ∥ T1.2 ∥ T2.1 ∥ T2.3 → T2.2 → T2.4 → T2.5 → T3.1 → T3.2/T3.3 → T4.1

---

### T1.1 行业现状数据集 industry_index
- **依赖**：无
- **文件**：`/workspace/alphapocket/applib/datasets/specs_market.py`、`/workspace/alphapocket/applib/datasets/registry.json`、`/workspace/alphapocket/applib/datasource.py`（fetch_all 内追加）、`/workspace/alphapocket/applib/datasets/merge.py`（如需）
- **步骤**：
  1. specs_market.py 追加 `DatasetSpec(name="industry_index", path="industry/industry_index.json", category="market", merge_policy="overwrite", sources=[SourceRef(interface="ak.stock_board_industry_hist_em", upstream="东方财富", role="primary")])`
  2. datasource.py fetch_all 中实现 `_fetch_industry_index(boards)`：对每个板块名调 `ak.stock_board_industry_hist_em(symbol=板块名, period="日k", adjust="")`，取近 250 日 `{dt, close, pct, amount}`；板块名来源 = universe 各标的的行业（`dim/industry_sw.json` PIT 或 security.industry 静态标注）去重并集
  3. registry.json 登记（含 min_rows: 50 验证）；provenance 登记模式照抄现有数据集
- **验收**：`PYTHONPATH=/workspace/alphapocket core/.venv/bin/python -c "from applib.datasource import *"` 无异常；手动调用 fetch 后 `remote/repo/industry/industry_index.json` 存在且含 ≥1 个板块、每板块 ≥50 根

### T1.2 个股新闻数据集 stock_news
- **依赖**：无
- **文件**：同上 + 新目录 `industry/`、`news/`
- **步骤**：同套路：spec（path=`news/stock_news.json`，merge_policy="overwrite"）+ `_fetch_stock_news(secids)`：每标的 `ak.stock_news_em(symbol=code)`，保留最新 30 条 `{dt, title, content, source, url}`；fetch_params 注明"东财个股新闻，增量随 data_sync"
- **验收**：fetch 后 JSON 含 ≥10 个 secid；每条有 dt/title/url

### T2.1 AnalysisProfile 模型与迁移
- **依赖**：无
- **文件**：`/workspace/alphapocket-web/core/app/models/__init__.py`（导出）、新建 `core/app/models/analysis.py`、新建 alembic 迁移 `core/migrations/versions/*_analysis_profiles.py`
- **步骤**：字段见 §2.2；`core/app/db/session.py` 无需动；迁移照抄最近一个 migration 的表结构风格
- **验收**：`cd core && PYTHONPATH=/workspace/alphapocket .venv/bin/alembic upgrade head` 成功；psql `\d analysis_profiles` 显示全字段

### T2.2 CRUD API + 影子组合生命周期
- **依赖**：T2.1
- **文件**：新建 `/workspace/alphapocket-web/core/app/api/v1/analysis.py`、新建 `core/app/services/analysis_svc.py`（先只放 CRUD 辅助）、`core/app/api/v1/portfolios.py`（列表过滤 strategy.kind）
- **步骤**：
  1. `AnalysisCreate(name?, secid, note?, pid?)` / `AnalysisUpdate` / `AnalysisOut` pydantic 模型
  2. 五个端点（§2.5），删除走软删；创建时同步建影子组合（pid=`shadow_{aid}`，positions=[{secid, 100}]，strategy={"kind":"analysis_shadow","aid":aid}）
  3. AuditLog(entity="analysis_profile", action=create/update/delete)
  4. `GET /portfolios` 默认排除 strategy.kind=="analysis_shadow"（query 参数 include_shadow=false 缺省）
- **验收**：curl 全链路（登录→POST 创建→GET 列表→PUT→DELETE→再 GET 应 404）；组合列表不含影子组合、include_shadow=true 含

### T2.3 技术指标纯函数（applib/technical.py）
- **依赖**：无
- **文件**：新建 `/workspace/alphapocket/applib/technical.py`
- **步骤**：纯函数（无 IO）：`rsi(closes, n=14)`（Wilder 平滑）、`kdj(bars, n=9, m1=3, m2=3)`、`oversold_score(bars)`——组合 RSI(14)<30、J<0、rev_20d>0.08、距 20 日高点回撤>15% 四条件，返回 `{"rsi":…, "k":…, "d":…, "j":…, "rev_20d":…, "drawdown_20d":…, "oversold": bool, "signals": [...]}；样本不足返回 None 字段
- **验收**：`PYTHONPATH=/workspace/alphapocket core/.venv/bin/python -c "from applib.technical import rsi, kdj, oversold_score; print(oversold_score([{'close':10-i*0.1,'dt':f'2026-01-{i+1:02}','high':0,'low':0,'open':0} for i in range(30)]))"` 输出结构正确且下跌序列 oversold 可为 true

### T2.4 分析引擎（job type="analysis"）
- **依赖**：T1.1、T1.2、T2.2、T2.3
- **文件**：`core/app/services/analysis_svc.py`（主体）、`core/app/services/jobs.py`（注册 type="analysis"）、`core/app/api/v1/analysis.py`（run 端点）
- **步骤**：
  1. `run_analysis(db, job, params)`：物化 sandbox（照抄 backtest_svc L118-189 模式）→ 读四模块数据源 → 组装 `{"aid","secid","as_of","fundamental":{...},"industry":{...},"technical":{...},"news":[...]}` → 写 runs（type="analysis"）+ 上传 artifact `analysis_report.json`
  2. 基本面：fundamental_values.reports 取近 4 期（营收/净利/ROE 序列 + 同比）；行业：industry_index[板块] 近 20/60 日收益、amount 5 日均值/60 日均值、标的 pct 在板块 20 日收益中的位置；技术：technical.oversold_score；新闻：最近 limit 条
  3. `POST /analysis/{aid}/run` → jobs.submit(type="analysis", params={"aid":…})；data_risk 提示模式照抄 review.py
- **验收**：对 601058.SH 建档案 → run → job done → GET runs 含 type=analysis 记录 → artifact JSON 四模块齐全且 industry 模块含板块名与两个收益数字

### T2.5 新闻端点
- **依赖**：T1.2、T2.2
- **文件**：`core/app/api/v1/analysis.py`
- **步骤**：`GET /analysis/{aid}/news?limit=10`（读 sandbox 物化的 news/stock_news.json[secid] 切片，无数据时 data_risk 式提示）；`POST /analysis/{aid}/news/refresh` → 小 job（type="news_refresh"，仅刷该 secid 的 stock_news_em）——jobs.py 同步注册
- **验收**：refresh job done 后 GET news 返回 ≥1 条含 url

### T3.1 前端 API 封装
- **依赖**：T2.2（契约即可先行）
- **文件**：`/workspace/alphapocket-web/web/src/api/index.ts`
- **步骤**：追加 `listAnalyses/createAnalysis/getAnalysis/updateAnalysis/deleteAnalysis/runAnalysis/getAnalysisNews/refreshAnalysisNews/backtestAnalysis`（路径常量 + apiClient 调用，照抄 portfolios 封装段 L146-201）
- **验收**：`pnpm -C web build` 通过（或 vue-tsc 无类型错）

### T3.2 公司列表页
- **依赖**：T3.1
- **文件**：新建 `web/src/views/CompaniesView.vue`、`web/src/router/index.ts`（`/companies` 与 `/companies/:aid` 两路由，注意动态路由注册顺序）
- **步骤**：卡片列表（name/secid/最近 run 状态/四模块开关徽章）+ 新建对话框（内嵌现有 securities 搜索）+ 删除确认；loading/error 模式照抄 DataView.vue L28-35
- **验收**：浏览器走通 新建→列表出现→删除消失

### T3.3 公司详情页（四模块 tab + 回测）
- **依赖**：T3.1、T2.4
- **文件**：新建 `web/src/views/CompanyDetailView.vue`
- **步骤**：顶部档案头 + 「运行分析」按钮（提交后轮询 job 进度，模式抄 DataView runSync）+ 四 tab：基本面（近 4 期表）、行业（板块名/20日/60日收益/成交额趋势）、技术（指标卡 + oversold 红绿徽章）、消息（列表 + 「刷新」+ 外链）；「回测」按钮带 shadow_pid 跳 `/backtest?pid=shadow_{aid}`
- **验收**：对 601058.SH：运行分析 → 四 tab 有数据、技术徽章正确；点回测跳转后可正常跑

### T4.1 回归验证与文档
- **依赖**：全部
- **文件**：`/workspace/alphapocket-web/docs/MANUAL-TEST-CHECKLIST.md`（追加 M10 公司分析章节）、本方案文件状态更新
- **步骤**：M10 用例：M10-1 档案 CRUD、M10-2 四模块分析、M10-3 新闻刷新、M10-4 单标的回测、M10-5 影子组合隔离（组合页不显示）
- **验收**：现有 M0-M9 用例无回归（重点：组合页列表过滤不影响存量组合、六项验证仍全绿——data_sync 新增两数据集不破坏 provenance）

## 4. 风险与边界

- **akshare 接口可用性**：stock_news_em/stock_board_industry_hist_em 若限流 → fetch 失败不阻断 data_sync（照抄现有软性模式），analysis 对缺失模块降级提示
- **美股/港股新闻**：stock_news_em 仅 A 股——港美标的新闻模块返回 data_risk 式提示"暂无该市场新闻源"（不阻断）
- **行业现状表达**：以板块指数动量 + 成交额趋势为主；若需估值分位/资金流，留作后续扩展（数据集已留 overwrite 语义可加字段）
- **DAVE.US 拆股遗留**：跨 2023-01 长窗口技术指标已知失真（前已报备），单公司分析若遇 DAVE 类标的在 UI 提示

## 5. 实现状态（2026-10-21）

全部任务已实现并通过验证（T1.1 / T1.2 / T2.1 / T2.3 / T2.2 / T2.4 / T2.5 / T3.1 / T3.2 / T3.3 / T4.1）。

### 5.1 实现期偏离（方案 → 实际）

| 项 | 方案 | 实际 | 原因 |
|---|---|---|---|
| 行业数据源 | 东财板块指数（`stock_board_industry_*`） | **申万一级行业指数**（`ak.index_hist_sw`） | 实测东财 push2 域名在沙箱出口 ProxyError 不可达（`stock_individual_info_em` 同样失败）；申万指数接口可用且全历史 |
| 行业归属 | 东财「行业」字段与板块名同源 | 静态标注 + **巨潮** `stock_profile_cninfo`「所属行业」→ `applib.industry.match_sw_l1` 归一 | 同上；巨潮接口可达 |
| 新闻 | 东财个股新闻 | 同（`ak.stock_news_em`，域名可用） | — |
| 新闻刷新 | 小 job 仅刷该标的 | 写 remote+sandbox 两份并同步 provenance 的 sha256/bytes | 否则 provenance_check 哈希不符转红 |

### 5.2 实现期修复（顺带）

1. **ETF/LOF 历史截断（既有 bug）**：基金分支按增量起点 `s0` 切片，`daily_bars` 的 `new_nonempty` 语义会用数根增量窗口整体替换旧序列 → ETF 1400+ 根被截为 7 根、`golden_test` 越界崩溃。修法：基金与 A 股 qfq 同口径按 **全量起点** 切片（无复权漂移，不需增量窗口）。
2. **DAVE 拆股豁免持久化**：2023-01-06（1:28 反向拆股）上游事件元数据缺失 → 豁免每次 bootstrap 即丢。改为代码内置 `_KNOWN_EX_DATES` 常量并在每次拉取后并入 `adjust_ex_dates`（仅 universe 内含该标的时）。
3. **`analysis_profiles` 纳入契约快照**：`test_models` / `test_migrations` 的表清单、`openapi.json` 快照随新端点刷新。
4. **`test_seed` 计数口径**：改为「seed 基线 ≥ + 二次 seed 不变」，兼容运行期用户新增标的/组合（原精确 15/3/15 只在纯净库成立）。

### 5.3 验证结果

- 数据：25 标的 has_bars 25/25，as_of 2026-10-21；六项验证全绿（provenance 14 数据集、adjust 25598 点·毛刺豁免 4 点、golden 339 项）。
- 分析（A 股 601058.SH）：基本面 2026-06-30 期（ROE 9.5%）；行业「汽车」（20 日 -6.1%、成交额 5/60=0.94、相对强度 -4.5%）；技术 RSI 33.7 / J 18.9 / 20 日超跌 10.6%（超卖）；消息 10 条。
- 分析（港股 00700.HK）：新闻模块降级「该市场暂无公开新闻源」，其余模块正常。
- 影子回测：`POST /analysis/ca_1/backtest` → job done，产出 `bt_20261021_172037_ac7969`。
- 自动化：core `190 passed / 13 skipped`；applib 数据集 + 技术/行业单测 `39 passed`；web `pnpm build` 通过（新文件无类型错误）。
- 浏览器：登录 → /companies 列表 → 新建 ca_2 → 详情四 tab 数据正确 → 运行分析 2–3 秒完成，console 无报错。


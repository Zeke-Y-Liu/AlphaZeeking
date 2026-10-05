# AlphaPocket V4-β · Web 系统实施方案
## 基于 V4-α 双组合实测能力 · 完整可运行 · 可分解任务版

- 版本：v1.0（2026-10-05）
- 上游文档：《AlphaPocket-V4-完整版增强技术方案》（下称"主方案"）——**系统架构设计选型保持不变**，本方案是其 V4-β 阶段的落地实施书
- 实测输入：V4-α 真实数据版已跑通 cn4（股票四标的）/ multi5（多资产五标的）双组合的 **评估复盘、六项验证、回测、调仓执行单** 全链路
- 执行者：本方案面向**逐任务分派给独立 Flash 模型实施**——每个任务（§9）自带目标、产出、接口、验收命令与实测坑提示

---

# 0. 文档使用说明（实施前必读）

1. **唯一事实源**：§4（存储分治）、§5（Schema 与数据契约）、§6（API 契约）为**冻结契约**。实施任务时若发现契约缺陷，不得单方面变更——在任务交付说明中登记问题，由协调方修订本方案后重新分派。
2. **任务纪律**：一个任务 = 一个可独立验收的交付单元。验收标准全部是**可执行命令**，全绿才算完成。禁止超出任务边界的"顺手改进"。
3. **全局禁止事项**（违反即打回）：
   - 不得改动主方案已定的技术选型（Vue3 / NestJS BFF / FastAPI / PostgreSQL / Redis / 对象存储(MinIO↔OSS) / DuckDB / Docker / AKShare 数据源）；
   - 不得 fork 复制 `alphapocket/applib`（Web 服务只能引用，单一事实源）；
   - 不得绕过六项验证门禁（数据不绿 → 拒绝生成新 run）；
   - 不得引入任何券商交易接口（引擎永不直接动仓位）；
   - 不得在东财系 AKShare 接口上做任何依赖（α 实测网络不可达，见 §1.3）。
4. **术语**：`core` = FastAPI 量化服务（含数据层）；`bff` = NestJS 网关；`web` = Vue3 前端；`workspace` = 一次运行的工作区目录（α 的 `sandbox/` 概念推广）。

---

# 1. 背景与输入：V4-α 已验证资产

## 1.1 代码资产（`/workspace/alphapocket/applib/`，16 模块 ≈3.5k 行，Python 3.12+）

| 模块 | 能力（已实测） | Web 阶段角色 |
| --- | --- | --- |
| `datasource.py` | AKShare 新浪行情(qfq+raw)/腾讯交叉源/逐期财务/分红/交易日历/ETF·LOF 行情 + provenance 溯源 + 份额折算检测 | core 数据同步 job 直接复用，需 universe 参数化（T1.4） |
| `factors.py` | 引擎A（估值/质量/成长/动量/红利低波/五年分位/五维评分，PIT 阶梯）+ 引擎B（20日信号） | 原样复用 |
| `tools.py` | 16 个工具：review / rebalance_plan / exec_orders / backtest / report / repo / checkpoint（MCP 暴露面） | core 内部调用入口，去全局态（T0.3） |
| `backtest.py` | 等权月度再平衡回测 vs 沪深300ETF：净值/年化/波动/夏普/索提诺/卡玛/回撤三段/月度年度表 | 复用 + 参数化（T3.1） |
| `exec_orders.py` | 目标权重→金额→整手(100份)委托：佣金万2.5 最低¥5、印花税仅股票卖出、T+0/T+1 标注 | 原样复用 |
| `report.py` | HTML+MD 报告（体检/因子表/信号/下一交易日指引/调仓建议/风险），浏览器渲染已核验 | 原样复用，产物入对象存储 |
| `runner.py` | 六项验证：pit_audit / golden_test(99项) / cross_source(90点) / calendar_check / adjust_check(5531点) / provenance_check | 同步 job 完成后自动触发（T1.6） |
| `repo.py` / `ctx.py` / `mcp_server.py` / `cli.py` / `data.py` / `seed.py` | 对象库/检查点/CLI/MCP | MCP+CLI 面保持（IDE 本地运行时不变），`ctx.py` 需 root 参数化（T0.3） |

## 1.2 实测锚点（回归基线，冻结数据快照下精确断言）

| # | 锚点 | 数值（数据截至 2026-09-30） |
| --- | --- | --- |
| A1 | multi5 复盘验证 | 六项全绿：golden 99 项对账 · cross_source 90 点 · adjust 5531 点·最大偏差 0.2339% · 停牌豁免 3 日 |
| A2 | multi5 回测（2021-01-04→2026-09-30，月度再平衡，rf=0，基准 510300 买入持有） | 组合：累计 +105.6% · CAGR 13.9% · 年化波动 11.0% · MDD -9.6% · 夏普 1.25 · 索提诺 1.16 · 卡玛 1.45 · 日胜率 56.9% · 1393 交易日 · 69 次再平衡；基准：累计 -16.9% · MDD -45.1%；超额 +122.5pp |
| A3 | cn4 回测（同区间同口径） | 见 `sandbox/artifacts/bt_*`（T7.2 生成快照断言） |
| A4 | multi5 执行单（RP-20260930-MULTI5，资金 ¥1,000,000） | 2 笔卖出：510300 卖 18,000 份(¥79,776)、518880 卖 9,300 份(¥80,287)；回款 ¥160,063；费用 ¥40.01；期末现金 15.8% |

> 回归方式：把 α 的 `sandbox/repo`（1393 交易日九标的快照）复制为 `core/tests/fixtures/ws_snapshot/`，锚点在**同一快照**上断言，避免数据滚动漂移。

## 1.3 已知缺陷与网络约束（α 实测结论 → β 处理决策）

| # | 事实 | β 阶段决策 |
| --- | --- | --- |
| C1 | 东财 push2his 系接口在当前出口**不可达** | 数据层仅允许：新浪 `stock_zh_a_daily` / `fund_etf_hist_sina` / `stock_financial_analysis_indicator` / `stock_history_dividend_detail` / `tool_trade_date_hist_sina` + 腾讯 `stock_zh_a_hist_tx` |
| C2 | `portfolio_rebalance_plan` 的 `cost_est_cny` 有 100× 错误（turnover 已是百分数又 ×100000） | T4.1 修复 + 单测锚定 |
| C3 | ETF/LOF 新浪无复权无分红接口 → 回测为**价格收益**，低估含息总收益 | 产品/报告全链路明示口径；P2 任务（东财分红接口云端可达性验证）不阻塞 |
| C4 | 份额折算（如 513100 2022-01-14 单日 -80.5%）| 保留 pct→None + provenance 登记机制 |
| C5 | `SIGNALS_FROM_COMPUTE` 模块级全局 dict；`Ctx` 路径硬编码 `PROJECT_ROOT` | T0.3 服务化改造（去全局态、root 注入），回归 α CLI 全绿 |
| C6 | Playwright `fullPage` 截图对长页有绘制伪影（非页面缺陷） | E2E 断言用 DOM/分屏截图，不用整页截图像素比对 |
| C7 | 新浪限频：连续快速调用会被临时封禁 | 同步 job 并发=1、请求间隔≥0.5s、失败退避 2s×3 次（`_retry` 已有） |

---

# 2. 目标与范围

## 2.1 功能范围（P0，本方案覆盖）

1. **组合/策略管理**：新建/编辑/删除组合（A股股票 + ETF/LOF 混合持仓、权重编辑与归一、标的检索与登记）；"策略"= 组合 + 调仓参数（当前为等权 + 引擎B规则，参数化在 §5.2 契约中预留）。
2. **数据层（保持 AKShare 在线数据）**：bootstrap 全量拉取、每日/按需增量拉取、六项验证自动门禁、数据质量看板。
3. **评估复盘**：任意组合一键复盘 → 六项验证 → HTML/MD 报告（引擎A因子表 + 引擎B信号 + 下一交易日指引）。
4. **回测**：可调参数（区间/再平衡频率/基准/无风险利率）→ 净值曲线、指标对比、月度/年度收益、标的贡献。
5. **调仓**：评估单（理由链+证据）→ 执行单（整手委托+费用，CSV 可下载）→ **人工确认** → 成交登记 → 全程审计。

## 2.2 非目标（本阶段明确不做）

- 券商交易直连、盘中 tick 实时行情（数据口径为**日频 EOD**，每次任务拉最新收盘）；
- 多租户/多用户社区（单用户 + 2FA 扩展位）；
- DSH 多 agent 研究所、因子注册中心全量页面（RC 阶段，主方案 §11）；
- 云端生产部署（本阶段交付 docker-compose 单机可运行 + 阿里云映射说明；生产化在 GA）。

## 2.3 可运行判据（Definition of Done）

```
git clone 后：
  cp .env.example .env && make up && make seed
  → 浏览器打开 http://localhost:8080
  → 演示账号登录 → 新建 multi5 复刻组合 → 触发数据同步（六项验证全绿）
  → 生成评估报告 → 跑回测（锚点区间断言通过）→ 生成执行单 → 人工确认+成交登记
  → make test-e2e 全绿
```

---

# 3. 总体架构（选型不变，继承主方案 §8/§14）

## 3.1 β 形态架构图

```
┌────────────────────────────────────────────────────────────────┐
│ 浏览器（微信/QQ/PC，响应式一套代码）                              │
│   web/  Vue3 + Vite + TS + Pinia + UnoCSS + ECharts(按需)        │
│         <768px 单列+底部tabbar ｜ ≥1024px 三栏工作台              │
└───────────────┬────────────────────────────────────────────────┘
                │ HTTPS /api/*（dev: vite proxy → bff）
┌───────────────▼────────────────────────────────────────────────┐
│ bff/  NestJS 10 · JWT 鉴权 · 限流(@nestjs/throttler)             │
│       · DTO 裁剪 · 统一错误映射 · 纯代理不触库                    │
└───────────────┬────────────────────────────────────────────────┘
                │ HTTP（内网，CORE_URL）
┌───────────────▼────────────────────────────────────────────────┐
│ core/  FastAPI + pydantic v2（Quant Core + DataHub 合体，β 形态）│
│   ├─ 组合/标的/任务/运行/调仓 REST（§6）                          │
│   ├─ JobRunner：ThreadPoolExecutor(2) + workspace 文件锁         │
│   └─ 计算内核 = alphapocket/applib（pip install -e 引用，不复制）│
│        datasource · factors · tools · backtest · exec_orders     │
│        report · runner(六项验证)                                  │
└──┬──────────────┬───────────────┬───────────────┬───────────────┘
   │              │               │               │
┌──▼─────┐  ┌─────▼─────┐  ┌──────▼──────┐  ┌────▼────────┐
│ PG 16  │  │ Redis 7   │  │ MinIO(S3)   │  │ DuckDB 文件  │
│ 实体/  │  │ job 缓存/ │  │ ap-artifacts│  │ 因子宽表     │
│ 审计   │  │ 限频计数  │  │ 桶(↔OSS)    │  │ (workspace) │
└────────┘  └───────────┘  └─────────────┘  └─────────────┘

dev 编排：deploy/docker-compose.dev.yml（pg/redis/minio/core/bff/web 六服务）
云端映射（GA，主方案 §14 不变）：compose→ECS 单机 · MinIO→OSS · PG→RDS
IDE 本地运行时（ap CLI + ap-mcp）与 Web 共享同一 applib 与数据契约，双运行时不变
```

## 3.2 端口与内部地址

| 服务 | 端口（dev） | 说明 |
| --- | --- | --- |
| web (nginx) | 8080 | 静态托管 Vue 构建产物；`/api` 反代 → bff |
| bff | 8081 | NestJS |
| core | 8082 | FastAPI（OpenAPI: `/docs`） |
| pg / redis / minio | 5432 / 6379 / 9000(+9001 控制台) | 仅 compose 内网，不对外 |

## 3.3 选型清单（与主方案映射，均为已定选型）

| 层 | 选型 | 主方案出处 |
| --- | --- | --- |
| 前端 | Vue3 + Vite + TS + Pinia + UnoCSS + ECharts 按需 | §10 |
| BFF | NestJS（JWT/限流/DTO） | §8 |
| 量化服务 | FastAPI + pydantic v2 + SQLAlchemy 2 + Alembic | §8 |
| 存储 | PostgreSQL 16 · Redis 7 · MinIO(S3 兼容，生产 OSS) · DuckDB | §8/§14 |
| 数据源 | AKShare（新浪主源 + 腾讯交叉源，接口白名单见 §1.3-C1） | §15.1 |
| 运行时 | Docker Compose（镜像 python:3.12-slim / node:20-alpine） | §14.2 |
| 计算内核 | alphapocket/applib（V4-α 资产） | §16 |

> 认证简化声明（不改选型，只定 β 节奏）：β 用单用户 + JWT(HS256, access 15m / refresh 7d) + argon2；RS256 / 微信 OAuth / PAT 随 RC 多实例化（主方案 §9）。

## 3.4 仓库布局

```
/workspace/alphapocket-web/          # 新仓库（β 交付物）
├── core/                            # FastAPI 服务
│   ├── app/  (api/ services/ models/ schemas/)
│   ├── tests/  (fixtures/ws_snapshot/ ← α 数据快照)
│   ├── pyproject.toml               # 依赖含 -e ../alphapocket（applib）
│   └── Dockerfile
├── bff/                             # NestJS
│   ├── src/ (auth/ proxy/ health/)
│   └── Dockerfile
├── web/                             # Vue3
│   ├── src/ (views/ components/ stores/ api/)
│   └── Dockerfile(nginx)
├── deploy/  (docker-compose.dev.yml · initdb/ · minio-init/)
├── e2e/    (Playwright)
├── docs/   (API.md · RUNBOOK.md)
├── Makefile · .env.example · README.md
└── (构建上下文含 ../alphapocket 源码挂载，见 T0.2)
```

---

# 4. 数据层设计（保持 AKShare 在线数据）

## 4.1 数据口径与"实时"定义

- **在线取数**：所有计算任务执行前确保 workspace 数据为"最新交易日"——评估/回测/调仓 job 先做增量检查（存在缺口自动增量补拉），保证"每次任务用最新收盘"。
- **日频 EOD 口径**：K 线为日线收盘；页面标注"日频数据 · 收盘口径"。盘中实时行情为 P2（需先验证非东财系实时接口可达性）。
- **双份留存**：股票 qfq 前复权 + raw 不复权（复权校验用）；ETF/LOF 仅 raw（新浪无复权接口，口径明示）。

## 4.2 同步模型（三种，全部落在 `POST /api/v1/data/sync` 的 mode 参数）

| mode | 触发 | 行为 | 预期时长 |
| --- | --- | --- | --- |
| `bootstrap` | 首次 / 新标的登记 | universe=PG securities 全集，自 2021-01-01 全量拉取（五年分位+250日因子窗口所需） | 5–15 分钟（9 标的实测；限频串行） |
| `incremental` | 每日定时（cron 建议 18:30）/ 手动 / 任务前自动缺口检查 | 各标的自 workspace 最后日期次日起增量拉取 | <1 分钟 |
| `refresh` | 用户对单标的点"刷新" | 单标的强制重拉最近 N(=5) 日并覆盖 | 秒级 |

- 同步 job 内部流程：`datasource.fetch_all(ctx, universe, mode)` → 落 workspace（repo JSON + DuckDB）→ 自动执行六项验证 → 结果写 PG `verify_results`。
- **限频纪律**（C7）：进程内全局同步互斥（同一时间仅 1 个 sync job），请求间隔 ≥0.5s，`_retry(3, 2s)` 已内建；Redis 记录上游连续失败计数，≥3 次熔断 10 分钟并返回 503。

## 4.3 存储分治

| 存储 | 内容 | 写入方 |
| --- | --- | --- |
| PostgreSQL | 实体（用户/标的/组合/持仓/job/run/验证结果/调仓单/执行单/审计）——**唯一权威账本** | core |
| DuckDB（workspace 内） | 因子宽表 `factor_value_daily` / `factor_pit_meta`（applib 既有） | applib 工具 |
| workspace repo JSON | `daily_bars/raw/cross_check_tx/trade_calendar/fundamental_values/provenance/dim_security/portfolio/*.json`（applib 既有契约，**冻结**） | datasource |
| MinIO `ap-artifacts` 桶 | run 产物（report.html/md、backtest.*、exec_order.* …），key = workspace artifacts 相对路径 | core |
| Redis | job 进度缓存、限频/熔断计数、（P2）实时报价缓存 | core/bff |

## 4.4 验证门禁（继承主方案 §15.2）

- 六项检查（pit_audit / golden_test / cross_source / calendar_check / adjust_check / provenance_check）在每次 sync job 末尾自动执行，逐项结果入库。
- **门禁规则**：最近一次 sync 的六项验证非全绿时，`review / backtest / rebalance-plan` 返回 `409 VERIFY_GATE_FAILED`（附失败明细），前端红条提示。豁免类（停牌/份额折算）为 pass 且透明计数，不阻断。
- 与 α 的语义完全一致：全绿才可用，失败可看明细。

---

# 5. 领域模型与数据契约

## 5.1 PG Schema（DDL 冻结；Alembic 管理，core 唯一写方）

```sql
CREATE TABLE users (
  id            BIGSERIAL PRIMARY KEY,
  username      TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,                 -- argon2id
  totp_secret   TEXT,                          -- P1 可空
  role          TEXT NOT NULL DEFAULT 'owner',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE securities (
  secid       TEXT PRIMARY KEY,                -- '510300.SH' / '002014.SZ'
  code        TEXT NOT NULL,
  name        TEXT NOT NULL,
  market      TEXT NOT NULL,                   -- 'A'
  asset_type  TEXT NOT NULL,                   -- 'STOCK' | 'ETF' | 'LOF'
  industry    TEXT,
  sina_symbol TEXT NOT NULL,                   -- 'sh510300'
  ftype       TEXT,                            -- etf/lof（基金类）
  settle      TEXT,                            -- 'T+1' / 'T+0'
  cost        TEXT,                            -- 费用说明
  source      TEXT NOT NULL DEFAULT 'user',    -- user|seed
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE portfolios (
  id         BIGSERIAL PRIMARY KEY,
  pid        TEXT UNIQUE NOT NULL,             -- 短码：'multi5' / 自动生成
  name       TEXT NOT NULL,
  note       TEXT,
  base_ccy   TEXT NOT NULL DEFAULT 'CNY',
  strategy   JSONB   NOT NULL DEFAULT '{}',    -- §5.2 策略参数（预留）
  status     TEXT NOT NULL DEFAULT 'active',   -- active|archived
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE positions (
  id           BIGSERIAL PRIMARY KEY,
  portfolio_id BIGINT NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
  secid        TEXT NOT NULL REFERENCES securities(secid),
  weight       NUMERIC(6,3) NOT NULL CHECK (weight > 0),   -- 百分数：20.0
  UNIQUE (portfolio_id, secid)
);

CREATE TABLE jobs (
  id          TEXT PRIMARY KEY,                -- 'job_<ts>_<rand>'
  type        TEXT NOT NULL,                   -- data_sync|review|backtest|rebalance_plan|exec_orders
  portfolio_id BIGINT REFERENCES portfolios(id),
  status      TEXT NOT NULL DEFAULT 'queued',  -- queued|running|done|failed
  params      JSONB NOT NULL DEFAULT '{}',
  result      JSONB,
  error       TEXT,
  progress    INT NOT NULL DEFAULT 0,          -- 0-100
  started_at  TIMESTAMPTZ, finished_at TIMESTAMPTZ,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE runs (
  id            BIGSERIAL PRIMARY KEY,
  run_uid       TEXT UNIQUE NOT NULL,          -- 沿用 applib new_run_id 形态
  job_id        TEXT NOT NULL REFERENCES jobs(id),
  portfolio_id  BIGINT REFERENCES portfolios(id),
  type          TEXT NOT NULL,                 -- review|backtest|rebalance_plan|exec_orders|data_sync
  as_of         DATE,
  summary       JSONB,                         -- 指标摘要（列表页直接读）
  artifact_keys TEXT[] NOT NULL DEFAULT '{}',  -- MinIO keys
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE verify_results (
  id        BIGSERIAL PRIMARY KEY,
  job_id    TEXT NOT NULL REFERENCES jobs(id),
  check     TEXT NOT NULL,                     -- pit_audit|golden_test|...
  status    TEXT NOT NULL,                     -- pass|fail
  detail    JSONB NOT NULL,                    -- 计数/明细/豁免说明
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE rebalance_plans (
  id           BIGSERIAL PRIMARY KEY,
  plan_uid     TEXT UNIQUE NOT NULL,           -- 'RP-20260930-MULTI5'
  portfolio_id BIGINT NOT NULL REFERENCES portfolios(id),
  run_id       BIGINT REFERENCES runs(id),
  status       TEXT NOT NULL DEFAULT 'draft',  -- draft|superseded|executed|cancelled
  payload      JSONB NOT NULL,                 -- §5.2 plan 契约原样
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE exec_orders (
  id           BIGSERIAL PRIMARY KEY,
  exec_uid     TEXT UNIQUE NOT NULL,           -- 'EX-20260930-MULTI5'
  plan_id      BIGINT NOT NULL REFERENCES rebalance_plans(id),
  portfolio_id BIGINT NOT NULL REFERENCES portfolios(id),
  capital_cny  NUMERIC(14,2) NOT NULL,
  status       TEXT NOT NULL DEFAULT 'draft',  -- draft|confirmed|executed|cancelled
  orders       JSONB NOT NULL,                 -- §5.2 exec 契约原样
  confirmed_at TIMESTAMPTZ, confirmed_by TEXT,
  filled_at    TIMESTAMPTZ, fills JSONB,       -- [{secid,price,shares}]
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE audit_log (
  id         BIGSERIAL PRIMARY KEY,
  entity     TEXT NOT NULL,                    -- portfolio|exec_order|security|...
  entity_id  TEXT NOT NULL,
  action     TEXT NOT NULL,                    -- create|update|delete|confirm|fill|sync
  actor      TEXT NOT NULL,
  payload    JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit_entity ON audit_log(entity, entity_id);
```

## 5.2 组合物化 JSON（α 兼容契约，applib 工具零改动）

组合保存时由 core 物化到 `workspace/repo/portfolio/{pid}.json`（applib `portfolio.review` 等工具直接读它）：

```json
{
  "id": "multi5", "name": "沪深300/南方原油/黄金/纳指/十年国债", "base_ccy": "CNY",
  "note": "等权多资产",
  "positions": [
    {"secid": "510300.SH", "name": "沪深300ETF", "weight": 20.0,
     "asset_type": "ETF", "settle": "T+1", "cost": "ETF 免印花税·佣金约万2.5"}
  ]
}
```

- `weight` 为百分数；`asset_type` ∈ STOCK/ETF/LOF；股票缺省 `settle:"A股 T+1"`、`cost:"印花税卖出0.05%+佣金"`。
- `strategy` 参数预留（当前默认引擎B规则：MA20±2% 确认带、跌破减仓 40%、冷却 10 日）：本阶段仅存储不参与计算，RC 开放参数。

产物契约（applib 既有输出，冻结）：`review.json` / `rebalance_plan.json`（含 `diff_trades[].{side,secid,name,from,to,reason,evidence,settle,cost}`）/ `exec_order.{md,csv,json}`（含 orders/cash/fees/checklist）/ `report.{html,md}` / `backtest.{html,md,json}`。

## 5.3 对象存储布局

```
ap-artifacts/
  runs/{run_uid}/report.html|report.md|review.json|rebalance_plan.json|backtest.*|exec_order.*
  # key 即 workspace artifacts 相对路径；core 代理流式下载（β 不做公开预签名）
```

---

# 6. API 契约（core `/api/v1`，OpenAPI 自动生成，BFF 原样透传加 `/api` 前缀）

## 6.1 异步 Job 模式（统一约定）

- 长任务（sync/review/backtest/rebalance_plan）`POST` → `202 {"job_id", "poll": "/api/v1/jobs/{id}"}`；
- `GET /api/v1/jobs/{id}` → `{"id","type","status","progress","result","error"}`；终态后 `result` 内含 `run_uid` 与产物 key 列表；
- 轮询间隔 ≥1s（β 不做 SSE；SSE 为 P1：`GET /api/v1/jobs/{id}/events`）；
- 同一 workspace 同时最多 1 个计算 job（互斥），sync 全局唯一；冲突返回 `409 JOB_LOCKED`（前端转排队提示）。

## 6.2 端点清单

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | /healthz | 存活检查（含 pg/minio 探测） |
| POST | /auth/login | 用户名+密码 → `{access,refresh}`（core 侧自签发，bff 校验） |
| GET | /securities/search?q= | 标的检索（代码/名称前缀，本地 PG；空 q 返回最近 20） |
| POST | /securities/register | `{code}` → 判定 stock/etf/lof（AKShare 试拉校验）→ 入表；已存在幂等返回 |
| POST | /data/sync | `{mode: bootstrap\|incremental\|refresh, secid?}` → 202 job；结束时自动跑六项验证 |
| GET | /data/status | 各标的最后 bar 日期、缺口、最近验证状态汇总、universe 计数 |
| GET | /data/verifications?limit=20 | 验证结果列表（含 detail） |
| GET | /portfolios | 组合列表（含最近 run 摘要与持仓数） |
| POST | /portfolios | `{name,pid?,note,positions:[{secid,weight}],equal_weight?}`；校验：标的存在、权重>0、和∈[99.5,100.5]（越界 422 附归一建议） |
| GET/PUT/DELETE | /portfolios/{id} | 详情/更新/归档（DELETE=archived 软删） |
| POST | /portfolios/{id}/review | 202 job；前置：数据门禁全绿（否则 409）；job 内自动增量缺口检查 |
| POST | /portfolios/{id}/backtest | `{start?,end?,rf?,rebalance?,benchmark_secid?}` → 202 job |
| POST | /portfolios/{id}/rebalance-plan | 202 job；产出 plan（status=draft） |
| GET | /plans/{id} | plan 详情；`GET /portfolios/{id}/plans` 列表 |
| POST | /plans/{id}/exec-orders | `{capital_cny}` → 同步返回执行单（毫秒级换算，非 job） |
| POST | /exec-orders/{id}/confirm | `{ack_discipline:true}` → confirmed（否则 422） |
| POST | /exec-orders/{id}/fill | `{fills:[{secid,price,shares}]}` → executed |
| GET | /exec-orders/{id} | 详情含状态时间线与审计 |
| GET | /runs?type=&portfolio_id=&limit= | 运行历史 |
| GET | /runs/{run_uid}/artifacts/{file} | 产物流（content-type 按扩展名；html 供 iframe 嵌入） |
| GET | /jobs/{id} | job 状态（§6.1） |

## 6.3 错误码与门禁语义

| code | 场景 | 前端行为 |
| --- | --- | --- |
| 409 `VERIFY_GATE_FAILED` | 数据验证未全绿即请求计算 | 红条 + 跳数据页看明细 |
| 409 `JOB_LOCKED` | workspace 计算互斥冲突 | 排队提示 + 自动轮询 |
| 409 `DATA_STALE`（内部化处理） | 任务前缺口检查需补拉 | 自动转为 sync job 后重试原任务（核心内部处理，前端只见串联 job） |
| 422 `WEIGHT_NOT_NORMALIZED` / `SECURITY_NOT_FOUND` / `ACK_REQUIRED` | 输入校验 | 表单内联错误 |
| 503 `UPSTREAM_RATE_LIMITED` | AKShare 熔断 | 提示稍后重试 |

---

# 7. 调仓工作流（状态机与不变量）

```
review(评估) ──► rebalance_plan(draft) ──► exec_orders(draft) ──► [人工确认 confirmed]
                     │  superseded(新plan取代)                     │
                     │  cancelled                                 ▼
                     └────────────────────────────► (登记成交 fills) executed
                                                          │
                                            audit_log 全程记录（谁/何时/改了什么）
```

**不变量**（前端后端双重强制）：
1. 引擎永不直接动仓位：无任何券商接口；确认/登记只是状态+审计写入。
2. `confirm` 必须显式 `ack_discipline:true`（前端呈现纪律勾选框 + 二次确认弹窗）。
3. `fill` 允许部分成交：未登记满的差额保持 `confirmed` 子状态标注（orders.fills 记录实际，不做复杂部分状态机）。
4. 新 plan 生成时，同组合旧 `draft` plan 自动置 `superseded`。
5. 执行单上强制展示：QDII 溢价核对提示、节假日跳空提示、T+0/T+1 结算列、费用明细、限价参考带。

---

# 8. 前端页面规格

路由（`web/src/router`）：

| 路由 | 内容 | 断点差异 |
| --- | --- | --- |
| `/login` | 登录（用户名/密码） | 同 |
| `/portfolios` | 组合卡片列表：名称/标的数/最近评估摘要(当日·YTD·1年·MDD)/最近 run 时间 + 「新建组合」 | PC 三栏中栏；H5 tabbar「组合」 |
| `/portfolios/new` | 三步向导：①基本信息 → ②标的搜索+权重编辑（等权一键/手输/归一提示；股票·ETF·LOF 类型徽章；新标的即时 register） → ③确认（可选勾选"立即同步数据"） | 同 |
| `/portfolios/:id` | Tab「概览」：体检卡（当日/20日/YTD/1年/1年MDD）、引擎A 因子表、引擎B 信号卡、下一交易日指引、数据截至徽章 | PC 表格全宽；H5 卡片纵向 |
| `/portfolios/:id/report` | 报告页：HTML 报告 iframe 嵌入 + MD/原文链接 | 同 |
| `/portfolios/:id/backtest` | 参数表单（区间/再平衡频率/基准/rf/运行）+ 结果：净值双线 ECharts、指标对比卡、月度收益表、标的贡献表 + 历史回测列表 | H5 图表降采样 |
| `/portfolios/:id/rebalance` | 调仓页：最新 plan（理由链/证据徽章 E2）→ 生成执行单（资金输入）→ 委托表（CSV 下载）→ 确认流（纪律勾选+二次弹窗）→ 成交登记表单 → 状态时间线 | 同 |
| `/data` | 数据看板：universe 表（最后日期/缺口/豁免）、六项验证徽章墙、手动 sync（bootstrap/incremental）、验证详情抽屉 | 同 |
| `/runs` | 运行历史（type/status 筛选，产物直达） | PC 表格；H5 列表 |

- 视觉基线：沿用 v3.1「纸质档案 + 印章红」体系（主方案 §2.1）；基础组件库 `web/src/components/ui/`（Badge/Card/Table/Button/Toast/Tabs/Modal/Stepper）。
- 所有数字展示统一 `fmt_pct/fmt_cny` 工具；免责声明为全局页脚固定项（附录 B 合规继承）。

---

# 9. 任务分解（WBS · Flash 执行单元）

## 9.0 依赖图与执行波次

```
T0.1 ─ T0.2 ─ T0.3 ┐
                    ├─► T1.1 ─► T1.2 ─┬─► T1.3 ─► T1.4 ─► T1.5 ─► T1.6
                    │                ├─► T2.1 ─► T2.2 ─► T2.3 ─► T2.4
T5.1（仅依赖契约，可并行起步）          │           └─► T4.1 ─► T4.2 ─► T4.3 ─► T4.4
T6.1（仅依赖契约，可并行起步）          └─► T3.1 ─► T3.2
T5.1 ─► T5.2 ─► T5.3
T6.1 ─► T6.2 ─► T6.3 ─► T6.4 ─► T6.5 ─► T6.6 ─► T6.7
全部 ─► T7.1 / T7.2 / T7.3
```

**建议波次**（每波内任务可并行分派）：

| 波次 | 任务 | 说明 |
| --- | --- | --- |
| W1 | T0.1 T0.2 T0.3 | 地基（契约冻结下无歧义） |
| W2 | T1.1 T5.1 T6.1 | 三个服务骨架并行 |
| W3 | T1.2 T2.1 T5.2 T6.2 | 运行器/库表/认证/登录 |
| W4 | T1.3 T2.2 T5.3 T6.3 | 标的与组合域 + BFF 透传 |
| W5 | T1.4 T1.5 T1.6 T2.3 T3.1 | 数据同步主链 + 评估 + 回测参数化 |
| W6 | T2.4 T3.2 T4.1 T6.4 | 产物服务 + 回测 API + 调仓域启动 |
| W7 | T4.2 T4.3 T4.4 T6.5 T6.6 | 调仓全链 + 前端两大页 |
| W8 | T6.7 T7.1 T7.2 T7.3 | 数据页 + 集成测试 + 交付固化 |

通用验收环境：`make up` 后各服务健康；每任务交付含**自测命令输出**贴回任务单。

---

## 9.1 T0 · 脚手架与环境

### T0.1 monorepo 脚手架
- 目标：建立 §3.4 仓库布局与工程规范。
- 产出：目录骨架；`core/pyproject.toml`（fastapi/uvicorn/pydantic-settings/sqlalchemy/alembic/psycopg[binary]/redis/minio/httpx/pytest/ruff；`alphapocket @ file:../alphapocket` 可编辑安装）；`bff`（NestJS10+ts+eslint+jest）；`web`（Vite5+Vue3.4+TS+Pinia+UnoCSS+echarts 按需+vitest）；`Makefile`（up/down/logs/psql/migrate/seed/test/lint/test-e2e）；`.env.example`（§3.2 端口/PG/REDIS/MINIO/JWT）；根 README 快速启动。
- 依赖：无。
- 验收：`make lint && make test`（骨架空测试全绿）；`pnpm -C bff build && pnpm -C web build` 成功。
- α 坑：无。

### T0.2 docker-compose 与基础设施
- 目标：一键起全栈（§2.3 DoD 的底座）。
- 产出：`deploy/docker-compose.dev.yml`（pg16/redis7/minio/core/bff/web 六服务+healthcheck）；`deploy/initdb/00-extensions.sql`；minio 自动建桶 `ap-artifacts`（minio/mc init 容器）；core/bff/web 三 Dockerfile（构建上下文=仓库根，core 镜像内 COPY `alphapocket/applib` 并以包安装；dev 模式 volume 热载源码）；nginx 配置（静态+/api 反代 bff）。
- 依赖：T0.1。
- 验收：`make up && curl -sf localhost:8082/healthz && curl -sf localhost:8081/healthz && curl -sf -o /dev/null localhost:8080`；`docker compose -f deploy/docker-compose.dev.yml ps` 全 healthy。
- α 坑：无（注意 minio 桶初始化竞态：init 容器需 `depends_on: minio: condition: service_healthy`）。

### T0.3 applib 服务化改造（唯一允许改 `alphapocket/` 的任务）
- 目标：applib 可被多进程服务安全复用，CLI 行为零变化。
- 改动点：① `ctx.Ctx` 增加 `root: Path = None` 注入（None 时维持现 `PROJECT_ROOT`，完全向后兼容）；② `tools.SIGNALS_FROM_COMPUTE` 全局态移除——`factor_compute` 已返回 signals，runner 若读全局改为从工具返回值传递；③ `datasource.fetch_all(ctx, start=..., universe=None, incremental_from=None)` 参数化（universe=None 时默认 `CN4+MULTI5`，保持现行为；incremental_from 传 `{secid: last_dt}` 字典时从次日起拉）；④ workspace 目录约定文档化（`var/ws/<name>/{repo,data,artifacts,logs}`，复用 Ctx 既有子路径逻辑）。
- 依赖：T0.1（不需 T0.2）。
- 验收（α 回归，全绿即通过）：
  ```
  cd /workspace/alphapocket
  python3 ap fetch && python3 ap clone && python3 ap run tasks/review_cn4.yaml
  python3 ap run tasks/review_multi5.yaml && python3 ap backtest --portfolio multi5
  python3 ap exec-plan --portfolio multi5 --capital 1000000
  # 六项验证 pass、回测锚点 A2、执行单锚点 A4 数值不变
  ```
- α 坑：C5；改造时**严禁**顺手重构其他逻辑——回归锚点必须逐位一致。

---

## 9.2 T1 · 数据层

### T1.1 core 服务骨架
- 产出：`core/app/main.py`（FastAPI+lifespan：启动连 pg/redis/minio 探测）；`/healthz`；pydantic-settings 配置（env 前缀 `AP_`）；统一异常处理器（§6.3 错误体 `{code,message,detail}`）；结构化日志（job_id 贯穿）；pytest fixture（临时 workspace + mock applib 调用）。
- 依赖：T0.1/T0.3。
- 验收：`pytest core/tests/test_health.py -q` 绿；`curl localhost:8082/healthz` 返回各依赖状态。

### T1.2 workspace 管理与 Job 运行器
- 产出：`WorkspaceManager`（`var/ws/default/` 初始化=α `ctx.ensure_sandbox()` 逻辑；进程级 RLock：计算互斥、sync 全局互斥）；`JobRunner`（ThreadPoolExecutor(max_workers=2)；job 状态机写 PG；progress 上报回调；异常捕获入 `jobs.error`）；`GET /api/v1/jobs/{id}`。
- 依赖：T1.1、T2.1（jobs 表先行迁移）。
- 验收：`pytest core/tests/test_jobs.py`——并发提交 3 个 sleep 任务，断言互斥/排队/终态与 PG 记录一致；kill -9 场景启动自愈（running 超时>30min 置 failed）。
- α 坑：DuckDB 单文件单写——所有 applib 调用必须持 workspace 锁串行。

### T1.3 标的注册与检索
- 产出：`GET /securities/search?q=`（PG 前缀匹配 code/name/secid）；`POST /securities/register`：入参 `{code}` → 判定市场前缀（6 位 A 股：60/68→sh、00/30→sz；5→sh 基金、1→sz 基金）→ AKShare 试拉 10 根 K 线验证可达（股票 `stock_zh_a_daily`，基金 `fund_etf_hist_sina`）→ 判定 asset_type（ETF/LOF 用 ftype 入参或默认 ETF；注册接口允许显式 `asset_type`）→ 入 `securities` 表 + 审计。
- 依赖：T2.1、T1.2。
- 验收：`pytest core/tests/test_securities.py`——注册 `600519`（股票）、`510300`（已存在幂等）、`159915`（sz 基金）、无效代码（422）；search 中文前缀命中。
- α 坑：C1 白名单——注册探测只能用新浪/腾讯接口；LOF 与 ETF 均走 `fund_etf_hist_sina`（α 已验证 sh501018 可用）。

### T1.4 数据同步 job（核心数据链）
- 产出：`POST /data/sync`（§4.2 三 mode）→ job：universe=PG securities 全集（替代 α 的硬编码 CN4+MULTI5）→ 调 T0.3 参数化的 `fetch_all`（bootstrap/incremental/refresh）→ 落 workspace → 物化 `dim/security.json` 与 `portfolio/*.json` 刷新（从 PG 读当前组合全集）→ 触发六项验证（T1.6 交付后串联，先留钩子）→ `runs` 记录 data_sync 类型。同步前置检查：新标的缺失 → 自动并拉。限频熔断（§4.2）。
- 依赖：T1.2、T1.3。
- 验收：`pytest core/tests/test_sync.py`（mock datasource：增量起点正确、universe 并集、豁免计数透传）；真实冒烟 `curl -XPOST .../data/sync -d '{"mode":"incremental"}'` 在现有 9 标的上 <60s 完成；bootstrap 在干净 workspace 与 α 快照对账（行数、provenance 齐全）。
- α 坑：份额折算日 pct=None 必须保留原逻辑（C4）；停牌豁免分类依赖腾讯源同日有无（runner 既有）。

### T1.5 数据状态 API
- 产出：`GET /data/status`（每标的：最后 bar 日期/距今天数/缺失豁免计数/asset_type；universe 汇总；最近 sync job 状态；六项验证最新徽章）；`GET /data/verifications?limit=`（含 detail：golden 项数、cross 点数、adjust 最大偏差、豁免明细）。
- 依赖：T1.4。
- 验收：`pytest core/tests/test_data_status.py`（fixture workspace 快照断言字段）；`curl` 输出与 α 报告 ① 节一致（99/90/5531/豁免3）。
- α 坑：豁免≠缺失——状态页两类分开展示（α 报告已区分，保持）。

### T1.6 验证门禁
- 产出：sync job 末尾执行六项验证（复用 `runner.py` 校验函数，输出结构化逐项 `{check,status,detail}` 入 `verify_results`）；门禁中间件/服务层：review/backtest/rebalance_plan 前置校验最近 sync 全绿，否则 `409 VERIFY_GATE_FAILED` + 失败明细。
- 依赖：T1.4、T2.3（联调）。
- 验收：`pytest core/tests/test_gate.py`——人为注入 golden fail fixture → 三个计算端点均 409 且错误体含失败项；全绿放行。
- α 坑：六项的 pass 语义=α 实测语义（豁免计数属 pass）。

---

## 9.3 T2 · 组合与评估

### T2.1 PG Schema 与迁移
- 产出：Alembic init + §5.1 全量 DDL 首版迁移；`make seed`（演示用户 `demo/alphapocket`、CN4+MULTI5 九标的入 `securities`、两组合及持仓入表）。
- 依赖：T1.1。
- 验收：`make migrate && make seed && psql -c '\dt'` 十表齐；seed 数据与 α `remote/repo` 元数据一致（行业/ftype/settle/cost 字段对齐）。

### T2.2 组合 CRUD 与物化
- 产出：§6.2 组合端点全套；校验（权重和窗口、标的已注册、去重）；保存→物化 §5.2 portfolio JSON 到 workspace（含从 securities 表补 name/asset_type/settle/cost）；pid 冲突/自动生成（拼音或 `pf_<n>`）；变更审计；DELETE=归档。
- 依赖：T2.1、T1.2。
- 验收：`pytest core/tests/test_portfolios.py`——创建 multi5 复刻（等权 5×20）物化 JSON 与 α `remote/repo/portfolio/multi5.json` 字段级一致；权重 99/101 边界 422；未注册标的 422。
- α 坑：weight 是百分数（20.0 非 0.2）——契约沿 α。

### T2.3 评估 run
- 产出：`POST /portfolios/{id}/review` → job：门禁检查 → 缺口自动增量（必要时串 sync）→ `portfolio_review` + `report.render_all` → 产物上传 MinIO → `runs` 记录（summary=体检指标+信号计数）。
- 依赖：T2.2、T1.6。
- 验收：真实冒烟——seed 的 multi5 评估 run 产物 report.html 与 α `run_20261005_055947_e54fc7/report.md` 内容口径一致（as_of 同快照下）；`GET /runs` 列表正确。
- α 坑：review 需要读 `factors/fundamental_values.json`——股票组合无该文件时报错路径要在 sync 物化时保证存在（α fetch_all 已产出，勿破坏）。

### T2.4 产物服务
- 产出：`GET /runs/{run_uid}/artifacts/{file}`——MinIO 流式代理 + 扩展名 content-type（html/md/json/csv）+ 不存在 404；iframe 嵌入所需响应头（`X-Frame-Options: SAMEORIGIN`）。
- 依赖：T2.3。
- 验收：`pytest core/tests/test_artifacts.py`；浏览器直开 HTML 报告渲染正常（C6：用分屏 DOM 断言，勿整页截图）。

---

## 9.4 T3 · 回测

### T3.1 回测引擎参数化（applib/backtest.py 改造）
- 产出：`run_backtest(bars, portfolio, start, end, rf, rebalance='monthly', benchmark_secid='510300.SH', cost_bps=2.5)`；`rebalance ∈ {daily,monthly,quarterly,yearly,buyhold}`（月度=每月末交易日；季/年=每 3/12 月末；buyhold=期初等权后不再平衡；daily=α 复盘口径）；基准可为 universe 内任意已同步 secid（非组合内亦可）；成本参数化（默认万2.5 与 α 一致）。**默认参数输出与 α 锚点 A2 逐位一致**。
- 依赖：T0.3。
- 验收：`pytest core/tests/test_backtest_params.py`——A2 锚点回归（月度默认）+ 5 频率 × 快照烟雾（buyhold=n_rebalances 0；daily 与复盘 NAV 口径抽样一致性）+ 自定义基准（如 511260）。
- α 坑：A2 的 69 次再平衡含区间首月末；改频率逻辑时勿动成本计提位置（月末收盘先漂移再计成本，α 实现已验证）。

### T3.2 回测 API
- 产出：`POST /portfolios/{id}/backtest`（§6.2 参数校验：start≤end、benchmark 需在 bars、rebalance 枚举）→ job → 调 T3.1 → 产物三件套入 MinIO → summary=核心指标（含 strategy/benchmark/excess）。
- 依赖：T3.1、T2.3（job/门禁复用）。
- 验收：真实冒烟 multi5 默认参数 → summary 与 A2 一致；`start>end` 422；未知基准 422。

---

## 9.5 T4 · 调仓

### T4.1 调仓评估单 API + cost bug 修复
- 产出：`POST /portfolios/{id}/rebalance-plan` → job（review+plan）→ `rebalance_plans(draft)` + 产物落 MinIO + 旧 draft 置 superseded；**修复 C2**：`tools.portfolio_rebalance_plan` 的 `cost_est_cny` 改为按 `turnover_pct` 正确换算（修复后 A4 场景 plan 的 cost_est ≈ ¥40，与执行单实算一致）。
- 依赖：T2.3。
- 验收：`pytest core/tests/test_plan.py`——bug 修复单测（构造已知 turnover 断言费用）；真实冒烟 multi5 plan 与 α `RP-20260930-MULTI5` 的 diff_trades 一致（同快照）。
- α 坑：修复必须保留 `turnover_est` 文案格式（前端与报告模板依赖）。

### T4.2 执行单 API
- 产出：`POST /plans/{id}/exec-orders` `{capital_cny}`（默认 1,000,000）→ 同步调用 `exec_orders.build_exec_orders(review_now, plan_payload, capital)` → `exec_orders(draft)` + md/csv/json 入 MinIO；`GET /exec-orders/{id}`。
- 依赖：T4.1、T2.4。
- 验收：真实冒烟——RP（A4 场景）×1,000,000 → 2 卖单、18,000/9,300 份、费用 ¥40.01（快照一致）；capital=100,000 时整手取整与费用正确；`GET` 状态时间线含 draft。
- α 坑：用**最新 review 数据 + 存量 plan** 组合（plan 的 to 权重 + 当前 close）；plan 过老（as_of≠最新交易日）时 409 提示先重跑评估。

### T4.3 确认与成交登记（状态机）
- 产出：`POST /exec-orders/{id}/confirm`（强制 `ack_discipline:true`，否则 422 `ACK_REQUIRED`；写审计）；`POST /exec-orders/{id}/fill`（fills 校验：secid∈单内、价格>0、数量为 100 整数倍且方向一致；登记实际成交，状态→executed；部分成交允许并标注）；时间线接口。
- 依赖：T4.2。
- 验收：`pytest core/tests/test_exec_flow.py`——完整状态机（draft→confirmed→executed）、ACK 缺失 422、非法 fills 422、审计三条齐全；取消路径 cancelled。
- α 坑：无自动下单路径存在——代码评审确认无任何外发交易请求。

### T4.4 调仓历史与审计
- 产出：`GET /portfolios/{id}/rebalance-history`（plans+orders+fills 时间线）；审计列表查询 `GET /audit?entity=&entity_id=`。
- 依赖：T4.3。
- 验收：`pytest core/tests/test_history.py`——多轮 plan/order 场景的时间线顺序与状态正确。

---

## 9.6 T5 · BFF（NestJS）

### T5.1 骨架与代理
- 产出：NestJS 模块（ConfigModule+`CORE_URL`；HttpService 超时 120s/重试 0；全局异常过滤器映射 core 错误体；请求日志带 traceId；`@nestjs/throttler` 全局 100 req/min·IP）；`/healthz`；`/api/*` → core 透传（路径原样、query/body/响应流原样；`GET /runs/*/artifacts/*` 流式）。
- 依赖：契约（可与 T1.1 并行，用 §6 mock）。
- 验收：`pnpm -C bff test`（e2e spec：mock core 透传/错误映射/限流 429）；`curl -sf localhost:8081/api/v1/...` 通。

### T5.2 认证
- 产出：`POST /api/auth/login`（core 校验 argon2 → 签发 JWT HS256 access 15m/refresh 7d）；AuthGuard（除 /healthz /auth/login 外全拦截）；`POST /api/auth/refresh`；`GET /api/auth/me`；CORS（dev `http://localhost:5173`）；演示账号 seed。
- 依赖：T5.1、T2.1（users 表）。
- 验收：`pnpm -C bff test`（登录/过期刷新/无 token 401/错误密码 401 计数锁定 5 次锁 15min——Redis 计数）。
- α 坑：无（RS256/微信 OAuth/PAT 为 RC，勿提前实现）。

### T5.3 路由与 DTO 裁剪
- 产出：BFF 侧 DTO 校验层（组合创建/回测参数等核心入参 zod/ class-validator 白名单，防 core schema 漂移）；组合列表/数据状态响应裁剪（剔除内部字段）；`Cache-Control` 策略（run 详情 immutable）；SSE 端点占位（501）。
- 依赖：T5.2。
- 验收：`pnpm -C bff test`——非法字段被拒（多余字段剥离/422）；契约快照测试（与 core OpenAPI 对照自动生成）。

---

## 9.7 T6 · 前端（Vue3）

### T6.1 脚手架与布局
- 产出：Vite+Vue3+TS+Pinia+router+UnoCSS+ECharts 按需；`useBreakpoint()`（<768/768-1024/≥1024）；布局骨架（H5：顶部品牌+底部 tabbar 组合/数据/运行/我的占位；PC：三栏 导航树+主内容+上下文栏）；`components/ui/` 基础件（§8 清单）；api client（axios 封装+类型从 core OpenAPI 生成）；vite proxy→bff。
- 依赖：契约（可与 T5 并行）。
- 验收：`pnpm -C web build && pnpm -C web test`（布局两断点快照）；本地 dev 起服务路由可达。

### T6.2 登录与 auth store
- 产出：`/login` 页；token 存储（refresh token httpOnly 不可行则 localStorage+XSS 说明）、401 拦截→refresh→重放、路由守卫、登出。
- 依赖：T6.1、T5.2。
- 验收：`vitest`（守卫单测）+ Playwright 冒烟（错误密码提示/登录成功跳组合列表/过期自动刷新）。

### T6.3 组合列表与新建向导
- 产出：`/portfolios`（卡片：名称/pid/标的徽章行/最近评估摘要四指标/最近 run 时间/状态）；`/portfolios/new` 三步向导（§8）：标的搜索下拉（防抖调 search）、新标的即时 register（含资产类型选择）、权重编辑器（等权一键/手输/实时求和与归一建议）、第三步确认+可选立即同步（跳数据页跟踪 job）。
- 依赖：T6.2、T2.2、T1.3。
- 验收：Playwright——新建 multi5 复刻组合全程（含新标的 register）成功；权重 99.5 以下时出现归一提示且阻断提交；422 内联错误展示。

### T6.4 组合详情·概览与报告
- 产出：Tab 概览（体检卡五指标、引擎A 因子表——基金列自动 `—`（α 口径）、引擎B 信号卡（state 徽章 above/below/neutral+冷却提示）、下一交易日指引列表、数据截至徽章+门禁红条）；Tab 报告（iframe 嵌 HTML 产物 + 下载链接 md/json）。
- 依赖：T6.3、T2.3/T2.4。
- 验收：Playwright——multi5 评估后详情页指标与 A1/A2 报告口径一致；验证门禁 409 时红条+跳数据页；iframe 报告可滚动渲染（分屏断言）。

### T6.5 回测页
- 产出：参数表单（区间 datepicker 默认全样本、再平衡频率五选一、基准下拉（universe）、rf 数字输入）→ 提交 job 轮询；结果区（ECharts 净值双线+图例、八指标对比卡（组合 vs 基准 vs 超额）、月度收益表（复用 α 14 列紧凑表，容器横向滚动兜底）、标的贡献表）；历史回测列表（点击切换结果）。
- 依赖：T6.4、T3.2。
- 验收：Playwright——默认参数跑 multi5，指标卡显示 A2 数值（快照容差）；切 buyhold 后 n_rebalances=0 显示；窄屏表格横滚无溢出（C6 分屏断言）。

### T6.6 调仓页
- 产出：最新 plan 卡（diff_trades 理由链+E2 徽章+from/to 权重）→ 「生成执行单」（资金输入，默认 ¥1,000,000）→ 委托表（方向/标的/数量与手数/金额/费用/结算 T+0/T+1/限价参考带）+ CSV 下载 → 确认流（纪律声明勾选框+二次确认 Modal，§7 不变量）→ 成交登记表单（每笔价格/数量，校验整手）→ 状态时间线（draft/confirmed/executed+审计摘要）；QDII 溢价警示条（组合含 QDII 时常显）。
- 依赖：T6.5、T4.2/T4.3。
- 验收：Playwright——A4 场景全流：执行单数值（18,000/9,300/¥40.01）→ 未勾选纪律无法确认 → 确认后登记成交 → 时间线与审计可见；CSV 下载文件行数正确。

### T6.7 数据页
- 产出：universe 状态表（最后日期/缺口/豁免/类型徽章）；六项验证徽章墙（点击抽屉看 detail：golden 99 等）；手动 sync 按钮组（bootstrap 需二次确认·提示时长；incremental 即发）+ job 进度条（轮询）；`/runs` 历史列表（筛选+产物直达）。
- 依赖：T6.4、T1.5。
- 验收：Playwright——incremental 触发后徽章更新、豁免与缺失分行展示；run 列表产物链接可下载。

---

## 9.8 T7 · 集成测试与交付

### T7.1 E2E 冒烟（Playwright）
- 产出：`e2e/` 两剧本：**A** 新建 multi5 复刻（新 workspace bootstrap）→ 验证全绿 → 评估 → 回测 → 执行单 → 确认登记全流；**B** seed cn4 复刻回归（评估+回测）。含门禁 409 分支、限流 429 分支 mock。
- 依赖：全部 T1–T6。
- 验收：`make test-e2e` 全绿（CI=true 无头模式）；C6——禁用整页截图像素断言。
- α 坑：AKShare 真实网络偶发抖动——E2E 允许 `AP_E2E_FIXTURE=1` 切换 fixture workspace（快照）跑数据链，保证可重复。

### T7.2 契约与锚点回归
- 产出：core OpenAPI 快照测试；α 锚点回归套件（A1–A4，fixture=α `sandbox/repo` 快照）；exec_orders 纯函数单测表（整手边界/最低佣金/印花税仅股票卖出）；权重校验/回测参数校验单测。
- 依赖：T3/T4 完成后即可先行部分。
- 验收：`make test` 全绿；A2 断言逐位（同快照）。

### T7.3 交付固化
- 产出：根 README（§2.3 DoD 演练脚本+截图）；`docs/API.md`（端点+curl 示例）；`docs/RUNBOOK.md`（compose 排障、AKShare 熔断处置、workspace 重置、fixture 刷新方法）；阿里云映射说明（MinIO→OSS/RDS/ECS 单机，主方案 §14 引用）；已知局限清单（C3 价格收益口径、日频 EOD、QDII 溢价人工核对）。
- 依赖：T7.1。
- 验收：新环境（干净目录）按 README 10 步内跑通 DoD 剧本。

---

# 10. 测试与验收

## 10.1 测试金字塔

| 层 | 工具 | 覆盖 |
| --- | --- | --- |
| 单测 | pytest（core）/ vitest（web）/ jest（bff） | 纯函数：回测指标、整手/费用、权重校验、PIT、状态机 |
| 契约 | OpenAPI 快照 + bff 透传 spec | §6 契约不漂移 |
| 集成 | pytest + fixture workspace（α 快照） | sync/review/backtest/plan/exec 全链（不依赖外网） |
| E2E | Playwright（`AP_E2E_FIXTURE` 可切） | §9.8 两剧本 |

## 10.2 回归锚点断言（fixture 快照下，逐位）

A1 六验证计数（99/90/5531/豁免3）；A2 回测八指标+1393日+69次；A4 执行单 2 单/18,000/9,300/¥40.01/现金15.8%；C2 修复后 plan cost_est≈¥40。数据滚动场景（真实外网）只断言区间合理性（±5pp 容差），并在报告标注数据截至日。

## 10.3 验收清单（协调方最终验收）

- [ ] `make up && make seed && make test` 全绿；`make test-e2e` 全绿
- [ ] §2.3 DoD 剧本人工走查通过（录屏）
- [ ] A1–A4 锚点回归通过；C2 已修复且 α CLI 回归全绿（T0.3 验收命令）
- [ ] 六项验证门禁有效（注入失败被 409 拦截）
- [ ] 无任何券商/交易外发请求（代码评审+网络白名单确认）
- [ ] 全局免责声明与审计链完整

---

# 11. 风险与缓解

| 风险 | 概率 | 缓解 |
| --- | --- | --- |
| AKShare 上游改版/限频升级 | 中 | 接口白名单收敛两源（新浪+腾讯）；fixture 快照使测试与外网解耦；适配层仅 datasource.py 单点 |
| 多 Flash 并行实施契约漂移 | 中 | §4–§6 冻结 + OpenAPI 快照测试 + 每任务验收命令；越界变更打回 |
| DuckDB/workspace 并发写损坏 | 中 | 单 workspace 互斥锁 + 单 JobRunner；E2E 并发剧本覆盖 |
| EOD 数据满足不了"实时"预期 | 低 | 产品口径明示（§4.1）；P2 实时接口预研任务挂账 |
| 长任务超时/挂死 | 低 | job 心跳+30min 自愈 failed；前端进度与取消（P1） |
| 报告 HTML 嵌入样式冲突 | 低 | iframe 隔离 + SAMEORIGIN |

---

# 12. 与 V4 路线图的衔接

- **本方案 = V4-β 的完整实施书**（主方案 §16）：在原 β 范围（Web 骨架/登录/对象库远端化/PR 审查①②）基础上，按用户指令纳入 α 已实测的 组合评估/回测/调仓 三大能力与 AKShare 在线数据层；DSH 研究所与 PAT/RS256 多实例认证仍留 RC。
- β 完成后：RC 补 `ap daemon` 任务推送、SSE、TOTP/PAT、PR 审查队列上云；GA 阿里云生产化（§14 拓扑不变）。
- IDE 本地运行时（ap CLI / ap-mcp / tasks YAML）不受影响，与 Web 共享 applib 与数据契约——双运行时同构原则（主方案 §1）保持。

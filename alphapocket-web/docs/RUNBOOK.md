# AlphaPocket V4-β · 运维手册（RUNBOOK）

适用对象：本地开发、单机部署与故障处置。命令默认在 `alphapocket-web/` 目录执行。

## 1. 启停与健康检查

### 1.1 一键开发全栈（需 docker）

```bash
cp .env.example .env
make up            # docker compose -f deploy/docker-compose.dev.yml up -d --build
make migrate       # alembic upgrade head（core 容器内）
make seed          # demo 用户 + cn4 / multi5 两组合（幂等）
make logs          # 跟随日志
make down
```

### 1.2 本地直跑（无 docker，已直跑 PG:5432 / Redis:6379）

**沙箱重置后的一键恢复（推荐）：`bash deploy/start-local-sandbox.sh`**——自动安装并初始化
PG/Redis/MinIO、跑迁移与 seed、拉起 core/bff/web，幂等可重复执行。以下为手动步骤：

```bash
make install-core                    # 建 core/.venv 并装依赖
make migrate-local                   # alembic upgrade head
make seed-local                      # 幂等灌数

# core（:8082）
PYTHONPATH=/workspace/alphapocket core/.venv/bin/uvicorn app.main:app --port 8082 --app-dir core
# bff（:8081）
pnpm -C bff install && pnpm -C bff start:dev
# web（:5173，/api 代理至 :8081）
pnpm -C web install && pnpm -C web dev
```

### 1.3 健康检查

```bash
curl -s localhost:8082/healthz   # {"status":"ok|degraded","deps":{pg,redis,minio}}
```

`status=degraded` 表示某依赖 TCP 探测失败；HTTP 仍为 200（启动期不因依赖不可达而拒绝服务）。
首次建库需创建两个扩展（compose 首次初始化自动执行 `deploy/initdb/00-extensions.sql`）：

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;      -- /securities/search 模糊检索
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
```

## 2. 常见故障处置

| 现象 | 定位 | 处置 |
| --- | --- | --- |
| core 启动后大量 `500`，测试大量 `skip` | PG 不可达 | 确认 `AP_DATABASE_URL` 与 PG 端口；`make migrate-local` 重跑迁移 |
| 登录接口 500 / 限流异常 | Redis 不可达 | 确认 `AP_REDIS_URL`。注：**登录锁定检查为 fail-open**（Redis 不可达时放行并记 WARNING），可用性优先 |
| `/data/sync` 返 `503 UPSTREAM_RATE_LIMITED` | AKShare 熔断已打开 | 见 §3 |
| 提交计算任务返 `409 JOB_LOCKED` | workspace 计算互斥 | 同一 workspace 同时仅 1 个计算 job；等前一个终态或前端排队轮询 |
| 计算任务返 `409 VERIFY_GATE_FAILED` | 六项验证未全绿 | 先跑数据同步并查看 `/data/verifications` 明细，红灯项修复后再计算 |
| 产物 404 | 产物未生成 / 对象存储不可达 | 确认 run 已 `done`；检查 MinIO（`AP_MINIO_*`） |
| `make migrate` 报 `Target database is not up to date` | 迁移分叉 | 先 `alembic history` 对齐；PG 单写方只有 core，勿手工改表 |
| 端口被占用 | 8080/8081/8082 | 改 `.env` 的 `*_PORT` 或释放端口 |
| 预览面板 `no healthy upstream` | 平台边缘网关 → 沙箱的会话注册失效（应用侧正常） | 按 §2.1 自检确认后重开会话；恢复后跑一键脚本 |

### 2.1 预览 `no healthy upstream`（边缘注册失效，2026-10-09 实测）

现象：预览面板打开报 `no healthy upstream`，重开面板/重新注册均无效，但本地服务与数据全部正常。

处置流程：

```bash
# 1) 确认应用与预览代理自身健康（三条都应 200；有失败先修应用侧）
curl -s -m 3 -o /dev/null -w 'web=%{http_code}\n'  localhost:5173
curl -s -m 3 -o /dev/null -w 'proxy=%{http_code}\n' localhost:16000
curl -s -m 3 -o /dev/null -w 'core=%{http_code}\n'  localhost:8082/healthz

# 2) 点击预览链接后，确认边缘流量是否真的进入沙箱：
#    无输出 = 边缘请求根本没进来，注册已失效
ss -tn | grep ':16000' | grep -v '127.0.0.1'
tail -20 /var/log/tool/agent-tool-host.stdout.log | grep preview-proxy

# 3) 第 1 步全 200 且第 2 步无外部流量 → 平台侧问题，重开会话重建 Pod↔边缘映射；
#    会话恢复后一键拉起全栈（数据/代码在 /workspace，不丢失）
bash deploy/start-local-sandbox.sh
```

特征佐证：快照恢复型沙箱中，`agent-tool-host` 进程环境残留的 `HOSTNAME` 可能是旧 Pod 名
（`tr '\0' '\n' < /proc/$(pgrep -f agent-tool-host)/environ | grep ^HOSTNAME`），与当前
Pod（`hostname`）不一致时，即边缘上游仍指向已销毁的旧 Pod，重开面板无法自愈。

## 3. AKShare 熔断（数据同步上游）

- 触发：`data_sync` 连续上游失败达 `FAIL_THRESHOLD=3` → 写熔断键
  `ap:sync:upstream_circuit`，TTL `600s`。
- 表现：熔断期内 `POST /api/v1/data/sync` 立即返回 `503 UPSTREAM_RATE_LIMITED`。
- 处置：等待 TTL 自动恢复；确需立即重试时人工清除（仅限确认上游已恢复）：

```bash
redis-cli DEL ap:sync:upstream_circuit
```

- 适配层为单点：`alphapocket/applib/datasource.py`；数据源主/交叉/对账/降级关系已
  声明化（见 §3.1），上游改版只需改对应 spec 声明 + interface 实现函数。

### 3.1 数据源路由（T-R11 声明式）

数据集与其多源关系统一登记在 `alphapocket/applib/datasets/specs_*.py`（T-R9
DatasetSpec），每个数据集的 `sources[]` 按 `primary / cross / reconcile / fallback`
四角色声明：

| 数据域 | primary | cross | reconcile | fallback |
| --- | --- | --- | --- | --- |
| A 股行情 | 新浪 stock_zh_a_daily(qfq) | 腾讯 stock_zh_a_hist_tx | — | — |
| 美股行情 | 腾讯 usK线(raw 口径) | — | 新浪 stock_us_daily(raw) | 新浪 raw 升主源（腾讯整体失败时） |
| 港股行情 | 新浪 stock_hk_daily | 腾讯 web.ifzq.gtimg.cn | — | — |
| 实时行情 | 新浪批量 | — | — | 腾讯逐标的 |
| 港股通 | 雪球在线 | — | — | ggt_static 快照（`GGT_SNAPSHOT_DATE`） |
| Fed 利率 | ak.macro_bank_usa_interest_rate | — | — | 今值缺失→前值兜底 |
| 宏观各指标 | `_MACRO_META` 逐条 source | — | — | 逐指标 error 登记不阻断 |

- `provenance.json` 的 `sources[]`（dataset/interface/upstream/params/file/sha256/bytes
  七键）由 `_reg` 从 spec 声明解析登记；未注册数据集走手工参数（WARNING 透明降级，不阻断）。
- 上游改版处置：改 spec 的 `interface` 声明 + 对应实现函数即可，`fetch_all` 主体无需动。

## 4. workspace 的查看 / 重置

- 根目录由 `AP_WORKSPACE_ROOT` 决定（默认 `var/ws/default`），布局
  `var/ws/<name>/{remote,sandbox}`；`sandbox/data/ap.duckdb` 为因子宽表。
- **DuckDB 单文件单写**：同一 workspace 的计算必须串行（`compute_lock`）；
  sync 全局唯一（`sync_lock`）。切勿绕过 JobRunner 并发直连 DuckDB。
- **数据集清单入口（T-R9）**：`alphapocket/applib/datasets/specs_*.py` 声明
  `DatasetSpec`（落盘路径/合并策略/入库表/验证联动/多源路由）——新数据集只改声明，
  即自动进增量合并（`_MERGEABLE` 由 active specs 派生）、provenance 登记与验证链路，
  `fetch_all` 主体不动。
- **指标注册表写保护（T-R6）**：`sandbox/repo/factors/registry.json`（v2）在每次
  同步（含 rmtree 重建）前先快照、尾部按 merge 语义回写——用户的指标启停/调参跨
  同步保留；管理入口为 `ap metric …` CLI 与 `engine.metrics.*` MCP（API.md §7）。
- **同步语义（2026-10-09 起）**：`incremental` / `refresh` 走**增量合并**——
  `fetch_all` 先快照 remote/repo 可合并数据集再重建，按 secid 合并（旧尾 + 新窗口，
  重叠日以新为准）；空窗口或个别标的拉取失败时旧数据完整保留，**不再出现"一次增量
  把已绿数据拉红"**。`bootstrap` 仍为全量整体重建。
- **calendar_check 尾部豁免**：末根落后 1 个交易日且腾讯交叉源已有该日 →
  「发布滞后豁免」（新浪港股个别标的收盘后延迟发布，实测可达数小时）；腾讯亦无 →
  「尾部无行情豁免」（停牌）。落后 ≥2 个交易日且腾讯有更新仍判 FAIL（真缺失）。
  豁免在验证明细中透明计数，不连坐全市场。
- `market/macro_liquidity.json`（引擎C 宏观流动性指标，月频为主；已纳入增量合并
  `_MERGEABLE`，增量窗口为空时旧值保留）。
- 重置（数据全量重建）：

```bash
rm -rf var/ws/default
curl -X POST localhost:8082/api/v1/data/sync -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"mode":"bootstrap"}'   # 全量重拉
```

## 5. 回归 fixture（α 快照）刷新

T3/T7 的回测与执行单锚点回归只读 **α 快照**，不依赖外网：

| 用途 | 默认路径 | 覆盖环境变量 |
| --- | --- | --- |
| α 根 | `/workspace/alphapocket` | `AP_ALPHA_REPO_DIR` |
| 评估/回测快照 repo | `<α根>/remote/repo` | `AP_ALPHA_SNAPSHOT` |
| A2 回测锚点产物 | `<α根>/sandbox/artifacts/bt_20261005_063846_98bce9/backtest.json` | `AP_ALPHA_A2` |
| A4 执行单锚点产物 | `<α根>/sandbox/artifacts/run_20261005_072944_1cb997/exec_order.json` | `AP_ALPHA_A4_RUN` |

刷新流程：用 α CLI 在 `alphapocket/` 内重新生成
`remote/repo`（`ap fetch` / `ap clone`）与 `sandbox/artifacts/**`（`ap run` / `ap backtest` /
`ap exec-plan`），再更新上表锚点常量（见 `core/tests/test_backtest_params.py`、
`core/tests/test_exec_orders.py`）。锚点按 §10.2 冻结，改动必须显式评审。

## 6. 验收命令

```bash
make lint        # core(ruff) + bff(eslint) + web(eslint)
make test        # core(pytest) + bff(jest) + web(vitest)
pnpm -C bff build && pnpm -C web build
make test-e2e    # Playwright（T7.1：剧本 A/B + 409/429/401 分支）
```

> ⚠️ **core pytest 会写/清默认库**（`.env` 的 `AP_DATABASE_URL` 指向的库）：`test_migrations.py`
> 会 `drop_all` 全部表。存有真实 job/runs 历史的库请按 **§6.2 独立测试库** 跑，勿直接 `make test-core`。

**当前实测结果**（2026-10-10，PG 16 + Redis 7 就绪，core 走 §6.2 独立库；
含引擎组件化重构 T-R1~T-R12 总验收）：

| 项 | 结果 |
| --- | --- |
| α 内核 `pytest tests/` | **226 passed**（T-R1~T-R11 各任务新增用例后递增） |
| core `ruff check app tests` | 通过 |
| core `pytest` | **188 passed / 3 skipped**（跳过项为需 `AP_NET_TESTS=1` 的外网用例） |
| bff `eslint` | 通过 |
| bff `jest` | 9 suites / **41 passed** |
| bff `nest build` | 通过 |
| web `eslint` | 通过 |
| web `vitest` | 17 files / **100 passed** |
| web `vite build` | 通过 |
| core OpenAPI 契约快照 | 20 端点冻结，通过 |
| `make test-e2e` | **7 passed**（夹具驱动，不依赖外网/后端） |

PG/Redis 未就绪时，core 中依赖数据库的用例会整体 `skip`（不判失败），以便纯 CI 环境仍绿。

### 6.1 E2E 运行前置（沙箱/CI）

E2E 默认经 CDP 复用既有 Chrome（避免下载 Playwright 浏览器）；`e2e/fixtures/test.ts` 读取
`AP_E2E_CDP`（默认 `http://127.0.0.1:9222`）：

```bash
# 复用既有 Chrome（推荐，无外网）
google-chrome --headless=new --remote-debugging-port=9222 &
make test-e2e

# 无 CDP 时回退本地启动 chromium（需已 npx playwright install）
AP_E2E_CDP=launch make test-e2e
```

用例全程由 `page.route` 拦截 `/api/**` 返回冻结夹具（`e2e/fixtures/api-mock.ts`），
不依赖真实后端、MinIO 与外网；`webServer` 会自动拉起 `web`（`vite :8080`）。

### 6.2 测试环境与数据库安全（2026-10-09 实测）

**对共享 PG 有写入/破坏的测试面**（core 套件）：

| 测试 / 夹具 | 行为 |
| --- | --- |
| `test_migrations.py` | `drop_all` **全部表**后往返重建——jobs/runs/verify_results/reviews/plans/exec_orders 全历史与用户自建 securities/portfolios **被清空** |
| `test_gate.py` 的 unknown 用例 | 删除**全部** `data_sync` job 的 `verify_results`（构造无记录场景） |
| `seeded` 夹具（sync/gate/seed/portfolios） | 幂等 upsert demo 数据（不清用户数据） |
| `tracked` 夹具（多数文件） | 只清理本用例创建的行（安全） |

影响范围仅 PG；workspace 文件（`var/ws`）与 MinIO 产物不受影响，但 DB 记录清空后
产物入口会 404。bff / web / e2e 测试不连 PG，可放心跑。

**顺序陷阱**：pytest 按文件名字典序执行，`test_migrations`（m）排在
`test_backtest_api` / `test_data_status` / `test_exec_flow` 等之后——套件隐含
「起跑时表已存在」前提（主库 `ap` 平时满足）。**空库不迁移直接跑套件会前半场
`UndefinedTable` 失败（实测 28 个）**，必须先 `alembic upgrade head`。

**安全跑法（推荐：独立测试库）**——环境变量优先于 `.env`，且 alembic `env.py` 与
app 同源读取 `AP_DATABASE_URL`：

```bash
# 一次性：建库 + 建表
su postgres -c "createdb -O ap ap_test"
cd core && AP_DATABASE_URL=postgresql+psycopg://ap:ap@localhost:5432/ap_test \
  ./.venv/bin/alembic upgrade head

# 每次跑
cd core && AP_DATABASE_URL=postgresql+psycopg://ap:ap@localhost:5432/ap_test \
  PYTHONPATH=/workspace/alphapocket ./.venv/bin/python -m pytest -q
```

实测：独立库 **188 passed / 3 skipped**（与主库结果一致），主库 `ap` 的
jobs/runs/verify_results 行数零变化。

**已在主库跑过测试之后**：历史记录不可恢复（无备份时）；恢复演示态用
`make seed-local`（seed 幂等）。

## 7. 云端映射（GA · 主方案 §14）

| dev（compose） | 阿里云生产 |
| --- | --- |
| pg 容器 | RDS PostgreSQL（同 DDL，Alembic 管理） |
| redis 容器 | 云数据库 Redis |
| MinIO 桶 `ap-artifacts` | OSS 桶（S3 兼容 endpoint 替换） |
| core/bff/web 容器 | ECS 单机（compose 等价编排）或容器服务 |

切换只需替换 `AP_DATABASE_URL / AP_REDIS_URL / AP_MINIO_*` 等环境变量，无需改代码。

## 8. 已知局限

- **价格收益口径（C3）**：组合净值为「等权·日再平衡·名义」近似，非真实持仓份额账本；
  最大回撤为近 252 交易日窗口。
- **日频 EOD**：数据口径为日频收盘，满足不了日内实时预期（§4.1 已明示）。
- **QDII 溢价**：跨境品种场内价可偏离净值，执行前须人工核对 IOPV 溢价。
- **β 单用户**：审计 actor 固定为 `system`；多用户/权限待接入登录用户后替换（T5.2）。
- **引擎C 口径限制**：美联储资产负债表/美国 M2/欧日央行表在免费源缺失，D1 以政策利率+CPI 近似、
  US 的 D2 维度缺席（报告中透明登记）；引擎C 为市场级条件变量，不参与权重决策。
- **指标新公式仍需代码实现（T-R 包）**：注册表只承载启停/参数/权重/锚点/维度归属；
  `metric add` 的 `formula` 必须引用 `factors.py` 内置公式白名单——新公式本身仍需在
  `factors.py` 实现并经测试（公式属代码，启停调参属配置）。
- **锚点说明文案硬编码**：`report.py` 五维锚点的说明文案仍为硬编码文本，与 registry
  `anchors` 可能漂移——改锚点时需同步文案。
- **宏观指标无横截面回放**：`ap metric test --tool walkforward` 仅适用引擎A/B；
  对 macro 引擎明确报错（引擎C 为市场级条件变量，无个股横截面可回放）。
- 无券商/交易外发请求（代码评审 + 网络白名单确认）。

## 9. 引擎C 宏观流动性（运行口径 · T-C8 收尾）

> 纪律：市场条件变量——**不决策、不参与权重**；不进 data_risk、不进六项验证门禁
> （缺失 → unavailable 透明降级，评估照常完成）。

### 9.1 采集与查看（双入口同一内核）

```
ap fetch 宏观    # fetch_macro_liquidity：12 指标设计（实测 11 命中，HK 三指标 unavailable 降级）
                 # → sandbox/repo/market/macro_liquidity.json
ap macro         # CLI：冻结五键 JSON 头 + 三市场逐指标 dim/score/分位表 + 口径注记
MCP factor.macro # 工具面：读同一 payload → compute_macro_liquidity（与 CLI 逐字段一致）
```

`ap clone` 后评估自动回填报告 §2.5 与前端引擎C 卡片；payload 缺失 → 降级灰条（不失败）。

### 9.2 三层聚合口径（T-C2，scorer 版本 `v1`）

1. **单指标 scorer**：历史窗口分位点 × 方向 → 阈值触发（±2）/ 跳平带（|s|<0.2 → 0）/
   前值站位（carried_prev，滞后标注）/ 回退（样本不足 → `insufficient` 档 0，聚合排除不折损）。
   分位窗：日度 250 日、月度 12 期（新旧双期排位）。
2. **维度等权**：D1 货币（M2/社融/信贷脉冲）· D2 信用社融 · D3 利率（Shibor/LPR/FR007…）·
   D4 跨境利差（中美 10Y / FRA-OIS…）→ 维内均值 → 跨维均值；缺维度/缺市场不折损已知维度。
3. **状态机**：全局五档 ±2…+2 → `state ∈ {tight, neutral, loose}`（+unavailable）；
   市场级按 A/HK/US 独立聚合，HK 三指标资产缺失 → 市场级 `"unavailable"`。

### 9.3 增量防丢口径（T-C1，红线 4）

- 失败/空窗口 → **旧数据零丢失**：`carried_old` 合并，逐指标落 provenance；
  成功 → union 合并、同 dt 新值覆盖、**永不物理删除**历史点。
- 月度指标（M2/社融等）按 **12 个月滚动窗**拉取；PIT 防前视：月度指标 as_of 当月站位点
  不可用（用上月值站位），日度严格 `dt<=as_of`（T-C2）。

### 9.4 已知边界（诚实登记）

- **HK 三指标（H-0006 资产缺失）**：`hk_rates.json` 无法登记 → 港股市场级持续 unavailable
  （不折损 A/US 聚合），待数据资产补齐。
- **engines 配置注入**：引擎C 不经 engines 配置注入版本不符由组件化 T-R3 收编（本版本内联）。
- **扩展位**：D2/D3 补 `dr007` / `repo_tier_007` / `m1_m2_gap`（macro 12→16 指标）→ M7。
- e2e 夹具就位但 chromium 实跑受阻（T-C7 登记），单测已覆盖同一渲染分支。

## 10. 引擎配置注入与版本绑定（T-R3 · 处置口径）

- **配置位置**：`sandbox/repo/config/engines.json`（或进程内 dict），形态
  `{engine_id: {params, enabled, registry_version}}`；缺文件/缺条目 → 走函数签名
  默认并输出 `_config_warnings`（不静默）。
- **注入顺序**：显式入参 > `config.params` > compute_x 函数签名默认；注入经包装层
  `__call__(args, config=...)`，不得绕过 T-R1/T-R2 契约（校验/位范围/provenance 全程生效）。
- **版本绑定**：配置 `registry_version` 与 `ENGINE_REGISTRY_VERSION`（engines/base.py，
  当前 `v1`）核对。**不符 → `_version_warning` 显式登记：结果照常产出、不静默、不阻断**
  （已知遗留观察「版本不符、结果正确产出」即此口径，与引擎C 00-总览 §8 双向引用）。
- **处置**：版本不符出现后 → 1) 核对配置文件的 registry_version 是否过期；
  2) 若为引擎集/契约变更 → bump base.py 常量（M1 注册中心建立后改由 registry 提供）；
  3) 对齐后重跑该评估 run，`_version_warning` 消失即为处置完成。
- **停用**：`enabled: false` → 包装器返回 `status=disabled`（不进公式）；
  报告/工具层对该引擎输出的消费方须处理 disabled 形态（T-R11 回归面覆盖）。

## 11. 数据同步三模式（T-R8 · `ap fetch --mode`）

- **bootstrap**：全量重建（首次落地/灾后重建；rmtree + 全量拉取 + restore_guard 回写）。
- **incremental**：日常同步；不 rmtree（单数据集拉取失败 → 旧文件保留）；
  `incremental_from` 缺省时**自动从库内各标的最末 dt 推导起点**；K 线走 union_bars 合并。
- **refresh**：合并式刷新——**替换最近 5 个交易日（7 自然日窗口）而非整体覆盖**，
  同 dt 新值覆盖=点位修正，历史不动；用于数据源修正回填后的定点刷新。
- **缺省（不传 --mode）**：按参数推导（有 incremental_from → incremental，否则 bootstrap）
  并输出 `mode_warning` 告警（防误触覆盖）；建议生产路径全部显式传 mode。
- **冲突拒绝**：`--mode bootstrap/refresh` 与显式 incremental_from 同传 → ValueError
  （窗口自定，防旁路覆盖）。模式与起点写入 `provenance.mode/mode_source` 可追溯。

## 12. registry 写保护与 orphan 处置（T-R4/T-R7 · 三语义）

- **三语义**：① 覆盖→merge（机器拉取不再整文件覆盖用户登记）；② 用户改动优先
  （条目字段冲突保用户值；`MACHINE_FIELDS = {version, universe, type, horizon_days}`
  为机器字段新胜——边界见 [writeguard.py](../../alphapocket/applib/writeguard.py)）；
  ③ orphan 标记不物理删（消失条目加 `orphan: true` + `orphaned_at`，可审计可恢复）。
- **守护面**（GUARDED_RELS，快照-恢复守护）：`factors/registry.json`、
  `portfolio/cn4.json`、`portfolio/multi5.json`、`market/daily_bars.json`、
  `market/daily_bars_raw.json`、`market/cross_check_tx.json`；财务/分红/日历/市值/宏观
  为上游全量重拉（覆盖=修正），不入守护面。
- **orphan 处置**：orphan 条目默认保留在文件内（不参与计算但可查询）；
  人工确认废弃后可物理删除（删前建议 `ap` 外部留档）；恢复 = 下一轮拉取重现即自动去 orphan。
- **合并策略单点**：merge 逻辑一律委托 [datasets](../../alphapocket/applib/datasets/__init__.py)
  的 MERGE_POLICY_FN 注册表（overwrite / union_bars / registry_merge / portfolio_merge /
  carry_old），restore_guard 落盘格式由 MERGE_POLICY_FORMAT 声明；新增策略只注册表，不得私造第二实现。

## 13. data_missing 检测（T-R10 · 提示不阻断，与门禁失败语义的显式区别）

- **入口**：MCP 工具 `data.missing`（stale_days 可调，缺省 7）→
  [check_data_missing](../../alphapocket/applib/datasets/__init__.py) 单点检测。
- **驱动源**：DatasetSpec.validation 声明（`required_keys` / `min_rows` / `as_of_field`）；
  新增数据集在 registry.json 写声明即自动接入检测（零代码）。
- **severity**：missing(4) > invalid(3) > incomplete(2) > stale(1) > ok(0)，
  单数据集只返回最严重 status 与 reason。
- **红线（透明降级）**：检测结果只产提示、**不抛异常、不阻断评估流程、不进门禁**——
  web 黄条 / 引擎C unavailable 灰条同语义消费。与行情门禁 data_risk 的失败语义
  （可阻断、可 FAIL）显式区别：数据缺失 → 提示可见 + 结果标注 unavailable/降级，流程继续。
- **全量缺失也安全**：空仓/全缺场景返回结构化结果（summary 计数 + semantics 自描述），不失败。

## 13. AP-Checkpoint 快速启动（T2/T3 落地记录）

脚本：`deploy/ck/`（ck-env / pull-l0 / restore-l0 / start-infra / restore-l1 / warm-repo / build-l0* / build-l1）。

### 13.1 冷启动恢复链（实测通过，2026-10-17）

```
pull-l0.sh（Release 253MB，sha 校验）→ restore-l0.sh（venv/node/infra 解压+自检）
→ start-infra.sh（embedded PG16 initdb + redis + minio，零 apt）
→ restore-l1.sh（ap-state pg_restore，数据 1/15/3）→ 起三件套 → warm-repo.sh（增量）
```

### 13.2 已知边界（实测发现，诚实登记）

1. **港股 qfq 前复权漂移 × 增量合并**：qfq 序列随最新价动态漂移，incremental 合并
   新旧 qfq 后 adjust_check 会对历史点报红（如 `00700.HK@2022-01-20`）。
   处置：增量同步后若 adjust_check fail → 跑一次 bootstrap 全量重建（qfq/raw 同源即绿）。
   根治方向：datasource 对 HK 的 qfq 增量改为整序列替换。
2. **redis 非基础动态库**：apt redis 依赖 liblzf/jemalloc/libsystemd 等 14 个非基础
   .so，重置即丢——L0 打包时必须带 `libs/` 并在启动时设 `LD_LIBRARY_PATH`（已内置）。
3. **Debian PG 硬编码路径**：initdb/postgres 编译期指向 `/usr/{lib,share}/postgresql/16`，
   embedded 布局经 `ln -sfn` 接回（start-infra 内置），另 initdb 需 `-L` 或链接先行。
4. **gh 认证**：hosts.yml 随系统盘丢失，ck_ensure_gh_token 从 `/run/shared/git/credentials`
   恢复 GH_TOKEN（git push 一直可用的原因即此共享挂载）。
5. **gh release download 产空文件**：脚本环境下偶发静默空文件（实测），pull-l0 改用
   `gh api` 列元数据 + `curl -H "Accept: application/octet-stream"` 直下，稳定。

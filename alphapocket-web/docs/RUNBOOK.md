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

## 3. AKShare 熔断（数据同步上游）

- 触发：`data_sync` 连续上游失败达 `FAIL_THRESHOLD=3` → 写熔断键
  `ap:sync:upstream_circuit`，TTL `600s`。
- 表现：熔断期内 `POST /api/v1/data/sync` 立即返回 `503 UPSTREAM_RATE_LIMITED`。
- 处置：等待 TTL 自动恢复；确需立即重试时人工清除（仅限确认上游已恢复）：

```bash
redis-cli DEL ap:sync:upstream_circuit
```

- 适配层为单点：`alphapocket/applib/datasource.py`，接口白名单收敛「新浪 + 腾讯」两源；
  上游改版只需改此处。

## 4. workspace 的查看 / 重置

- 根目录由 `AP_WORKSPACE_ROOT` 决定（默认 `var/ws/default`），布局
  `var/ws/<name>/{remote,sandbox}`；`sandbox/data/ap.duckdb` 为因子宽表。
- **DuckDB 单文件单写**：同一 workspace 的计算必须串行（`compute_lock`）；
  sync 全局唯一（`sync_lock`）。切勿绕过 JobRunner 并发直连 DuckDB。
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

**当前实测结果**（PG 16 + Redis 7 就绪，`make migrate-local && make seed-local` 之后）：

| 项 | 结果 |
| --- | --- |
| core `ruff check app tests` | 通过 |
| core `pytest` | **163 passed / 1 skipped**（跳过项为需 `AP_NET_TESTS=1` 的外网用例） |
| bff `eslint` | 通过 |
| bff `jest` | 9 suites / **39 passed** |
| bff `nest build` | 通过 |
| web `eslint` | 通过 |
| web `vitest` | 16 files / **86 passed** |
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

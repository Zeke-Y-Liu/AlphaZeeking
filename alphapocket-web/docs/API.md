# AlphaPocket V4-β · API 参考（core `/api/v1`）

> 契约唯一事实源：core 的 OpenAPI 文档（`http://localhost:8082/openapi.json`）。
> 该文档已被 `core/tests/test_openapi_snapshot.py` 冻结为
> `core/tests/snapshots/openapi.json`——端点/参数/schema 任何漂移都会使该用例失败。

## 1. 拓扑与路由

```
浏览器 ──/api/*──▶ bff :8081（JWT 鉴权 / 限流 / DTO 裁剪 / 原样透传）
                        └──▶ core :8082（/api/v1，FastAPI）
```

| 层 | 地址 | 说明 |
| --- | --- | --- |
| web | `:8080` | Vue3 前端；dev 下 `/api` 代理至 bff |
| bff | `:8081` | NestJS 网关；对外前缀 `/api` |
| core | `:8082` | FastAPI；前缀 `/api/v1`；OpenAPI 在 `/openapi.json` |

- **认证端点为 BFF 直接暴露**：`/api/auth/{login,refresh,me}`（非 core `/v1` 前缀）。
- 其余端点前端统一走 `/api/v1/*`，BFF 原样透传。

## 2. 认证

除 `/healthz` 与登录/刷新外，所有端点需携带 `Authorization: Bearer <access>`。

### POST `/api/auth/login`（core 亦有 `/api/v1/auth/login`）

请求：`{"username": "demo", "password": "alphapocket"}`
（演示账号由 `make seed` 创建：`demo` / `alphapocket`，role=owner）

响应 `200`：

```json
{
  "access": "<JWT>",
  "refresh": "<JWT>",
  "token_type": "bearer",
  "expires_in": 900,
  "user": {"id": 1, "username": "demo", "role": "owner"}
}
```

- 用户名/密码错误 → `401 UNAUTHORIZED`（不区分用户是否存在，防枚举）；
- 连续失败超阈值 → `429 ACCOUNT_LOCKED`（锁定窗口见 `core/app/api/v1/auth.py`）。

### POST `/api/auth/refresh`

请求：`{"refresh": "<JWT>"}` → 同上 `TokenOut`；无效/过期/类型不符 → `401`。

### GET `/api/auth/me`

→ `{"id": 1, "username": "demo", "role": "owner"}`（需 Bearer access）。

## 3. 统一响应约定

### 3.1 异步 Job（长任务）

`POST` 触发类端点（sync / review / backtest / rebalance-plan）统一返回 `202`：

```json
{"job_id": "job_<ts>_<rand>", "poll": "/api/v1/jobs/job_<ts>_<rand>"}
```

轮询 `GET /api/v1/jobs/{job_id}` 获取 `{id,type,portfolio_id,status,progress,result,error,...}`；
终态 `status ∈ {done, failed}`，`result` 内含 `run_uid` 与产物 key 列表。前端轮询间隔 ≥1s。

### 3.2 统一错误体

任何错误响应体恒为：

```json
{"code": "VERIFY_GATE_FAILED", "message": "数据验证未全绿", "detail": {...}}
```

| code | HTTP | 场景 |
| --- | --- | --- |
| `VERIFY_GATE_FAILED` | 409 | 数据门禁未全绿即请求计算 |
| `JOB_LOCKED` | 409 | workspace 计算互斥冲突（同步 job 全局唯一） |
| `WEIGHT_NOT_NORMALIZED` | 422 | 组合权重和越界（附归一建议） |
| `SECURITY_NOT_FOUND` | 422 | 标的未注册 |
| `ACK_REQUIRED` | 422 | 执行单确认缺少纪律声明（`ack_discipline:true`） |
| `VALIDATION_ERROR` | 422 | 通用参数校验失败（`detail` 为 `[{loc,msg,type}]`） |
| `UNAUTHORIZED` | 401 | 认证失败 |
| `ACCOUNT_LOCKED` | 429 | 连续登录失败锁定 |
| `UPSTREAM_RATE_LIMITED` | 503 | AKShare 上游熔断 |
| `NOT_FOUND` | 404 | 资源不存在 |
| `INTERNAL_ERROR` | 500 | 内部错误（堆栈只进日志） |

## 4. 端点清单

### 4.1 健康检查

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 存活检查；返回 `{status, deps:{pg,redis,minio}}`；依赖不可达 → `status=degraded`（仍 200） |
| GET | `/api/v1/_ping` | 路由挂载自检 |

### 4.2 标的（T1.3；V4-β 多市场）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/securities/search?q=&market=` | 代码/名称/secid 前缀检索；空 `q` 返回最近 20；`market=A\|HK\|US` 按市场过滤 |
| POST | `/api/v1/securities/register` | `{code, market?, asset_type?}`；A股 6 位 / 港股 1-5 位数字（自动补零，如 `700`→`00700.HK`）/ 美股 1-6 位字母（`AAPL`→`AAPL.US`）；港股自动标记港股通（雪球名单，降级静态快照）；已存在幂等返回 |

> V4-β 字段：响应含 `market`（A/HK/US）、`currency`（CNY/HKD/USD）、`hk_connect`
> （仅港股有值：`true` 港股通标的 / `false` 非港股通 / `null` A股或美股）。

### 4.2a 实时行情（V4-β）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/market/quotes?secids=` | A股/港股/美股混合批量实时行情（逗号分隔，单次 ≤50）+ 汇率快照 `{HKD_CNY, USD_CNY}`；新浪 hq 主源（批量单请求）、腾讯兜底；Redis 5s 缓存；港/美标的附 `price_cny` 人民币折算价；双源失败条目带 `error`，不影响其余 |
| GET | `/api/v1/market/ggt` | 港股通标的名单 `{codes, count, source}`（雪球在线 → 静态快照降级） |

### 4.3 数据（T1.4 / T1.5 / T1.6）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/data/sync` | `{mode: bootstrap\|incremental\|refresh, secid?}` → `202` job；`refresh` 必须带 `secid`；结束时自动跑六项验证。熔断中 → `503`，运行中重复提交 → `409 JOB_LOCKED`。V4-β：universe 支持 A股/港股（新浪 qfq+raw、腾讯交叉、除息日豁免）/美股（腾讯主序列 raw 口径、新浪 raw 对账）混合 |
| GET | `/api/v1/data/status` | 各标的最后 bar 日期/缺口/距今（**按市场分组**：三市场交易日历独立）、universe 汇总（含 `markets` 计数）、最近验证与 sync 状态 |
| GET | `/api/v1/data/verifications?limit=` | 六项验证明细列表；`limit` ∈ [1,200]，默认 20；返回 `{items, count, limit}` |

### 4.4 组合（T2.2）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/portfolios` | 组合列表（含持仓数、最近 run 摘要） |
| POST | `/api/v1/portfolios` | `{name, pid?, note?, positions:[{secid,weight}], equal_weight?}` → `201`；权重和 ∈ [99.5,100.5]，越界 `422 WEIGHT_NOT_NORMALIZED`。V4-β：持仓支持 A股/港股/美股混合（同一组合可含 A股+港股通+美股） |
| GET | `/api/v1/portfolios/{pid}` | 详情（含 positions；V4-β 附 `market`/`currency`/`hk_connect`） |
| PUT | `/api/v1/portfolios/{pid}` | 更新（重跑校验 + 重新物化） |
| DELETE | `/api/v1/portfolios/{pid}` | 归档软删（`status=archived`） |

> 列表/详情中的 `latest_run` 原样返回 `{run_uid, type, as_of, created_at, summary}`；
> 列表页四指标（当日·YTD·1年·MDD）取自 `latest_run.summary.portfolio_metrics`（仅 review run 具备）。
>
> **引擎C `summary.macro_liquidity`（T-C6 冻结契约）**：review run 专属，字段级冻结
> （向后兼容只增不改，改名/删字段 = 破坏性变更须走契约修订）——
> `{"state","global","markets":{A,HK,US},"global_score","as_of"}`；
> 降级归一 unavailable 形态（`state="unavailable"`、`global_score=null`、`as_of=null`）。
> 边界：该字段**不进 data_risk**（不参与数据风险判定）、**不进六项验证门禁**
> （宏观流动性数据缺失区别于行情数据——透明降级，评估照常完成）。
> V4-β：多市场组合 `base_ccy=CNY`，各持仓 `trade_ccy` 按标的币种（A股 CNY / 港股 HKD
> （港股通以港币报价、人民币结算）/ 美股 USD），折算口径用 workspace `market/fx.json` 汇率。

### 4.5 评估 / 回测 / 调仓（T2.3 / T3.2 / T4.1）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/portfolios/{pid}/review` | `202` job；前置数据门禁全绿，否则 `409 VERIFY_GATE_FAILED` |
| POST | `/api/v1/portfolios/{pid}/backtest` | `{start?,end?,rf?,rebalance?,benchmark_secid?}` → `202` job |
| POST | `/api/v1/portfolios/{pid}/rebalance-plan` | `202` job；产出 plan（`status=draft`，引擎永不直接动仓位） |
| GET | `/api/v1/plans/{plan_id}` | plan 详情 |
| GET | `/api/v1/portfolios/{pid}/plans` | 组合的 plan 列表 |

### 4.6 运行历史 / 任务 / 产物

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/runs?type=&portfolio_id=&limit=` | 运行历史（最新在前） |
| GET | `/api/v1/runs/{run_uid}/artifacts/{file}` | 产物流；`content-type` 按扩展名（`report.html` 供 iframe 嵌入） |
| GET | `/api/v1/jobs/{job_id}` | job 状态/进度（§3.1） |

### 4.7 执行单 / 审计（T4.2 / T4.3 / T4.4）

`POST /plans/{plan_id}/exec-orders` 为**同步**返回（毫秒级机械换算，非 job）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/plans/{plan_id}/exec-orders` | `{capital_cny?}`（默认 ¥1,000,000）→ `201` 执行单；plan 不存在 `404`，plan 过老 `409` |
| GET | `/api/v1/exec-orders/{exec_id}` | 详情（完整 sheet + 状态时间线） |
| POST | `/api/v1/exec-orders/{exec_id}/confirm` | `{ack_discipline:true}` → `confirmed`；缺 ACK `422 ACK_REQUIRED`；非法跃迁 `409/422` |
| POST | `/api/v1/exec-orders/{exec_id}/fill` | `{fills:[{secid,price,shares}]}` → `executed`；secid 须在单内、价格 > 0、数量为 100 整数倍且方向一致，否则 `422`；允许部分成交 |
| POST | `/api/v1/exec-orders/{exec_id}/cancel` | `{reason?}` → `cancelled`（`draft`/`confirmed` 可取消） |
| GET | `/api/v1/portfolios/{pid}/exec-orders` | 该组合执行单列表（创建时间倒序） |
| GET | `/api/v1/portfolios/{pid}/rebalance-history` | plans + exec_orders + fills 归一化时间线 |
| GET | `/api/v1/audit?entity=&entity_id=&limit=` | 审计列表（时间倒序，`limit` ∈ [1,200]） |

状态机：`draft → confirmed → executed`，或 `draft/confirmed → cancelled`；每次跃迁写审计时间线。
本模块只做换算与状态/审计写入，**无券商/交易外发请求**（§7 不变量 1）。

## 5. curl 示例

```bash
BASE=http://localhost:8082          # 直连 core；经 BFF 时用 http://localhost:8081/api

# 健康检查
curl -s $BASE/healthz

# 登录（演示账号）
TOKEN=$(curl -s -X POST $BASE/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"demo","password":"alphapocket"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access"])')

AUTH="Authorization: Bearer $TOKEN"

# 标的检索 / 注册
curl -s "$BASE/api/v1/securities/search?q=5103" -H "$AUTH"
curl -s -X POST $BASE/api/v1/securities/register -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"code":"510300"}'

# 数据同步（异步 job）→ 轮询
JOB=$(curl -s -X POST $BASE/api/v1/data/sync -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"mode":"incremental"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["job_id"])')
curl -s $BASE/api/v1/jobs/$JOB -H "$AUTH"

# 数据状态 / 六项验证
curl -s $BASE/api/v1/data/status -H "$AUTH"
curl -s "$BASE/api/v1/data/verifications?limit=5" -H "$AUTH"

# 组合：列表 / 新建（多头权重须和≈100）
curl -s $BASE/api/v1/portfolios -H "$AUTH"
curl -s -X POST $BASE/api/v1/portfolios -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name":"multi5 复刻","pid":"multi5_copy","positions":[
        {"secid":"510300.SH","weight":20},{"secid":"501018.SH","weight":20},
        {"secid":"518880.SH","weight":20},{"secid":"513100.SH","weight":20},
        {"secid":"511260.SH","weight":20}]}'

# 评估 → 轮询 → 拉产物
JOB=$(curl -s -X POST $BASE/api/v1/portfolios/multi5/review -H "$AUTH" | python3 -c 'import sys,json;print(json.load(sys.stdin)["job_id"])')
RUN=$(curl -s $BASE/api/v1/jobs/$JOB -H "$AUTH" | python3 -c 'import sys,json;print(json.load(sys.stdin)["result"]["run_uid"])')
curl -s "$BASE/api/v1/runs/$RUN/artifacts/report.html" -H "$AUTH" -o report.html

# 回测 / 调仓评估单
curl -s -X POST $BASE/api/v1/portfolios/multi5/backtest -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"rebalance":"monthly","benchmark_secid":"510300.SH"}'
curl -s -X POST $BASE/api/v1/portfolios/multi5/rebalance-plan -H "$AUTH"

# 执行单：生成（同步）→ 确认（需 ACK）→ 登记成交
PLAN=$(curl -s $BASE/api/v1/portfolios/multi5/plans -H "$AUTH" | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["id"])')
EXEC=$(curl -s -X POST $BASE/api/v1/plans/$PLAN/exec-orders -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"capital_cny":1000000}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["exec_uid"])')
curl -s -X POST $BASE/api/v1/exec-orders/$EXEC/confirm -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"ack_discipline":true}'
curl -s -X POST $BASE/api/v1/exec-orders/$EXEC/fill -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"fills":[{"secid":"510300.SH","price":4.432,"shares":-18000}]}'
curl -s $BASE/api/v1/exec-orders/$EXEC -H "$AUTH"          # 详情 + 时间线
```

## 6. 前端 SDK 生成

core 的 OpenAPI 冻结后，前端可生成类型与调用骨架（替换手写的 `web/src/api/types.ts`）：

```bash
# 需 core 已启动
pnpm -C web gen:api     # → web/src/api/generated/schema.d.ts
```

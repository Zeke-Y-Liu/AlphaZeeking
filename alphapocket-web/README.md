# AlphaPocket V4-β · Web 系统

自选股/组合研究工作台：**Vue3 前端 + NestJS BFF + FastAPI 量化服务**（复用 V4-α 计算内核 `applib`）。
覆盖「数据同步 → 六项验证门禁 → 评估报告 → 回测 → 调仓计划 → 执行单 → 人工确认/成交登记」全链路，
响应式一套代码（<768px 单列 + tabbar；≥1024px 三栏工作台）。

## 仓库布局

```
alphapocket-web/
├── core/    FastAPI 量化服务（Python 3.12+，复用 V4-α 计算内核 applib）
├── bff/     NestJS 网关（JWT / 限流 / DTO 裁剪 / 代理）
├── web/     Vue3 + Vite5 + TS + Pinia + UnoCSS + vue-router + axios + echarts
├── deploy/  docker-compose 与基础设施（pg16 / redis7 / minio / core / bff / web）
├── e2e/     Playwright 端到端（T7.1：剧本 A/B + 409/429/401 分支，夹具驱动）
├── docs/    API.md / RUNBOOK.md
└── Makefile · .env.example · README.md
```

端口：web 8080 · bff 8081 · core 8082（详见 `.env.example` 与实施书 §3.2）。

## 快速开始（docker 全栈）

```bash
cp .env.example .env
make up && make seed          # compose 起全栈 + 迁移 + 灌演示数据（demo / cn4 / multi5）
# 浏览器打开 http://localhost:8080
```

### 本地开发（不依赖 docker）

```bash
# core（:8082）
make install-core
PYTHONPATH=/workspace/alphapocket core/.venv/bin/uvicorn app.main:app --port 8082 --app-dir core
curl localhost:8082/healthz    # {"status":"ok",...}

# bff（:8081）
pnpm -C bff install && pnpm -C bff start:dev

# web（:5173，/api 代理至 :8081）
pnpm -C web install && pnpm -C web dev
```

### GitHub Pages（前端静态托管）

`web` 前端可发布到 GitHub Pages：`https://zeke-y-liu.github.io/AlphaZeeking/`。

- **自动部署**：仓库根 `.github/workflows/gh-pages.yml` 在 push `main`（改动
  `alphapocket-web/web/**`）时构建并部署；也可在 Actions 页手动触发。
- **手动部署**：`pnpm -C web exec vite build --base=/AlphaZeeking/` 后将 `web/dist` 内容
  推到 `gh-pages` 分支即可。
- **后端说明**：Pages 仅静态托管，不含 bff/core。页面默认请求同源 `/api`（不可达）；
  在浏览器控制台设置自建后端地址后刷新即可登录（dev/生产镜像为同源 `/api`，无需设置）：

  ```js
  localStorage.setItem('ap.api-base', 'https://your-host/api'); // 指向任一可达 bff；清空恢复默认
  ```

- 子路径部署下路由自动切换为 hash 模式（`BASE_URL` 判断，见 `web/src/router/index.ts`），
  规避静态托管无服务端重写的深链接 404。

## DoD 演练（新环境 10 步走通）

对应实施书 §2.3「可运行判据」；有 docker 时 1–2 步即可，其余在浏览器完成。

```bash
# 1) 环境与全栈
cp .env.example .env
# 2) 启动 + 迁移 + 灌演示数据
make up && make seed
# 3) 打开工作台
open http://localhost:8080          # 登录账号 z / Alpha618
```

4. 登录 → 进入「组合」列表（seed 已含 `cn4`、`multi5`）。
5. 新建组合：`/portfolios/new` 三步向导（基本信息 → 标的与权重 → 确认）→ 提交。
6. 数据页 `/data`：触发同步（bootstrap / incremental），观察 job 进度与六项验证全绿。
7. 组合详情 `/portfolios/{pid}`：点「评估」→ 体检卡五指标 + 引擎A 因子表 + 引擎B 信号卡 + 下一交易日指引。
8. 回测 `/portfolios/{pid}/backtest`：设置区间/再平衡/基准 → 净值双线 + 八指标对比 + 月度收益 + 标的贡献。
9. 调仓 `/portfolios/{pid}/rebalance`：生成 plan → 生成执行单（整手/费用/回款/期末现金）。
10. 执行单：勾选纪律声明 → 确认 → 登记成交（价格>0、100 整数倍校验）→ 审计时间线。
最后本地回归：

```bash
make test && make test-e2e
```

## 常用命令

| 命令 | 说明 |
| --- | --- |
| `make lint` | core(ruff) + bff(eslint) + web(eslint) |
| `make test` | core(pytest) + bff(jest) + web(vitest) |
| `make test-e2e` | Playwright 端到端（剧本 A/B + 409/429/401 分支） |
| `make up / down / logs` | compose 全栈启停 / 日志 |
| `make migrate / seed / psql` | 迁移 / 灌数据 / 进 pg（走 compose/docker） |
| `make migrate-local / seed-local / psql-local` | 同上，本机无 docker、已直跑 PG(127.0.0.1:5432) 时用 |

## 当前验收状态

| 项 | 结果 |
| --- | --- |
| core `pytest` | 163 passed / 1 skipped（跳过项需 `AP_NET_TESTS=1` 外网） |
| bff `jest` | 9 suites / 39 passed |
| web `vitest` | 16 files / 86 passed |
| `vite build` / `nest build` | 通过 |
| core OpenAPI 契约快照 | 20 端点冻结，通过 |
| `make test-e2e` | 7 passed（夹具驱动，不依赖外网/后端） |

E2E 默认经 CDP 复用既有 Chrome（无外网也能跑）：

```bash
google-chrome --headless=new --remote-debugging-port=9222 &
make test-e2e            # 或 AP_E2E_CDP=launch（回退本地 chromium）
```

## 文档

- [docs/API.md](docs/API.md)：端点清单 + 统一错误体 + curl 示例（含执行单全流）。
- [docs/RUNBOOK.md](docs/RUNBOOK.md)：compose 排障、AKShare 熔断处置、workspace 重置、α fixture 刷新、云端映射、已知局限。

## V4-α 计算内核（`alphapocket`）引用方式

- 单一事实源：**不复制** `/workspace/alphapocket/applib`，服务只引用。
- `core/pyproject.toml` 声明 `alphapocket @ file:///workspace/alphapocket`。
  该目录当前是**纯包目录**（无 `pyproject.toml`/`setup.py`），因此：
  - **不要**执行 `pip install -e core`（会因该依赖无法解析而失败，属预期）；
  - 本地开发：`PYTHONPATH=/workspace/alphapocket` 注入（`make test` 如此）；
  - 生产镜像：构建上下文 `/workspace` 中 `COPY alphapocket /opt/alphapocket` 并以 `PYTHONPATH` 注入。

## 构建与部署约定

- **Dockerfile 构建上下文 = `/workspace`**（monorepo 父目录，同时含 `alphapocket/` 与 `alphapocket-web/`）：
  - `core`：`docker build -f alphapocket-web/core/Dockerfile /workspace`；镜像内 `PYTHONPATH=/opt/alphapocket:/app`，暴露 8082。
  - `bff`：`docker build -f alphapocket-web/bff/Dockerfile /workspace`；`PORT=8081`，产物 `dist/main.js`。
  - `web`：`docker build -f alphapocket-web/web/Dockerfile /workspace`；多阶段 node 构建 + nginx，容器内监听 80，`/api` 反代 `http://bff:8081`。
- **compose 路径**：`deploy/docker-compose.dev.yml`；服务名 `pg/redis/minio/core/bff/web`。
- **core 工作区根**：环境变量 `AP_WORKSPACE_ROOT`（默认 `var/ws/default`）。
- **API 前缀**：core 为 `/api/v1`，BFF 对外为 `/api`（`/api/*` → core 原样透传）；`/healthz` 三服务均返回 `{"status":"ok"}`。
- **云端映射**（GA）与已知局限见 [docs/RUNBOOK.md](docs/RUNBOOK.md) §7/§8。

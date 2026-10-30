# alphapocket-core

AlphaPocket V4-β 量化服务（FastAPI）。T0.1 仅提供脚手架：`/healthz` 存活探针。

## 本地开发

```bash
# 创建虚拟环境（Python 3.12，与生产镜像一致）
python3.12 -m venv .venv
.venv/bin/pip install fastapi "uvicorn[standard]" pydantic pydantic-settings pytest httpx ruff

# 运行服务
PYTHONPATH=/workspace/alphapocket .venv/bin/uvicorn app.main:app --port 8082
curl localhost:8082/healthz   # {"status":"ok"}

# 测试 / 静态检查
.venv/bin/pytest -q
.venv/bin/ruff check app tests
```

## 数据库迁移与 seed（T2.1）

Schema 由 Alembic 管理（`migrations/`），连接串取自 `app.core.config.settings.database_url`
（默认 `postgresql+psycopg://ap:ap@localhost:5432/ap`），**不在 alembic.ini 写死**。

```bash
# 有 docker（compose 全栈）
make migrate && make seed && make psql

# 本机无 docker、已直跑 PG(127.0.0.1:5432) → 用 *-local
make migrate-local   # cd core && .venv/bin/alembic upgrade head
make seed-local      # cd core && .venv/bin/python -m app.seed（幂等）
make psql-local      # psql -h 127.0.0.1 -U ap -d ap
```

种子数据：用户 `z/Alpha618`（argon2id；2026-10-31 起替换旧 demo/alphapocket）· 九标的（CN4 + MULTI5）· 组合 `cn4`/`multi5`
及持仓；以自然键 upsert，可重复执行。

## 关于 `alphapocket` 依赖（V4-α 计算内核）

`pyproject.toml` 通过 `alphapocket @ file:///workspace/alphapocket` 单点引用 V4-α 内核（**不复制**）。
当前 `/workspace/alphapocket` 是**纯包目录**（含 `ap` 可执行脚本与 `applib` 包，**无** `pyproject.toml`
/ `setup.py`），因此：

- **不要**执行 `pip install -e .`（会因该依赖无法解析而失败，属预期）；
- **本地开发**：用 `PYTHONPATH=/workspace/alphapocket` 注入 sys.path（`applib` 顶层包可直接 import），
  `make test` 走此方式，不安装该依赖；
- **生产镜像**：在构建上下文 `/workspace` 内 `COPY alphapocket` 到 `/opt/alphapocket`，并以
  `PYTHONPATH=/opt/alphapocket` 注入（见 `Dockerfile`）；
- 生产若需真正的包安装（`pip install -e`），需 T0.3 之后为 α 补齐打包文件（pyproject/setup）——本任务不处理。

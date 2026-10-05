# deploy · 开发全栈编排（T0.2）

`docker-compose.dev.yml` 一键起 AlphaPocket V4-β 六服务（+ 一次性 minio-init）：
**web(Vue3/nginx) → bff(NestJS) → core(FastAPI) → pg / redis / minio**。

## 1. 快速开始

```bash
cd /workspace/alphapocket-web
cp .env.example .env        # 首次必做；compose 的 env_file 指向它
make up                     # = docker compose -f deploy/docker-compose.dev.yml up -d --build
docker compose -f deploy/docker-compose.dev.yml ps   # 期望全部 healthy
```

停止 / 看日志：

```bash
make down
make logs
```

> 构建上下文固定为 `/workspace`（父目录），需同时存在 `alphapocket/` 与 `alphapocket-web/`。
> 单独构建镜像示例：
> ```bash
> docker build -f alphapocket-web/core/Dockerfile -t ap-core /workspace
> docker build -f alphapocket-web/bff/Dockerfile  -t ap-bff  /workspace
> docker build -f alphapocket-web/web/Dockerfile  -t ap-web  /workspace
> ```

## 2. 访问地址

| 服务 | 宿主地址 | 说明 |
| --- | --- | --- |
| web | http://localhost:8080 | 前端入口（nginx 静态托管 + `/api` 反代 bff） |
| bff | http://localhost:8081/healthz | NestJS 网关 |
| core | http://localhost:8082/healthz · http://localhost:8082/docs | FastAPI + OpenAPI |
| MinIO 控制台 | http://localhost:9001 | 账号/密码见 `.env`（默认 minioadmin/minioadmin） |
| MinIO S3 API | http://localhost:9000 | 桶 `ap-artifacts`（minio-init 自动创建） |
| pg | localhost:5432 | 仅为调试映射；容器间用服务名 `pg:5432` |

端口可用 `.env` 的 `WEB_PORT / BFF_PORT / CORE_PORT` 覆盖（compose 内用 `${VAR:-默认}` 插值）。

## 3. 依赖链与运行顺序

```
pg(healthy) ─┐
redis(healthy)├─▶ core(healthy) ─▶ bff(healthy) ─▶ web(healthy)
minio(healthy)┘
minio(healthy) ─▶ minio-init(建桶 ap-artifacts 后退出)
```

所有依赖均使用 `depends_on: condition: service_healthy`，避免启动竞态。

## 4. 排障要点

- **端口被占用**：`docker compose ... ps` 报错时，改 `.env` 里的 `*_PORT`；`5432/9000/9001` 若被本机 PG/其他 MinIO 占用，可临时注释 `deploy/docker-compose.dev.yml` 中对应 `ports:` 映射（容器间仍可互访）。
- **MinIO 建桶竞态**：`minio-init` 已 `depends_on: minio: condition: service_healthy`，正常不会抢跑；若失败，`docker compose ... logs minio-init`，再 `docker compose ... up -d minio-init` 重跑（`mc mb --ignore-existing` 幂等，可安全重试）。
- **容器内 hostname 必须用服务名**：core 连库/缓存/对象存储要用 `pg` / `redis` / `minio`，不能用 `localhost`（compose 已在 `environment` 覆盖根 `.env` 里的 localhost 值）。手动在容器里执行命令时同理。
- **初始化 SQL 不生效**：`/docker-entrypoint-initdb.d/` 只在**空数据卷首次**初始化时执行。改了 `initdb/*.sql` 需 `make down && docker volume rm alphapocket-dev_pg_data && make up`。
- **healthcheck 依赖工具**：core 镜像装了 curl；bff/web 用 alpine busybox `wget --spider`；minio 用其官方存活端点 `/minio/health/live`。若 `minio` 因镜像内缺 curl 探活失败，可将该 healthcheck 换为 `mc ready local` 或改用 `CMD-SHELL` + `/bin/sh` 探测（视镜像版本而定）。
- **改 MinIO 凭据**：`.env` 的 `AP_MINIO_ACCESS_KEY/SECRET_KEY` 会被 compose 插值到 minio/core/minio-init 三处，保持一致；改后 `make down && docker volume rm alphapocket-dev_minio_data && make up`（凭据变更需重建数据卷）。

## 5. 与 T0.1 的接口核对

- **nginx 反代（`web/nginx.conf`）**：T0.1 已正确实现，无需改动——SPA 回退 `try_files $uri $uri/ /index.html`；`location /api/ { proxy_pass http://bff:8081; }`（`proxy_pass` 无尾斜杠，**保持 `/api` 路径前缀**原样透传）。
- **Makefile**：`up/down/logs/psql/migrate/seed` 均已指向 `deploy/docker-compose.dev.yml`，且服务名假设为 `pg`、`core`，与本 compose 一致，**无需修改**。

## 6. 已知限制（T0.2 范围外，供后续任务处理）

- 构建上下文为 `/workspace` 时，Docker 仅读取**上下文根**的 `.dockerignore`；`bff/.dockerignore`、`web/.dockerignore` 在此场景下不生效，`COPY alphapocket-web/bff/ ./` 会把宿主 `node_modules/`、`dist/` 一并带入构建层。功能上可构建，但镜像层偏大、可能带入陈旧产物。建议后续在 `/workspace/.dockerignore` 统一排除（属 T0.1/T0.3 范围，本任务未改动）。
- 应用层未实现（迁移 `make migrate`、`make seed` 依赖 T2.1 交付的 alembic / `app.seed`）；本 compose 只保证基础设施与三服务可用。

# syntax=docker/dockerfile:1
#
# core 服务镜像（FastAPI）
# 构建上下文 = /workspace（monorepo 父目录，需同时含 alphapocket/ 与 alphapocket-web/）
#   docker build -f alphapocket-web/core/Dockerfile -t ap-core /workspace
# T0.2 的 compose 使用：build.context=/workspace, build.dockerfile=alphapocket-web/core/Dockerfile

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

# 1) V4-α 计算内核：单点引用，构建期从上下文挂载源码（不复制进业务仓库）
COPY alphapocket /opt/alphapocket

# 2) core 服务代码
COPY alphapocket-web/core/pyproject.toml ./pyproject.toml
COPY alphapocket-web/core/app ./app
COPY alphapocket-web/core/tests ./tests

# 3) 安装运行依赖（此处显式列白名单，避免解析 alphapocket @ file:... 失败）
RUN pip install \
        "fastapi>=0.115" "uvicorn[standard]>=0.30" "pydantic>=2.7" "pydantic-settings>=2.3" \
        "sqlalchemy>=2.0" "alembic>=1.13" "psycopg[binary]>=3.2" "redis>=5.0" "minio>=7.2" \
        "httpx>=0.27" "python-jose[cryptography]>=3.3" "passlib[argon2]>=1.7" \
        "pytest>=8.0" "ruff>=0.6"

# applib 通过 sys.path 注入（与本地开发一致）
ENV PYTHONPATH=/opt/alphapocket:/app

EXPOSE 8082

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=5 \
    CMD curl -sf http://localhost:8082/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8082"]

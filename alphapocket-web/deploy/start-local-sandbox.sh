#!/usr/bin/env bash
# 在无 docker 的沙箱内本地拉起 AlphaPocket 全栈：pg / redis / minio / core / bff / web
# 场景：沙箱每次重置后（apt 组件、/usr/local/bin、PG 数据都会丢失，仅 /workspace 持久化）一键恢复。
# 用法：bash deploy/start-local-sandbox.sh
set -uo pipefail

ROOT="/workspace/alphapocket-web"
LOG="/tmp/alphapocket-logs"
mkdir -p "$LOG"
export DEBIAN_FRONTEND=noninteractive
export PATH="/usr/lib/postgresql/16/bin:$PATH"

say() { echo "[start] $*"; }
port_up() { ss -ltn 2>/dev/null | grep -q ":$1 "; }

# ---------- 0) 系统依赖（缺失才装）----------
if ! command -v psql >/dev/null 2>&1 || ! command -v redis-server >/dev/null 2>&1; then
  say "apt: 安装 postgresql / redis-server / curl"
  apt-get update -qq && apt-get install -y -qq --no-install-recommends postgresql redis-server curl ca-certificates
fi

# ---------- 1) PostgreSQL ----------
say "启动 PostgreSQL"
pg_ctlcluster 16 main start 2>/dev/null || true
for _ in $(seq 1 30); do pg_isready -h 127.0.0.1 -p 5432 -q && break; sleep 1; done
su postgres -c "psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='ap'\"" | grep -q 1 \
  || su postgres -c "psql -c \"CREATE ROLE ap LOGIN PASSWORD 'ap' SUPERUSER;\"" >/dev/null
su postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='ap'\"" | grep -q 1 \
  || su postgres -c "createdb -O ap ap"
su postgres -c "psql -q -d ap -c 'CREATE EXTENSION IF NOT EXISTS pg_trgm;' -c 'CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\";'" >/dev/null
say "PG 就绪 ap@127.0.0.1:5432/ap"

# ---------- 2) Redis ----------
say "启动 Redis"
pgrep -x redis-server >/dev/null 2>&1 || redis-server --daemonize yes --bind 127.0.0.1 --port 6379 --dir /var/lib/redis
redis-cli -h 127.0.0.1 -p 6379 ping >/dev/null && say "Redis 就绪 127.0.0.1:6379"

# ---------- 3) MinIO（社区版 AGPLv3）----------
# 注意：官方 dl.min.io 现仅提供商业版 AIStor（无 license 会拒绝所有 S3 操作），故用中国镜像的社区版。
if [ ! -x /usr/local/bin/minio ]; then
  say "下载 MinIO 社区版"
  curl -fsSL --retry 3 -o /usr/local/bin/minio https://dl.minio.org.cn/server/minio/release/linux-amd64/minio \
    && chmod +x /usr/local/bin/minio
fi
if ! curl -sf -m 2 -o /dev/null http://127.0.0.1:9100/minio/health/live; then
  say "启动 MinIO（tmpfs + mount namespace，规避 virtiofs 对 O_DIRECT 的对齐限制）"
  mkdir -p /mnt/mio
  setsid nohup unshare -Urm sh -c \
    'mount -t tmpfs -o size=2g tmpfs /mnt/mio && exec env MINIO_ROOT_USER=minioadmin MINIO_ROOT_PASSWORD=minioadmin /usr/local/bin/minio server /mnt/mio --address 127.0.0.1:9100 --console-address 127.0.0.1:9101' \
    >"$LOG/minio.log" 2>&1 &
  for _ in $(seq 1 30); do curl -sf -m 2 -o /dev/null http://127.0.0.1:9100/minio/health/live && break; sleep 1; done
fi
curl -sf -m 3 -o /dev/null http://127.0.0.1:9100/minio/health/live && say "MinIO 就绪 127.0.0.1:9100（控制台 9101）"

# core 的 /healthz 依赖探测在启动时缓存一次；若 core 先于 MinIO 启动，缓存会一直是
# degraded —— 此处检测到过期缓存则重启 core（下方第 6 步会自动重新拉起）。
if port_up 8082 && curl -s -m 3 http://127.0.0.1:8082/healthz | grep -q '"minio":{"ok":false'; then
  say "core 的 MinIO 探测缓存已过期，重启 core"
  pkill -f 'uvicorn app.main:app' 2>/dev/null || true
  sleep 1
fi

# ---------- 4) 迁移 + 种子（幂等）----------
say "alembic upgrade head"
make -C "$ROOT" migrate-local >/dev/null || { echo "迁移失败"; exit 1; }
say "seed"
make -C "$ROOT" seed-local

# ---------- 5) 建桶（幂等）----------
"$ROOT/core/.venv/bin/python" - <<'PY'
from minio import Minio
c = Minio("127.0.0.1:9100", access_key="minioadmin", secret_key="minioadmin", secure=False)
if not c.bucket_exists("ap-artifacts"):
    c.make_bucket("ap-artifacts")
print("[start] 桶 ap-artifacts 就绪")
PY

# ---------- 6) 应用三件套 ----------

if ! port_up 8082; then
  say "启动 core :8082"
  setsid nohup env PYTHONPATH=/workspace/alphapocket "$ROOT/core/.venv/bin/uvicorn" \
    app.main:app --host 0.0.0.0 --port 8082 --app-dir "$ROOT/core" >"$LOG/core.log" 2>&1 &
fi
if ! port_up 8081; then
  say "启动 bff :8081"
  setsid nohup env PORT=8081 CORE_URL=http://localhost:8082 \
    pnpm -C "$ROOT/bff" start:dev >"$LOG/bff.log" 2>&1 &
fi
if ! port_up 5173; then
  say "启动 web :5173"
  setsid nohup pnpm -C "$ROOT/web" dev >"$LOG/web.log" 2>&1 &
fi

# ---------- 7) 健康检查 ----------
say "等待服务就绪…"
for _ in $(seq 1 40); do
  curl -sf -m 2 -o /dev/null http://127.0.0.1:8082/healthz \
    && curl -sf -m 2 -o /dev/null http://127.0.0.1:8081/healthz \
    && curl -sf -m 2 -o /dev/null http://127.0.0.1:5173/ && break
  sleep 1
done

echo
echo "===== 状态 ====="
echo -n "core  :8082  "; curl -s -m 5 http://127.0.0.1:8082/healthz || echo "DOWN"; echo
echo -n "bff   :8081  "; curl -s -m 5 http://127.0.0.1:8081/healthz || echo "DOWN"; echo
echo -n "web   :5173  "; curl -s -m 5 -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1:5173/ || echo "DOWN"
echo "日志目录：$LOG"

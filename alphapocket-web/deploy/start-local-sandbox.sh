#!/usr/bin/env bash
# 本地沙箱一键启动 · AP-Checkpoint 三路编排版（T4.1）
#
#   FAST-LOCAL  本地 L0 缓存命中 → 秒级恢复依赖（暖启动，~2min 全栈）
#   FAST-PULL   本地无缓存 → 从 GitHub Release 拉 L0（冷启动，~6-8min）
#   SLOW        Release 不可达 → 原全量安装路径（apt/pip/pnpm，~12-16min，尾部自动构建镜像）
#
# 用法：bash deploy/start-local-sandbox.sh [--slow]  （--slow 强制走原全量路径）
set -uo pipefail

ROOT="/workspace/alphapocket-web"
LOG="/tmp/alphapocket-logs"
CK_SCRIPTS="$ROOT/deploy/ck"
mkdir -p "$LOG"
export DEBIAN_FRONTEND=noninteractive
export PATH="/usr/lib/postgresql/16/bin:$PATH"

T0=$(date +%s)
mark() { echo "$(( $(date +%s) - T0 ))s $1" >> "$LOG/start-phases.txt"; }
say() { echo "[start $(printf '%3ss' $(( $(date +%s) - T0 )))] $*"; }
port_up() { ss -ltn 2>/dev/null | grep -q ":$1 "; }
FORCE_SLOW="${1:-}" ; [ "${1:-}" = "--slow" ] && FORCE_SLOW=slow

: > "$LOG/start-phases.txt"

# ---------------- 公共：三件套启动（幂等）----------------
start_apps() {
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
  for _ in $(seq 1 60); do
    curl -sf -m 2 -o /dev/null http://127.0.0.1:8082/healthz \
      && curl -sf -m 2 -o /dev/null http://127.0.0.1:8081/healthz \
      && curl -sf -m 2 -o /dev/null http://127.0.0.1:5173/ && break
    sleep 2
  done
  mark "三件套就绪"
}

# ---------------- 公共：健康门禁 + 报告 ----------------
gate_and_report() {
  local mode="$1"
  say "健康门禁 …"
  local ok=1
  curl -sf -m 4 http://127.0.0.1:8082/healthz | grep -q '"status":"ok"' || ok=0
  curl -sf -m 4 http://127.0.0.1:8081/healthz | grep -q '"core":"ok"' || ok=0
  curl -sf -m 4 -o /dev/null http://127.0.0.1:5173/ || ok=0
  [ "$ok" -eq 1 ] || { say "门禁未过（见 /tmp/alphapocket-logs/）"; exit 1; }

  # 数据门禁（软性：as_of 就绪即绿，不因外网同步失败而判定启动失败）
  local asof
  asof=$(curl -s -m 6 localhost:8082/api/v1/data/status \
    -H "Authorization: Bearer $(curl -s -m 6 -X POST localhost:8082/api/v1/auth/login \
      -H 'Content-Type: application/json' -d '{"username":"demo","password":"alphapocket"}' \
      | "$ROOT/core/.venv/bin/python" -c "import sys,json;print(json.load(sys.stdin).get('access',''))" 2>/dev/null)" 2>/dev/null \
    | "$ROOT/core/.venv/bin/python" -c "import sys,json;print(json.load(sys.stdin).get('as_of'))" 2>/dev/null || echo "")

  echo
  echo "===== 启动报告 ====="
  echo "路径   : $mode"
  echo "总耗时 : $(( $(date +%s) - T0 ))s"
  echo "阶段   : $(cat "$LOG/start-phases.txt" | tr '\n' ' | ')"
  echo "core   : $(curl -s -m 4 http://127.0.0.1:8082/healthz | head -c 120)"
  echo "bff    : $(curl -s -m 4 http://127.0.0.1:8081/healthz)"
  echo "web    : HTTP $(curl -s -m 4 -o /dev/null -w '%{http_code}' http://127.0.0.1:5173/)"
  echo "数据   : as_of=${asof:-同步中}"
}

# ---------------- SLOW：原全量安装路径（兜底）----------------
slow_path() {
  say "路径：SLOW（全量安装；完成后将自动构建 checkpoint 供下次加速）"
  mark "apt 开始"
  if ! command -v psql >/dev/null 2>&1 || ! command -v redis-server >/dev/null 2>&1; then
    say "apt: 安装 postgresql / redis-server / curl"
    apt-get update -qq && apt-get install -y -qq --no-install-recommends postgresql redis-server curl ca-certificates
  fi
  mark "apt 完成"
  pg_ctlcluster 16 main start 2>/dev/null || true
  for _ in $(seq 1 30); do pg_isready -h 127.0.0.1 -p 5432 -q && break; sleep 1; done
  su postgres -c "psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='ap'\"" | grep -q 1 \
    || su postgres -c "psql -c \"CREATE ROLE ap LOGIN PASSWORD 'ap' SUPERUSER;\"" >/dev/null
  su postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='ap'\"" | grep -q 1 \
    || su postgres -c "createdb -O ap ap" >/dev/null
  su postgres -c "psql -q -d ap -c 'CREATE EXTENSION IF NOT EXISTS pg_trgm;' -c 'CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\";'" >/dev/null
  mark "PG 就绪"

  pgrep -x redis-server >/dev/null 2>&1 || redis-server --daemonize yes --bind 127.0.0.1 --port 6379 --dir /var/lib/redis
  redis-cli -h 127.0.0.1 -p 6379 ping >/dev/null && say "Redis 就绪"

  if [ ! -x /usr/local/bin/minio ]; then
    say "下载 MinIO 社区版"
    curl -fsSL --retry 3 -o /usr/local/bin/minio https://dl.minio.org.cn/server/minio/release/linux-amd64/minio \
      && chmod +x /usr/local/bin/minio
  fi
  if ! curl -sf -m 2 -o /dev/null http://127.0.0.1:9100/minio/health/live; then
    mkdir -p /mnt/mio
    setsid nohup unshare -Urm sh -c \
      'mount -t tmpfs -o size=2g tmpfs /mnt/mio && exec env MINIO_ROOT_USER=minioadmin MINIO_ROOT_PASSWORD=minioadmin /usr/local/bin/minio server /mnt/mio --address 127.0.0.1:9100 --console-address 127.0.0.1:9101' \
      >"$LOG/minio.log" 2>&1 &
    for _ in $(seq 1 30); do curl -sf -m 2 -o /dev/null http://127.0.0.1:9100/minio/health/live && break; sleep 1; done
  fi
  say "MinIO 就绪 127.0.0.1:9100"
  mark "infra 就绪"

  # venv / node_modules（slow 路径自己装）
  if [ ! -x "$ROOT/core/.venv/bin/python" ]; then
    say "python3.12 -m venv + pip（重）…"
    python3.12 -m venv "$ROOT/core/.venv"
    "$ROOT/core/.venv/bin/pip" install -U pip -q
    "$ROOT/core/.venv/bin/pip" install -q fastapi "uvicorn[standard]" pydantic pydantic-settings \
      sqlalchemy alembic "psycopg[binary]" redis minio httpx "python-jose[cryptography]" \
      "passlib[argon2]" pytest pytest-asyncio ruff pandas duckdb pyyaml requests numpy akshare
  fi
  for d in bff web e2e; do
    [ -d "$ROOT/$d/node_modules" ] || pnpm -C "$ROOT/$d" install >/dev/null 2>&1
  done
  mark "依赖就绪"

  printf '# 本地沙箱运行配置。9000 被系统服务占用，MinIO 固定 9100。\nAP_MINIO_ENDPOINT=localhost:9100\n' > "$ROOT/.env"
  say "alembic upgrade head"
  make -C "$ROOT" migrate-local >/dev/null || { echo "迁移失败"; exit 1; }
  say "seed"
  make -C "$ROOT" seed-local >/dev/null
  "$ROOT/core/.venv/bin/python" - <<'PY' >/dev/null 2>&1
from minio import Minio
c = Minio("127.0.0.1:9100", access_key="minioadmin", secret_key="minioadmin", secure=False)
if not c.bucket_exists("ap-artifacts"):
    c.make_bucket("ap-artifacts")
PY

  # 基线数据就位（git 内 remote/repo，避免 bootstrap 从零拉取）
  mkdir -p /workspace/alphapocket-web/var/ws/default/remote
  rm -rf /workspace/alphapocket-web/var/ws/default/remote/repo
  cp -r /workspace/alphapocket/remote/repo /workspace/alphapocket-web/var/ws/default/remote/repo 2>/dev/null || true

  start_apps
  gate_and_report "SLOW"

  # 尾钩：构建并推送 L0/L1（下次即快；失败不影响本次启动）
  say "尾钩：构建 checkpoint …"
  bash "$CK_SCRIPTS/build-l0-venv.sh" >/dev/null 2>&1 \
    && bash "$CK_SCRIPTS/build-l0-node.sh" >/dev/null 2>&1 \
    && bash "$CK_SCRIPTS/build-l0-infra.sh" >/dev/null 2>&1 \
    && bash "$CK_SCRIPTS/build-l0.sh" >/dev/null 2>&1 \
    && say "L0 已构建（本地缓存）；push 请跑 make ck-push" \
    || say "L0 构建失败（不影响本次启动；可手动重跑）"
  bash "$CK_SCRIPTS/build-l1.sh" >/dev/null 2>&1 && say "L1 状态快照已推送 ap-state" \
    || say "L1 快照构建失败（不影响本次启动）"
}

# ---------------- FAST：checkpoint 恢复路径 ----------------
fast_path() {
  local pulled=0
  # cache 实际位置在仓库根 /workspace/.ck（见 ck-env.sh CK_DIR），勿用 $ROOT/.ck
  if [ -f "/workspace/.ck/cache/l0/manifest-L0.json" ]; then
    say "路径：FAST-LOCAL（本地 L0 缓存命中）"
    MODE="FAST-LOCAL"
  else
    say "本地无 L0 缓存 → FAST-PULL（从 Release 拉取）"
    MODE="FAST-PULL"
    if ! bash "$CK_SCRIPTS/pull-l0.sh" "${CK_WITH_CHROMIUM:-}"; then
      say "FAST-PULL 失败 → 降级 SLOW"
      slow_path; return
    fi
    pulled=1
  fi
  mark "路径决策"

  bash "$CK_SCRIPTS/restore-l0.sh" "${CK_WITH_CHROMIUM:-}" \
    || { say "restore-l0 失败 → 降级 SLOW"; slow_path; return; }
  mark "L0 恢复"
  bash "$CK_SCRIPTS/start-infra.sh" || { say "start-infra 失败 → 降级 SLOW"; slow_path; return; }
  mark "infra 就绪"

  # .env 兜底（core 依赖 MinIO 9100）
  [ -f "$ROOT/.env" ] || printf '# 本地沙箱运行配置。9000 被系统服务占用，MinIO 固定 9100。\nAP_MINIO_ENDPOINT=localhost:9100\n' > "$ROOT/.env"

  bash "$CK_SCRIPTS/restore-l1.sh" \
    || { say "L1 无快照 → alembic+seed 兜底"; make -C "$ROOT" migrate-local >/dev/null; make -C "$ROOT" seed-local >/dev/null; }
  mark "L1 恢复"

  start_apps

  # 数据：git 基线 → incremental（软性：失败不阻断启动）
  bash "$CK_SCRIPTS/warm-repo.sh" >/dev/null 2>&1 \
    && mark "数据增量完成" || { say "数据增量未完成（可稍后手动跑 warm-repo.sh 或 bootstrap）"; mark "数据增量未完成"; }

  gate_and_report "$MODE"

  # 尾钩：L1 状态快照常新（体量小，每次启动后更新）
  bash "$CK_SCRIPTS/build-l1.sh" >/dev/null 2>&1 && say "L1 快照已更新（ap-state）" || true
  [ "$pulled" -eq 1 ] && say "提示：E2E 需要 chromium → make ck-pull CK_WITH_CHROMIUM=1 或跑 playwright install"
}

# ---------------- 入口 ----------------
if [ -n "$FORCE_SLOW" ]; then
  slow_path
else
  fast_path
fi

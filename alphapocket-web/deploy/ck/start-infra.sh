#!/usr/bin/env bash
# T2.2 — 零 apt 基础设施：embedded PG16 + redis + minio（全部来自 /workspace/ap-bin）
# 前置：restore-l0.sh 已恢复 ap-bin。幂等：端口已监听则跳过启动；role/库建立始终执行。
# PG 以 root 运行被禁止 → 自动确保 postgres 系统用户并以该用户运行。
set -uo pipefail
CK_SCRIPTS="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=ck-env.sh
source "$CK_SCRIPTS/ck-env.sh"

port_up() { ss -ltn 2>/dev/null | grep -q ":$1 "; }

# ---------- PG16 ----------
if ! port_up 5432; then
  id postgres >/dev/null 2>&1 || useradd -r -s /bin/bash postgres
  PGDATA=$AP_DATA/pg
  PGBIN=$AP_BIN/pg16/lib/bin
  mkdir -p "$PGDATA" && chown -R postgres:postgres "$PGDATA"
  # Debian PG 编译期硬编码 /usr/{lib,share}/postgresql/16——建符号链接把
  # embedded 布局接回原路径（无需 apt，仅 ln），initdb/postgres 即按原生方式工作
  mkdir -p /usr/lib/postgresql /usr/share/postgresql
  ln -sfn "$AP_BIN/pg16/lib" /usr/lib/postgresql/16
  ln -sfn "$AP_BIN/pg16/share/postgresql" /usr/share/postgresql/16
  if [ ! -f "$PGDATA/PG_VERSION" ]; then
    say "initdb → $PGDATA"
    rm -rf "$PGDATA" && mkdir -p "$PGDATA" && chown postgres:postgres "$PGDATA"
    # locale 优先 en_US.UTF-8（与 apt 初始集群一致），失败降级 C.UTF-8
    su postgres -c "locale -a 2>/dev/null | grep -qi '^en_US.utf8$' && LOCALE=en_US.UTF-8 || LOCALE=C.UTF-8; \
      $PGBIN/initdb -D $PGDATA --locale=\$LOCALE --encoding=UTF8" >/dev/null 2>&1 \
      || { su postgres -c "$PGBIN/initdb -D $PGDATA --locale=C.UTF-8 --encoding=UTF8" 2>&1 | tail -3; \
           say "ERR: initdb 失败"; exit 1; }
  fi
  say "启动 postgres :5432"
  # unix socket 也放 PGDATA（/var/run/postgresql 随 apt 丢失，不依赖系统目录）
  su postgres -c "$PGBIN/pg_ctl -D $PGDATA -l $PGDATA/pg.log -o '-p 5432 -c unix_socket_directories=$PGDATA' start" >/dev/null
  for _ in $(seq 1 20); do
    port_up 5432 && break; sleep 1
  done
  port_up 5432 || { say "ERR: postgres 未监听"; exit 1; }
else
  say "PG :5432 已在监听，跳过启动"
fi

# role/库/扩展（无论 PG 是否刚启动都执行；全部幂等；走 PGDATA 内 unix socket + peer 认证）
PSQL_SOCK="$AP_BIN/pg16/lib/bin/psql -h $AP_DATA/pg -U postgres"
su postgres -c "$PSQL_SOCK -tAc \"SELECT 1 FROM pg_roles WHERE rolname='ap'\"" 2>/dev/null | grep -q 1 \
  || su postgres -c "$PSQL_SOCK -c \"CREATE ROLE ap LOGIN PASSWORD 'ap' SUPERUSER\"" >/dev/null
su postgres -c "$PSQL_SOCK -tAc \"SELECT 1 FROM pg_database WHERE datname='ap'\"" 2>/dev/null | grep -q 1 \
  || su postgres -c "$AP_BIN/pg16/lib/bin/createdb -h $AP_DATA/pg -U postgres -O ap ap" >/dev/null
PGPASSWORD=ap "$AP_BIN/pg16/lib/bin/psql" -h 127.0.0.1 -U ap -d ap -q \
  -c 'CREATE EXTENSION IF NOT EXISTS pg_trgm' \
  -c 'CREATE EXTENSION IF NOT EXISTS "uuid-ossp"' >/dev/null 2>&1 || true
say "PG 就绪 ap@127.0.0.1:5432/ap（embedded）"

# ---------- Redis ----------
if ! port_up 6379; then
  say "启动 redis :6379（LD_LIBRARY_PATH=$AP_BIN/libs）"
  LD_LIBRARY_PATH="$AP_BIN/libs${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
    "$AP_BIN/redis-server" --daemonize yes --bind 127.0.0.1 --port 6379 --dir "$AP_DATA" >/dev/null
  port_up 6379 || { say "ERR: redis 未监听"; exit 1; }
  say "Redis 就绪"
else
  say "Redis :6379 已在监听，跳过"
fi

# ---------- MinIO（9100，避开被系统占用的 9000）----------
if ! port_up 9100; then
  say "启动 minio :9100（数据 $AP_DATA/mio）"
  mkdir -p "$AP_DATA/mio"
  setsid nohup "$AP_BIN/minio" server "$AP_DATA/mio" --address 127.0.0.1:9100 \
    --console-address 127.0.0.1:9101 >"$AP_DATA/minio.log" 2>&1 < /dev/null &
  for _ in $(seq 1 20); do
    curl -sf -m 2 -o /dev/null http://127.0.0.1:9100/minio/health/live && break; sleep 1
  done
  curl -sf -m 2 -o /dev/null http://127.0.0.1:9100/minio/health/live \
    || { say "ERR: minio health 未就绪"; exit 1; }
  say "MinIO 就绪"
else
  say "MinIO :9100 已在监听，跳过"
fi
curl -sf -m 2 -o /dev/null http://127.0.0.1:9100/minio/health/live || { say "ERR: minio 不可达"; exit 1; }
"$CK_ROOT/core/.venv/bin/python" - <<'PY' >/dev/null 2>&1 || true
from minio import Minio
c = Minio("127.0.0.1:9100", access_key="minioadmin", secret_key="minioadmin", secure=False)
if not c.bucket_exists("ap-artifacts"):
    c.make_bucket("ap-artifacts")
PY
say "===== start-infra 完成（PG/Redis/MinIO 全就绪）====="

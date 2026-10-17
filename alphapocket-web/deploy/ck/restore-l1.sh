#!/usr/bin/env bash
# T3.2 — 从 ap-state 数据分支恢复 L1（PG 数据 + MinIO artifacts）
# 前置：restore-l0（venv/alembic）+ start-infra（embedded PG 已起、role/库已建）
set -uo pipefail
CK_SCRIPTS="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=ck-env.sh
source "$CK_SCRIPTS/ck-env.sh"

STAGE=$CK_DIR/l1-restore
cd "$CK_REPO"

say "拉取 ap-state …"
# 显式 refspec + --force：ap-state 为整体替换语义（孤儿分支），非快进属常态
git fetch --force -q origin ap-state:refs/remotes/origin/ap-state 2>/dev/null \
  || { say "ERR: ap-state 分支不可达（无状态快照，走 alembic+seed 慢路径）"; exit 3; }
mkdir -p "$STAGE"
git show origin/ap-state:ap.dump > "$STAGE/ap.dump" || { say "ERR: ap.dump 不在分支中"; exit 1; }
git show origin/ap-state:state-manifest.json > "$STAGE/state-manifest.json" 2>/dev/null || true
[ -f "$STAGE/state-manifest.json" ] && say "状态快照：$(python3.12 -c "import json;print(json.load(open('$STAGE/state-manifest.json'))['state_key'])")"

say "pg_restore → ap（--clean --if-exists 幂等）…"
if ! PGPASSWORD=ap "$AP_BIN/pg16/lib/bin/pg_restore" -h 127.0.0.1 -U ap -d ap \
  --clean --if-exists --no-owner "$STAGE/ap.dump" 2>"$STAGE/restore.err"; then
  # --clean 对不存在的对象会报「does not exist, skipping」无关错误，只拦致命项
  if grep -qE "FATAL|connection" "$STAGE/restore.err"; then
    say "ERR: pg_restore 连接级失败：$(tail -2 "$STAGE/restore.err")"
    exit 1
  fi
  say "pg_restore 带非致命告警完成（详见 $STAGE/restore.err）"
fi

# ---------- 数据校验 ----------
ROWS=$(PGPASSWORD=ap "$AP_BIN/pg16/lib/bin/psql" -h 127.0.0.1 -U ap -d ap -tAc \
  "SELECT (SELECT count(*) FROM users)||'/'||(SELECT count(*) FROM securities)||'/'||(SELECT count(*) FROM portfolios)" 2>/dev/null)
say "PG 数据校验 users/securities/portfolios = $ROWS"
[ "$ROWS" = "1/15/3" ] || say "WARN: 行数与 seed 基线不一致（$ROWS）——如异常可改走 alembic+seed"

# ---------- alembic 与代码迁移对齐 ----------
say "alembic 迁移对齐 …"
( cd "$CK_ROOT/core" && PYTHONPATH=/workspace/alphapocket "$CK_ROOT/core/.venv/bin/alembic" upgrade head ) >/dev/null 2>&1 \
  && say "alembic: head 对齐" || say "WARN: alembic upgrade 失败（dump schema 可能新于代码）"

# ---------- MinIO artifacts（可选）----------
if git cat-file -e origin/ap-state:minio-artifacts.tar.gz 2>/dev/null; then
  git show origin/ap-state:minio-artifacts.tar.gz > "$STAGE/minio-artifacts.tar.gz"
  say "恢复 minio artifacts …"
  tar -C "$AP_DATA/mio" -xzf "$STAGE/minio-artifacts.tar.gz" 2>/dev/null || \
    say "WARN: minio 数据解包异常（可重建）"
else
  say "无 minio artifacts 快照（桶保持空，产物可重算）"
fi
say "===== L1 恢复完成 ====="

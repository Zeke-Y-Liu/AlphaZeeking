#!/usr/bin/env bash
# T3.1 — L1 状态快照：PG dump + MinIO artifacts → git 孤儿数据分支 ap-state
# 零检出方案：hash-object / mktree / commit-tree，不打断当前工作区。
# 产出分支：ap-state（每次构建整体替换，不叠历史）
set -uo pipefail
CK_SCRIPTS="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=ck-env.sh
source "$CK_SCRIPTS/ck-env.sh"

STAGE=$CK_DIR/l1-stage
mkdir -p "$STAGE"
cd "$CK_REPO"

# ---------- ① PG dump ----------
# pg_dump 优先用 embedded（ap-bin），apt 版仅作兜底——系统盘重置后 /usr/bin 版会消失
PG_DUMP="$AP_BIN/pg16/lib/bin/pg_dump"
[ -x "$PG_DUMP" ] || PG_DUMP=/usr/bin/pg_dump
say "pg_dump ap → ap.dump …"
if ! PGPASSWORD=ap "$PG_DUMP" -Fc -h 127.0.0.1 -U ap ap > "$STAGE/ap.dump" 2>"$STAGE/dump.err"; then
  say "ERR: pg_dump 失败：$(tail -2 "$STAGE/dump.err")"
  exit 1
fi
say "OK: ap.dump（$(du -sh "$STAGE/ap.dump" | awk '{print $1}')）"

# ---------- ② MinIO artifacts（桶 ap-artifacts → tar.gz）----------
say "拉取 MinIO 桶 ap-artifacts …"
"$CK_ROOT/core/.venv/bin/python" - "$STAGE" <<'PY'
import os, sys
from minio import Minio
stage = sys.argv[1]
c = Minio("localhost:9100", access_key="minioadmin", secret_key="minioadmin", secure=False)
n = 0
for obj in c.list_objects("ap-artifacts", recursive=True):
    dst = f"{stage}/ap-artifacts/{obj.object_name}"
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    c.fget_object("ap-artifacts", obj.object_name, dst)
    n += 1
print(f"[ck] 拉取 {n} 个对象")
PY
if [ -d "$STAGE/ap-artifacts" ]; then
  tar -C "$STAGE" -czf "$STAGE/minio-artifacts.tar.gz" ap-artifacts
  say "OK: minio-artifacts.tar.gz（$(du -sh "$STAGE/minio-artifacts.tar.gz" | awk '{print $1}')）"
fi

# ---------- ③ state manifest ----------
AS_OF=$(curl -s -m 5 localhost:8082/api/v1/data/status -H "Authorization: Bearer $(cat /tmp/ap_token 2>/dev/null)" \
  | "$CK_ROOT/core/.venv/bin/python" -c "import sys,json;print(json.load(sys.stdin).get('as_of'))" 2>/dev/null || echo unknown)
HEAD=$(git rev-parse --short HEAD)
cat > "$STAGE/state-manifest.json" <<EOF
{
  "schema": "ap-checkpoint/l1@v1",
  "state_key": "$HEAD+$AS_OF",
  "git_head": "$HEAD",
  "as_of": "$AS_OF",
  "built_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "contains": ["ap.dump", "minio-artifacts.tar.gz"]
}
EOF
say "state_key=$HEAD+$AS_OF"

# ---------- ④ 孤儿分支（零检出）----------
declare -a BLOBS=()
add_blob() {  # add_blob <file> <name>
  local sha
  sha=$(git hash-object -w "$1") || return 1
  echo "100644 blob $sha	$2"
}
TREE_INPUT=""
TREE_INPUT+="$(add_blob "$STAGE/ap.dump" ap.dump)"$'\n'
[ -f "$STAGE/minio-artifacts.tar.gz" ] && TREE_INPUT+="$(add_blob "$STAGE/minio-artifacts.tar.gz" minio-artifacts.tar.gz)"$'\n'
TREE_INPUT+="$(add_blob "$STAGE/state-manifest.json" state-manifest.json)"
TREE=$(printf '%s\n' "$TREE_INPUT" | git mktree) || { say "ERR: mktree 失败"; exit 1; }
COMMIT=$(git -c user.name="Zeke-Y-Liu" -c user.email="Zeke-Y-Liu@users.noreply.github.com" \
  commit-tree "$TREE" -m "state: as_of=$AS_OF head=$HEAD")
git push origin --force "$COMMIT:refs/heads/ap-state" 2>&1 | tail -1
say "OK: ap-state → $COMMIT"

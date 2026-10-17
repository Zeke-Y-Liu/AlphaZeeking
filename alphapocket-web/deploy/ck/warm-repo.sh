#!/usr/bin/env bash
# T3.3 — 基线数据就位 + 增量同步（非 bootstrap）
# git 内 remote/repo（重置存活）→ var/ws/default/remote/repo → core incremental
# 前置：三件套已起（core :8082）、restore-l1 完成（seed/业务数据在）
set -uo pipefail
CK_SCRIPTS="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=ck-env.sh
source "$CK_SCRIPTS/ck-env.sh"

WS=$CK_ROOT/var/ws/default
mkdir -p "$WS/remote"

say "git 内基线数据 → workspace …"
rm -rf "$WS/remote/repo"
cp -r "$CK_REPO/alphapocket/remote/repo" "$WS/remote/repo"
say "OK（$(du -sh "$WS/remote/repo" | awk '{print $1}')）"

# .env 兜底（MinIO 端口；重置后常丢）
if [ ! -f "$CK_ROOT/.env" ]; then
  printf '# 本地沙箱运行配置。9000 被系统服务占用，MinIO 固定 9100。\nAP_MINIO_ENDPOINT=localhost:9100\n' > "$CK_ROOT/.env"
  say ".env 已重建（MinIO 9100）"
fi

# 登录 + 触发增量同步
TOK=$(curl -s -m 5 -X POST localhost:8082/api/v1/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"demo","password":"alphapocket"}' \
  | "$CK_ROOT/core/.venv/bin/python" -c "import sys,json;print(json.load(sys.stdin)['access'])" 2>/dev/null)
[ -n "$TOK" ] || { say "ERR: core 未起或登录失败"; exit 1; }

JOB=$(curl -s -m 5 -X POST localhost:8082/api/v1/data/sync -H "Authorization: Bearer $TOK" \
  -H 'Content-Type: application/json' -d '{"mode":"incremental"}' \
  | "$CK_ROOT/core/.venv/bin/python" -c "import sys,json;print(json.load(sys.stdin).get('job_id',''))" 2>/dev/null)
[ -n "$JOB" ] || { say "WARN: incremental 提交失败（可能 409/503），降级提示手动 bootstrap"; exit 1; }
say "incremental job: $JOB"

for _ in $(seq 1 60); do
  ST=$(curl -s -m 5 "localhost:8082/api/v1/jobs/$JOB" -H "Authorization: Bearer $TOK" \
    | "$CK_ROOT/core/.venv/bin/python" -c "import sys,json;d=json.load(sys.stdin);print(d.get('status'),d.get('progress'),(d.get('error') or '')[:100])" 2>/dev/null)
  case "$ST" in
    done*) say "同步完成: $ST"; break ;;
    failed*) say "ERR: 同步失败: $ST —— 请改跑 bootstrap"; exit 1 ;;
  esac
  sleep 5
done

# 终验：as_of 与六项验证
curl -s -m 5 localhost:8082/api/v1/data/status -H "Authorization: Bearer $TOK" \
  | "$CK_ROOT/core/.venv/bin/python" -c "
import sys,json
d=json.load(sys.stdin)
hb=sum(1 for s in d['securities'] if s['has_bars'])
print(f\"[ck] as_of={d['as_of']} bars={hb}/{len(d['securities'])}\")"
curl -s -m 5 "localhost:8082/api/v1/data/verifications?limit=6" -H "Authorization: Bearer $TOK" \
  | "$CK_ROOT/core/.venv/bin/python" -c "
import sys,json
items=json.load(sys.stdin)['items']
print('[ck] 六项验证:', ' '.join(f\"{i['check']}={i['status']}\" for i in items))"

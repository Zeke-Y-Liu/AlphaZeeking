#!/usr/bin/env bash
# T1.4 — L0 汇总：生成 manifest-L0.json（deps_key、文件清单、sha256、体积）
# 产出 $CACHE/l0/manifest-L0.json；push/pull/restore 均以该 manifest 为准。
set -uo pipefail
CK_SCRIPTS="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=ck-env.sh
source "$CK_SCRIPTS/ck-env.sh"

OUT_DIR=$CACHE/l0
KEY=$(deps_key) || exit 1
HEAD=$(git -C "$CK_REPO" rev-parse --short HEAD)
PYV=$(/usr/bin/python3.12 -V 2>&1 | awk '{print $2}')

python3.12 - "$OUT_DIR" "$KEY" "$HEAD" "$PYV" "$AP_PY_VERSION" <<'PY'
import hashlib, json, os, sys, time

out_dir, key, head, pyv_real, pyv_pin = sys.argv[1:6]
files = ["core-venv.tar.gz", "node-all.tar.gz", "chromium.tar.gz",
         "redis-minio.tar.gz", "pg-embedded.tar.gz"]
items = []
for f in files:
    p = os.path.join(out_dir, f)
    if not os.path.exists(p):
        continue  # chromium 等可选项缺席不阻断
    h = hashlib.sha256(open(p, "rb").read()).hexdigest()
    items.append({"file": f, "sha256": h, "bytes": os.path.getsize(p)})

manifest = {
    "schema": "ap-checkpoint/l0@v1",
    "deps_key": key,
    "git_head": head,
    "python": {"pinned": pyv_pin, "built_with": pyv_real},
    "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "assets": items,
}
dst = os.path.join(out_dir, "manifest-L0.json")
json.dump(manifest, open(dst, "w"), ensure_ascii=False, indent=2)
print(f"[ck] manifest 写入 {dst}（assets={len(items)}）")
PY

# 校验：manifest 内全部 asset 的 sha256 必须与磁盘一致
say "校验 manifest…"
python3.12 - "$OUT_DIR" <<'PY'
import hashlib, json, os, sys
out_dir = sys.argv[1]
m = json.load(open(os.path.join(out_dir, "manifest-L0.json")))
bad = []
for it in m["assets"]:
    p = os.path.join(out_dir, it["file"])
    h = hashlib.sha256(open(p, "rb").read()).hexdigest()
    if h != it["sha256"]:
        bad.append(it["file"])
if bad:
    print(f"[ck] ERR: sha256 不一致: {bad}")
    sys.exit(1)
print(f"[ck] 校验通过：{len(m['assets'])} 个 asset 全部一致（deps_key={m['deps_key']}）")
PY
say "deps_key=$KEY → Release tag: ck-l0-$KEY"

#!/usr/bin/env bash
# T2.1 — 从本地缓存恢复 L0（前置：pull-l0.sh 或 build-l0.sh 已产出 cache/l0）
# 用法：restore-l0.sh [--with-chromium]
# 安全语义：manifest 全量 sha256 校验通过才开始解压；任何不一致 → 退出 2，不做部分恢复。
set -uo pipefail
CK_SCRIPTS="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=ck-env.sh
source "$CK_SCRIPTS/ck-env.sh"

WITH_CHROMIUM="${1:-}"
OUT_DIR=$CACHE/l0
MANIFEST=$OUT_DIR/manifest-L0.json
[ -f "$MANIFEST" ] || { say "ERR: manifest 缺失（先 pull-l0.sh）"; exit 1; }

# ---------- 全量校验（先校验后动手）----------
say "校验 manifest 与本地 asset …"
python3.12 - "$MANIFEST" "$OUT_DIR" "$WITH_CHROMIUM" <<'PY' || exit 2
import hashlib, json, os, sys
m_path, out, opt = sys.argv[1:4]
m = json.load(open(m_path))
missing = []
for a in m["assets"]:
    if a["file"] == "chromium.tar.gz" and opt != "--with-chromium":
        continue
    p = os.path.join(out, a["file"])
    if not os.path.exists(p):
        missing.append(f"{a['file']}(不存在)")
        continue
    h = hashlib.sha256(open(p, "rb").read()).hexdigest()
    if h != a["sha256"]:
        missing.append(f"{a['file']}(sha 不符)")
if missing:
    print(f"[ck] ERR: 校验失败 {missing}——重跑 pull-l0.sh")
    sys.exit(1)
print(f"[ck] 校验通过（{len(m['assets'])} assets, deps_key={m['deps_key']}）")
PY

# ---------- 解压映射 ----------
restore() {  # restore <tar.gz> <dest> <描述>
  local ar="$1" dest="$2" desc="$3"
  say "恢复 $desc → $dest"
  ck_unpack "$OUT_DIR/$ar" "$dest" || { say "ERR: 解压失败 $ar"; exit 2; }
}

restore core-venv.tar.gz  /workspace        "core venv"
restore node-all.tar.gz   /workspace        "node_modules ×3"
restore redis-minio.tar.gz "$AP_BIN"        "redis/minio 二进制"
restore pg-embedded.tar.gz "$AP_BIN"        "PG16 embedded"
[ "$WITH_CHROMIUM" = "--with-chromium" ] && restore chromium.tar.gz /root/.cache "Playwright chromium"

# 布局对齐：redis-minio.tar.gz 内含 bin/ 前缀 → 上移到 AP_BIN 根
if [ -d "$AP_BIN/bin" ]; then
  mv "$AP_BIN/bin/"* "$AP_BIN/" 2>/dev/null || true
  rmdir "$AP_BIN/bin" 2>/dev/null || true
fi

# ---------- 恢复后自检 ----------
say "自检 …"
FAIL=0
"$CK_ROOT/core/.venv/bin/python" -c "import fastapi,pandas,duckdb,akshare" 2>/dev/null \
  || { say "FAIL: venv import"; FAIL=1; }
LD_LIBRARY_PATH="$AP_BIN/libs" "$AP_BIN/redis-server" --version >/dev/null 2>&1 \
  || { say "FAIL: redis"; FAIL=1; }
"$AP_BIN/minio" --version >/dev/null 2>&1 || { say "FAIL: minio"; FAIL=1; }
"$AP_BIN/pg16/lib/bin/initdb" --version >/dev/null 2>&1 || { say "FAIL: initdb"; FAIL=1; }
pnpm -C "$CK_ROOT/web" exec vite --version >/dev/null 2>&1 || { say "FAIL: web 工具链"; FAIL=1; }
[ "$FAIL" -eq 0 ] && say "L0 恢复完成，自检全过" || { say "L0 自检失败"; exit 2; }

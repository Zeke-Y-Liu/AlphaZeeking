#!/usr/bin/env bash
# T1.2 — L0 依赖层构建：node_modules ×3 + Playwright chromium（按需）
# 产出 $CACHE/l0/node-all.tar.gz、chromium.tar.gz（独立 asset，恢复时可跳过）
set -uo pipefail
CK_SCRIPTS="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=ck-env.sh
source "$CK_SCRIPTS/ck-env.sh"

OUT_DIR=$CACHE/l0
mkdir -p "$OUT_DIR"

missing=""
for d in bff/node_modules web/node_modules e2e/node_modules; do
  [ -d "$CK_ROOT/$d" ] || missing="$missing $d"
done
[ -z "$missing" ] || { say "ERR: node_modules 缺失:$missing"; exit 1; }

say "打包 node_modules ×3…"
tar -C /workspace -czf "$OUT_DIR/node-all.tar.gz" \
  alphapocket-web/bff/node_modules \
  alphapocket-web/web/node_modules \
  alphapocket-web/e2e/node_modules
( cd "$OUT_DIR" && sha256sum node-all.tar.gz > node-all.tar.gz.sha256 )
say "OK: node-all.tar.gz（$(du -sh "$OUT_DIR/node-all.tar.gz" | awk '{print $1}')）"

# chromium：存在才打（E2E 用，恢复时可按需跳过）
CHROMIUM_DIR=$(ls -d /root/.cache/ms-playwright 2>/dev/null || true)
if [ -n "$CHROMIUM_DIR" ]; then
  say "打包 Playwright chromium…"
  tar -C /root/.cache -czf "$OUT_DIR/chromium.tar.gz" ms-playwright
  ( cd "$OUT_DIR" && sha256sum chromium.tar.gz > chromium.tar.gz.sha256 )
  say "OK: chromium.tar.gz（$(du -sh "$OUT_DIR/chromium.tar.gz" | awk '{print $1}')）"
else
  say "SKIP: chromium 未安装（需要 E2E 时先 playwright install 再重跑本脚本）"
fi

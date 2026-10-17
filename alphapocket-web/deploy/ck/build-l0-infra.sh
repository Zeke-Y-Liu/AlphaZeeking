#!/usr/bin/env bash
# T1.3 — L0 infra 层构建：捕获「apt 安装产物」为免安装二进制包
# 实测修正（相对原任务卡）：不下载 EDB/静态版——恢复端为同款 Ubuntu 24.04 沙箱，
# 直接打包 apt 装好的 PG16（/usr/lib + /usr/share）与 redis、minio 单文件，
# 网络零依赖、体积更小、确定性更高；恢复端校验失败则降级 apt。
# 产出 $CACHE/l0/{redis-minio.tar.gz, pg-embedded.tar.gz}
set -uo pipefail
CK_SCRIPTS="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=ck-env.sh
source "$CK_SCRIPTS/ck-env.sh"

OUT_DIR=$CACHE/l0
STAGE=$(mktemp -d)
mkdir -p "$OUT_DIR" "$AP_BIN"

cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

say "捕获 redis / minio 单文件…"
mkdir -p "$STAGE/bin"
cp /usr/bin/redis-server "$STAGE/bin/" 2>/dev/null || { say "ERR: redis-server 不在（先 apt）"; exit 1; }
cp /usr/bin/redis-cli "$STAGE/bin/" 2>/dev/null || true
if [ -x /usr/local/bin/minio ]; then
  cp /usr/local/bin/minio "$STAGE/bin/"
else
  say "ERR: /usr/local/bin/minio 不在（先跑一次 start-local-sandbox.sh）"
  exit 1
fi

say "捕获 PG16（保持 initdb 的相对 share 推导布局）…"
# 布局：pg16/lib ← /usr/lib/postgresql/16；pg16/share/postgresql ← /usr/share/postgresql/16
# initdb 在 pg16/lib/bin/initdb 会推导 ../share/postgresql → pg16/share/postgresql ✓
[ -d /usr/lib/postgresql/16 ] || { say "ERR: PG16 未安装（先 apt）"; exit 1; }
mkdir -p "$STAGE/pg16/lib" "$STAGE/pg16/share/postgresql"
cp -r /usr/lib/postgresql/16/. "$STAGE/pg16/lib/"
cp -r /usr/share/postgresql/16/. "$STAGE/pg16/share/postgresql/"

# 落 AP_BIN（本机即刻可用）+ 打包
cp -r "$STAGE/bin/." "$AP_BIN/"
mkdir -p "$AP_BIN/pg16"
cp -r "$STAGE/pg16/lib" "$STAGE/pg16/share" "$AP_BIN/pg16/"

tar -C "$STAGE" -czf "$OUT_DIR/redis-minio.tar.gz" bin
tar -C "$STAGE" -czf "$OUT_DIR/pg-embedded.tar.gz" pg16
( cd "$OUT_DIR" && sha256sum redis-minio.tar.gz pg-embedded.tar.gz > infra.sha256 )

# 恢复端可用性预检：pg 二进制动态依赖是否全部可解析（同款沙箱应全过）
say "预检 postgres 动态依赖…"
if ldd "$AP_BIN/pg16/lib/bin/postgres" 2>/dev/null | grep -q "not found"; then
  say "WARN: postgres 存在未解析依赖（恢复端需重验，失败则降级 apt）"
  ldd "$AP_BIN/pg16/lib/bin/postgres" | grep "not found" || true
else
  say "预检通过：postgres 依赖全可解析"
fi

say "OK:"
du -sh "$OUT_DIR/redis-minio.tar.gz" "$OUT_DIR/pg-embedded.tar.gz"

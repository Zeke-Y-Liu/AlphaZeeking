#!/usr/bin/env bash
# T2.3 — 从 GitHub Release 拉取 L0 镜像到本地缓存
# 用法：pull-l0.sh [--with-chromium]   （chromium 279M，仅 E2E 需要）
# 下载通道：gh api 列 asset 元数据 + curl(Accept: octet-stream) 直下——
# gh release download 在脚本环境有静默产空文件的怪癖（实测），故弃用其文件落盘。
set -uo pipefail
CK_SCRIPTS="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=ck-env.sh
source "$CK_SCRIPTS/ck-env.sh"

WITH_CHROMIUM="${1:-}"
KEY=$(deps_key) || exit 1
TAG="ck-l0-$KEY"
OUT_DIR=$CACHE/l0
REPO_SLUG="Zeke-Y-Liu/AlphaZeeking"
mkdir -p "$OUT_DIR"
ck_ensure_gh_token
[ -n "${GH_TOKEN:-}" ] || { say "ERR: GH_TOKEN 不可用（无法访问 Release）"; exit 1; }

api_url="https://api.github.com/repos/$REPO_SLUG/releases/tags/$TAG"

fetch() {  # fetch <asset_name> <dest_file>
  local url
  url=$(gh api "$api_url" --jq ".assets[] | select(.name==\"$1\") | .url")
  [ -n "$url" ] || { say "ERR: asset 不存在 $1"; return 1; }
  curl -sfL --retry 3 -H "Authorization: Bearer $GH_TOKEN" \
    -H "Accept: application/octet-stream" "$url" -o "$2"
}

# ---------- manifest 先行 ----------
say "拉取 Release $TAG（manifest 先行）…"
rm -f "$OUT_DIR/manifest-L0.json"
fetch manifest-L0.json "$OUT_DIR/manifest-L0.json" || { say "ERR: manifest 下载失败"; exit 1; }
MANIFEST=$OUT_DIR/manifest-L0.json
[ -s "$MANIFEST" ] || { say "ERR: manifest 为空"; exit 1; }
python3.12 -c "import json,sys;json.load(open('$MANIFEST'))" || { say "ERR: manifest 解析失败"; exit 1; }
say "manifest 有效（deps_key=$(python3.12 -c "import json;print(json.load(open('$MANIFEST'))['deps_key'])")）"

# ---------- 逐 asset 下载（sha 命中跳过，支持续跑）----------
ok=0; skip=0; fail=0
N=$(python3.12 -c "import json;print(len(json.load(open('$MANIFEST'))['assets']))")
for i in $(seq 0 $((N-1))); do
  read -r f want < <(python3.12 - "$MANIFEST" "$i" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
a = m["assets"][int(sys.argv[2])]
print(a["file"], a["sha256"])
PY
)
  [ "$f" = "chromium.tar.gz" ] && [ "$WITH_CHROMIUM" != "--with-chromium" ] && { say "SKIP $f（未指定 --with-chromium）"; continue; }
  p="$OUT_DIR/$f"
  have=$([ -f "$p" ] && sha256sum "$p" | awk '{print $1}')
  if [ "$have" = "$want" ]; then
    skip=$((skip+1)); continue
  fi
  say "下载 $f …"
  rm -f "$p"
  if fetch "$f" "$p" && [ "$(sha256sum "$p" | awk '{print $1}')" = "$want" ]; then
    ok=$((ok+1)); say "OK: $f（$(du -sh "$p" | awk '{print $1}')，sha 校验过）"
  else
    fail=$((fail+1)); say "ERR: 下载或校验失败 $f"
  fi
done
[ "$fail" -eq 0 ] || { say "ERR: $fail 个 asset 失败"; exit 1; }
say "完成（新下载=$ok 命中跳过=$skip）→ $OUT_DIR"

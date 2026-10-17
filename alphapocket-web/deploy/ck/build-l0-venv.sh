#!/usr/bin/env bash
# T1.1 — L0 依赖层构建：core venv
# 依据 docs/CHECKPOINT-PLAN.md；产出 $CACHE/l0/core-venv.tar.gz + .sha256
#
# 实测修正（相对原任务卡）：venv 基于 /usr/bin/python3.12（Ubuntu 24.04 自带，
# 重置后恒在，pyvenv.cfg 可证），pyenv 层无需打包——原卡的 pyenv tar 步骤取消。
set -uo pipefail
CK_SCRIPTS="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=ck-env.sh
source "$CK_SCRIPTS/ck-env.sh"

VENV=$CK_ROOT/core/.venv
OUT_DIR=$CACHE/l0
mkdir -p "$OUT_DIR"

[ -d "$VENV" ] || { say "ERR: venv 不存在 $VENV（先重建环境）"; exit 1; }

# venv 打包需保留绝对路径语义：归档内容以 alphapocket-web/core/.venv 开头，
# 恢复端解到 /workspace 即还原原路径（shebang 指向 venv 内部，无需修正）。
ck_pack_venv() {
  local parent=$(dirname "$(dirname "$(dirname "$VENV")")")   # /workspace
  local rel="alphapocket-web/core/.venv"
  if command -v zstd >/dev/null 2>&1; then
    tar -C "$parent" -I zstd --exclude='__pycache__' -cf "$OUT_DIR/core-venv.tar.zst" "$rel" \
      && echo "$OUT_DIR/core-venv.tar.zst"
  else
    tar -C "$parent" -czf "$OUT_DIR/core-venv.tar.gz" --exclude='__pycache__' "$rel" \
      && echo "$OUT_DIR/core-venv.tar.gz"
  fi
}

say "打包 core venv（剔除 __pycache__）…"
ar=$(ck_pack_venv)
[ -n "$ar" ] || { say "ERR: 打包失败"; exit 1; }

say "记录 sha256…"
( cd "$OUT_DIR" && sha256sum "$(basename "$ar")" > "$(basename "$ar").sha256" )
say "OK: $ar（$(du -sh "$ar" | awk '{print $1}')）"

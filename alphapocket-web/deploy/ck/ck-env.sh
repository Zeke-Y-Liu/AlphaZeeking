#!/usr/bin/env bash
# AP-Checkpoint 脚本基座（T0.1，docs/CHECKPOINT-PLAN.md）
# 供 T1.x/T2.x/T3.x 各构建与恢复脚本 source 使用；本文件只定义变量与函数，无副作用。
#
# 用法：source "$(dirname "$0")/ck-env.sh"
set -uo pipefail

# ---------- 路径约定 ----------
CK_DIR=/workspace/.ck                # checkpoint 根（gitignored：cache/probe 等运行内容）
CACHE=$CK_DIR/cache                  # L0 归档缓存（Release 拉取后的落点）
AP_BIN=/workspace/ap-bin             # 免 apt 的基础设施二进制（gitignored）
AP_DATA=/workspace/ap-data           # 运行数据（PG 数据目录 / minio / redis dump）
CK_REPO=/workspace                   # git 仓库根
CK_ROOT=/workspace/alphapocket-web   # monorepo 业务根
CK_SCRIPTS=$CK_ROOT/deploy/ck        # 本目录

# deps_key 中 python 版本用常量而非运行时探测——
# 沙箱重置后裸系统 python 版本（3.14）与 venv 构建版本（3.12.3）不同，
# 用探测值会导致 deps_key 漂移、缓存误判失效。
AP_PY_VERSION="${AP_PY_VERSION:-3.12.3}"

# ---------- 日志 ----------
say() { echo "[ck $(date +%H:%M:%S)] $*"; }

# ---------- deps_key：L0 镜像的有效性绑定键 ----------
# 公式：sha256( pyproject ‖ bff-lock ‖ web-lock ‖ e2e-pkg ‖ AP_PY_VERSION )[:12]
# 任一依赖清单变更 → key 变更 → L0 需重建；数据/代码改动不影响（L0 只绑依赖）。
deps_key() {
  local acc="" f
  for f in \
    "$CK_ROOT/core/pyproject.toml" \
    "$CK_ROOT/bff/pnpm-lock.yaml" \
    "$CK_ROOT/web/pnpm-lock.yaml" \
    "$CK_ROOT/e2e/package.json"; do
    if [ ! -f "$f" ]; then
      say "ERR: 依赖清单缺失 $f"
      return 1
    fi
    acc="$acc$(sha256sum "$f" | awk '{print $1}')"
  done
  printf '%s|%s' "$acc" "$AP_PY_VERSION" | sha256sum | awk '{print substr($1,1,12)}'
}

# ---------- ck_hash：单文件 sha256 ----------
ck_hash() {  # ck_hash <file> → hash；文件缺失返回 1
  [ -f "$1" ] && sha256sum "$1" | awk '{print $1}'
}

# ---------- 打包 / 解包（zstd 优先，gzip 兜底）----------
# 沙箱裸系统无 zstd（实测），构建阶段装好 zstd-static 后自动升级压缩格式；
# manifest 记录实际后缀，恢复端按后缀分发。目录打包保留一层目录名，
# 解包到指定 dest（dest 内即出现该目录）。
ck_pack() {  # ck_pack <src_dir> <out_base> → 成功时 echo 归档实际路径
  local src="$1" out="$2" parent base
  if [ ! -d "$src" ]; then
    say "ERR: 打包源不是目录 $src"
    return 1
  fi
  parent=$(dirname "$src")
  base=$(basename "$src")
  if command -v zstd >/dev/null 2>&1; then
    tar -C "$parent" -I zstd -cf "$out.tar.zst" "$base" && echo "$out.tar.zst"
  else
    tar -C "$parent" -czf "$out.tar.gz" "$base" && echo "$out.tar.gz"
  fi
}

ck_unpack() {  # ck_unpack <archive> <dest_dir>
  local ar="$1" dest="$2"
  if [ ! -f "$ar" ]; then
    say "ERR: 归档不存在 $ar"
    return 1
  fi
  mkdir -p "$dest"
  case "$ar" in
    *.tar.zst)
      if command -v zstd >/dev/null 2>&1; then
        tar -I zstd -xf "$ar" -C "$dest"
      else
        say "ERR: $ar 需要 zstd 但系统缺失（应先恢复 ap-bin/zstd）"
        return 1
      fi
      ;;
    *.tar.gz|*.tgz) tar -xzf "$ar" -C "$dest" ;;
    *)
      say "ERR: 未知归档格式 $ar"
      return 1
      ;;
  esac
}

# ---------- 验收自测入口（仅手动调用）----------
# bash deploy/ck/ck-env.sh --selftest
if [ "${1:-}" = "--selftest" ]; then
  tmp=$(mktemp -d)
  echo "hello-checkpoint" > "$tmp/src"
  mkdir -p "$tmp/roundtrip/sub"
  echo "payload" > "$tmp/roundtrip/sub/file.txt"
  ar=$(ck_pack "$tmp/roundtrip" "$tmp/rt")
  rm -rf "$tmp/roundtrip"
  ck_unpack "$ar" "$tmp"
  if [ "$(cat "$tmp/roundtrip/sub/file.txt")" = "payload" ]; then
    say "selftest: pack/unpack round-trip OK ($(basename "$ar"))"
  else
    say "selftest: FAIL"
    exit 1
  fi
  say "selftest: deps_key=$(deps_key)"
  rm -rf "$tmp"
fi

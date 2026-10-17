#!/usr/bin/env bash
# 重置语义哨兵（T0.3，docs/CHECKPOINT-PLAN.md §2.1）
#
# 目的：用一对对照文件精确刻画每次沙箱重置清了什么——
#   - tracked marker   /workspace/.ck-probe-marker（进 git，随仓库走）
#   - untracked marker /workspace/.ck/probe-untracked.txt（gitignored，重置即丢）
# 每次「mark」覆盖写入新内容；下次会话跑「report」对比两者存活与 git HEAD，
# 输出本次重置的破坏面结论，为 checkpoint 存放策略提供持续证据。
#
# 用法：bash ck-probe.sh mark | report
set -uo pipefail

CK_ENV="$(dirname "$0")/ck-env.sh"
# shellcheck source=ck-env.sh
source "$CK_ENV"

TRACKED_MARKER=/workspace/.ck-probe-marker          # 进 git
UNTRACKED_MARKER=$CK_DIR/probe-untracked.txt        # gitignored（.gitignore 已登记 .ck/）

_mark() {
  local ts rand head
  ts=$(date +%s)
  rand=$RANDOM$RANDOM
  head=$(git -C "$CK_REPO" rev-parse --short HEAD 2>/dev/null || echo "no-git")
  mkdir -p "$CK_DIR"
  echo "mark|$ts|$rand|$head" > "$TRACKED_MARKER"
  echo "mark|$ts|$rand|$head" > "$UNTRACKED_MARKER"
  say "已写入对照标记（内容一致，存活面不同）："
  say "  tracked   $TRACKED_MARKER"
  say "  untracked $UNTRACKED_MARKER"
}

_report() {
  local t_exists u_exists t_head u_head cur_head verdict
  t_exists="no"; u_exists="no"
  [ -f "$TRACKED_MARKER" ] && t_exists="yes" && t_head=$(awk -F'|' '{print $4}' "$TRACKED_MARKER")
  [ -f "$UNTRACKED_MARKER" ] && u_exists="yes" && u_head=$(awk -F'|' '{print $4}' "$UNTRACKED_MARKER")
  cur_head=$(git -C "$CK_REPO" rev-parse --short HEAD 2>/dev/null || echo "no-git")

  say "git 跟踪内容  : $t_exists（marker HEAD=${t_head:-n/a}，当前 HEAD=$cur_head）"
  say "gitignored 内容: $u_exists"
  if [ "$t_exists" = "no" ]; then
    verdict="严重：连 git 跟踪内容都丢失（全新 clone 或更狠的清理）"
  elif [ "$u_exists" = "no" ]; then
    verdict="已重置：gitignored/系统盘被清，仅 git 跟踪内容存活（Checkpoint 方案的核心前提成立）"
  elif [ "$t_head" = "$u_head" ] && [ "$t_head" = "$cur_head" ]; then
    verdict="未重置：同一会话延续"
  else
    verdict="分支切换或部分清理：两个 marker 都在但 HEAD 漂移（marker HEAD=$t_head vs 当前=$cur_head）"
  fi
  say "结论          : $verdict"
}

case "${1:-report}" in
  mark)   _mark ;;
  report) _report ;;
  *)
    echo "用法：$0 mark|report" >&2
    exit 1
    ;;
esac

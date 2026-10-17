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
  local t_exists u_exists t_ts u_ts cur_head verdict
  t_exists="no"; u_exists="no"
  [ -f "$TRACKED_MARKER" ] && t_exists="yes" && t_ts=$(awk -F'|' '{print $2}' "$TRACKED_MARKER")
  [ -f "$UNTRACKED_MARKER" ] && u_exists="yes" && u_ts=$(awk -F'|' '{print $2}' "$UNTRACKED_MARKER")
  cur_head=$(git -C "$CK_REPO" rev-parse --short HEAD 2>/dev/null || echo "no-git")

  say "git 跟踪内容  : $t_exists（当前 HEAD=$cur_head）"
  say "gitignored 内容: $u_exists"
  # 判定只看两个 marker 的存活：HEAD 漂移属分支正常演进（同会话提交也会前进），
  # 不作为重置信号。untracked 丢而 tracked 在 = 重置（方案核心前提的实证）。
  if [ "$t_exists" = "no" ]; then
    verdict="严重：连 git 跟踪内容都丢失（全新 clone 或更狠的清理）"
  elif [ "$u_exists" = "no" ]; then
    verdict="已重置：gitignored/系统盘被清，仅 git 跟踪内容存活（Checkpoint 方案的核心前提成立）"
  else
    verdict="未重置：同一会话延续（marker ts: tracked=$t_ts untracked=$u_ts）"
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

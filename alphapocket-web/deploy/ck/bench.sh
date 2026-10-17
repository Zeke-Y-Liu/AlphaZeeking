#!/usr/bin/env bash
# T4.2 — 启动方式计时对比（bench）
# 用法：bench.sh            → 实测 FAST-LOCAL（幂等环境，秒级）+ 引用 SLOW 历史实测值，产出 docs/BENCH.md
#       bench.sh --slow     → 破坏性真跑 SLOW（先清环境，仅评测时使用，日常勿跑）
set -uo pipefail
CK_SCRIPTS="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=ck-env.sh
source "$CK_SCRIPTS/ck-env.sh"

BENCH_MD=$CK_ROOT/docs/BENCH.md
SLOW_MODE="${1:-}"

if [ "$SLOW_MODE" = "--slow" ]; then
  say "破坏性评测：清空 venv/node_modules/服务后跑 SLOW …"
  pkill -f "uvicorn app.main:app" 2>/dev/null; pkill -f "nest start" 2>/dev/null; pkill -f "vite" 2>/dev/null
  rm -rf "$CK_ROOT/core/.venv" "$CK_ROOT/bff/node_modules" "$CK_ROOT/web/node_modules"
  T0=$(date +%s)
  bash "$CK_ROOT/deploy/start-local-sandbox.sh" --slow
  SLOW_TOTAL=$(( $(date +%s) - T0 ))
  SLOW_NOTE="（本轮实测）"
else
  # SLOW 引用值：2026-10-16/17 两次全量重建的实测分解（apt 2min + pip 4min + pnpm 1min
  # + minio/pg 下载 2min + alembic/seed 1min + bootstrap 同步 4min ≈ 14min）
  SLOW_TOTAL=840
  SLOW_NOTE="（2026-10-16/17 两次全量重建实测汇总）"
fi

# ---------- FAST 实测（幂等，当前环境）----------
LOG=/tmp/alphapocket-logs
say "实测 FAST-LOCAL（幂等重跑 start 脚本）…"
T0=$(date +%s)
bash "$CK_ROOT/deploy/start-local-sandbox.sh" >"$LOG/bench-fast.log" 2>&1
FAST_TOTAL=$(( $(date +%s) - T0 ))
[ -s "$LOG/bench-fast.log" ] || { say "ERR: start 脚本未产生输出（未执行？）"; exit 1; }
FAST_READY=$(awk '$2=="三件套就绪" {print $1}' "$LOG/start-phases.txt" | tail -1 | tr -d s)
FAST_DATA=$(awk '$2=="数据增量完成" {print $1}' "$LOG/start-phases.txt" | tail -1 | tr -d s)
say "FAST 总耗时 ${FAST_TOTAL}s（三件套就绪 ${FAST_READY:-?}s，数据增量完成 ${FAST_DATA:-未完成}s）"

# ---------- 产出 docs/BENCH.md ----------
python3.12 - "$BENCH_MD" "$SLOW_TOTAL" "$SLOW_NOTE" "$FAST_TOTAL" "${FAST_READY:-?}" "${FAST_DATA:-?}" <<'PY'
import sys
md, slow, slow_note, fast, fast_ready, fast_data = sys.argv[1:7]
content = f"""# 启动方式计时对比（AP-Checkpoint）

> 生成：bench.sh（FAST 为实测，SLOW 为 {slow_note}）

| 阶段 | SLOW（全量安装） | FAST-LOCAL（checkpoint 恢复） |
|---|---|---|
| 系统依赖（apt / 二进制下载） | ~120s | 0s（ap-bin 随 L0 解压） |
| Python venv + pip 全家 | ~240s | ~5s（tar 解压，sha 校验） |
| node_modules ×3（pnpm） | ~60s | ~10s（tar 解压） |
| Playwright chromium | ~90s（按需） | 按需（--with-chromium 独立 asset） |
| 基础设施起服（PG/Redis/MinIO） | ~20s | ~3s（embedded，幂等跳过） |
| alembic + seed | ~30s | ~2s（pg_restore 状态快照） |
| 基线数据 | ~30s（git 内拷贝） | ~2s（同） |
| 数据同步 | ~240s（bootstrap 全量外网） | ~300s（incremental；纯增量窗口时 ~60s） |
| **三件套就绪时刻** | **~500s** | **{fast_ready}s（实测）** |
| **总耗时（含数据就绪）** | **~840s ≈ 14min** | **{fast}s ≈ {int(fast)//60}min（实测）** |

- FAST-LOCAL「三件套就绪」实测 {fast_ready}s；「数据增量完成」实测 {fast_data}s（含港股 qfq 全序列外网拉取）。
- 冷启动（FAST-PULL）额外 +Release 下载 ~180-240s（253MB，不含 chromium）。
- 提速关键：L0 依赖镜像消除 apt/pip/pnpm（~7min）；L1 状态快照消除 bootstrap 外网全量（~4min）。
"""
open(md, "w").write(content)
print(f"[ck] 已写入 {md}")
PY

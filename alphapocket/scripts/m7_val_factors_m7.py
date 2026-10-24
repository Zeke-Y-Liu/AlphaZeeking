"""M7 T-F2：估值族 4 因子物化 + val_pe_ttm v1.1.0（TTM 累计口径消费端修正）。

前置：m7_fetch_cashflow_em.py 已补拉 dep_amort；dim/market_cap.json 51 标的周频市值就位。
动作：
  1) bump val_pe_ttm 1.0.0→1.1.0（sandbox/repo 状态真源；v1.0.0 物化行冻结只读）
  2) factorlib.prepare：remote/repo → sandbox/data/fl.duckdb（幂等）
  3) full 物化 val_pe_ttm（yaml 已升 1.1.0）+ 新 4 估值因子（val_pb_lf/val_ps_ttm/
     val_ev_ebitda/val_div_yield，假设 H-0007..H-0010 绑定）
  4) 数据质量核对：各因子覆盖量级 / NULL 行为 / lineage 登记

运行：``cd alphapocket && /workspace/alphapocket-web/core/.venv/bin/python scripts/m7_val_factors_m7.py``
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REPO = ROOT / "remote" / "repo"           # 物化数据面（defs 回落包内 factorlib/defs）
SB_REPO = ROOT / "sandbox" / "repo"       # 状态真源 + hypothesis/registry.json
DB = ROOT / "sandbox" / "data" / "fl.duckdb"
NEW = ("val_pb_lf", "val_ps_ttm", "val_ev_ebitda", "val_div_yield")


def main() -> int:
    import duckdb

    from factorlib.materialize import materialize
    from factorlib.prepare import prepare
    from factorlib.registry import bump_version, load_factor

    t0 = time.time()
    print("[1/4] bump val_pe_ttm 1.0.0 -> 1.1.0（TTM 累计口径消费端修正；幂等）")
    from factorlib.registry import load_factor as _lf
    cur = _lf(SB_REPO, "val_pe_ttm").version
    if cur == "1.1.0":
        print("      已处于 1.1.0，跳过")
    else:
        rec = bump_version(SB_REPO, "val_pe_ttm", "1.1.0", who="m7-tf2",
                           why="TTM 阶梯改累计口径，消费端显式过滤 NULL 回退最近可算报告期")
        print(f"      {rec['from']} -> {rec['to']} @ {rec['at']}")

    print(f"[2/4] prepare {REPO} -> {DB}")
    summary = prepare(REPO, DB)
    for k in sorted(summary):
        print(f"      {k:24s} {summary[k]:>8} 行")

    print("[3/4] full 物化：val_pe_ttm(v1.1.0) + 4 估值因子")
    for fid in ("val_pe_ttm", *NEW):
        meta = load_factor(REPO, fid)
        if fid == "val_pe_ttm":
            # 包内 yaml 保持 1.0.0 静态基线（版本冻结守卫锚点）；运行时版本走状态面
            # bump（上方 [1/4]），物化经 dataclasses.replace 对齐 1.1.0（param_grid_eval 同款）
            from dataclasses import replace
            meta = replace(meta, version="1.1.0")
        s = materialize(meta, DB, mode="full")
        print(f"      {fid:16s} v{s.get('version', '?')} written={s.get('written')} "
              f"span={s.get('start')}..{s.get('end')} lineage={s.get('lineage_id', '')[:12]}…")

    print("[4/4] 数据质量核对")
    con = duckdb.connect(str(DB), read_only=True)
    try:
        for r in con.execute(
                "select factor_id, factor_version, count(*), count(distinct secid), "
                "min(dt), max(dt) from fl_factor_value_daily "
                f"where factor_id in ('val_pe_ttm', {','.join('?' * len(NEW))}) "
                "group by 1, 2 order by 1", list(NEW)).fetchall():
            print(f"      {r[0]:16s} v{r[1]} {r[2]:>8} 行 / {r[3]} 标的 / {r[4]}..{r[5]}")
        # lineage / 定义指纹登记核对
        for r in con.execute(
                "select factor_id, factor_version, count(*) from fl_factor_lineage "
                f"where factor_id in ('val_pe_ttm', {','.join('?' * len(NEW))}) "
                "group by 1, 2 order by 1, 2", list(NEW)).fetchall():
            print(f"      lineage {r[0]:16s} v{r[1]} ×{r[2]}")
        # 分位数快照（量纲合理性：P/B~1、P/S~数、EV/EBITDA~十位、股息率~小数）
        for fid in NEW:
            q = con.execute(
                "select quantile_cont(value, 0.1), median(value), quantile_cont(value, 0.9) "
                "from fl_factor_value_daily where factor_id = ? and value is not null",
                [fid]).fetchone()
            print(f"      {fid:16s} p10={q[0]:.4g} p50={q[1]:.4g} p90={q[2]:.4g}")
    finally:
        con.close()

    print(f"完成，耗时 {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

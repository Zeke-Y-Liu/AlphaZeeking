"""M7 T-F4：成长族 4 因子物化（gro_rev_yoy / gro_np_yoy / gro_rev_cagr_3y / gro_stability）。

前置：T-F1 财报序列 2017 起拉取（yoy 2018Q1 / cagr_3y·stability 2020Q1-2020Q2 起有值）。
动作（幂等）：
  1) factorlib.prepare：remote/repo → sandbox/data/fl.duckdb（重建 fl_* 输入表）
  2) full 物化 4 成长因子（假设 H-0016..H-0019 绑定）
  3) 数据质量核对：覆盖量级 / 量纲分位 / 预热锚点（2021-01 截面四因子覆盖 >0）
     / np_yoy 负净利置空计数披露（金融/周期股负净利占比，如实计数）

运行：``cd alphapocket && /workspace/alphapocket-web/core/.venv/bin/python scripts/m7_gro_factors_m7.py``
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REPO = ROOT / "remote" / "repo"           # 物化数据面（defs 回落包内 factorlib/defs）
DB = ROOT / "sandbox" / "data" / "fl.duckdb"
GRO4 = ("gro_rev_yoy", "gro_np_yoy", "gro_rev_cagr_3y", "gro_stability")


def main() -> int:
    import duckdb

    from factorlib.materialize import materialize
    from factorlib.prepare import prepare
    from factorlib.registry import load_factor

    t0 = time.time()
    print(f"[1/3] prepare {REPO} -> {DB}")
    summary = prepare(REPO, DB)
    for k in sorted(summary):
        print(f"      {k:24s} {summary[k]:>8} 行")

    print("[2/3] full 物化 4 成长因子")
    for fid in GRO4:
        meta = load_factor(REPO, fid)
        s = materialize(meta, DB, mode="full")
        print(f"      {fid:18s} v{s.get('version')} written={s.get('written')} "
              f"span={s.get('start')}..{s.get('end')} lineage={s.get('lineage_id', '')[:12]}…")

    print("[3/3] 数据质量核对")
    con = duckdb.connect(str(DB), read_only=True)
    try:
        for r in con.execute(
                "select factor_id, factor_version, count(*), count(distinct secid), "
                "min(dt), max(dt) from fl_factor_value_daily "
                f"where factor_id in ({','.join('?' * len(GRO4))}) "
                "group by 1, 2 order by 1", list(GRO4)).fetchall():
            print(f"      {r[0]:18s} v{r[1]} {r[2]:>8} 行 / {r[3]} 标的 / {r[4]}..{r[5]}")
        for r in con.execute(
                "select factor_id, factor_version, count(*) from fl_factor_lineage "
                f"where factor_id in ({','.join('?' * len(GRO4))}) "
                "group by 1, 2 order by 1, 2", list(GRO4)).fetchall():
            print(f"      lineage {r[0]:18s} v{r[1]} ×{r[2]}")
        # 量纲分位（yoy ~±小数、cagr ~正小数、stability ≥0）
        for fid in GRO4:
            q = con.execute(
                "select quantile_cont(value, 0.1), median(value), quantile_cont(value, 0.9) "
                "from fl_factor_value_daily where factor_id = ? and value is not null",
                [fid]).fetchone()
            print(f"      {fid:18s} p10={q[0]:.4g} p50={q[1]:.4g} p90={q[2]:.4g}")
        # 预热锚点（T-F1 拉取起点生效验证）：2021-01 月内截面四因子覆盖标的数
        for r in con.execute(
                "select factor_id, count(distinct secid) from fl_factor_value_daily "
                "where dt between '2021-01-01' and '2021-01-31' "
                f"and factor_id in ({','.join('?' * len(GRO4))}) group by 1 order by 1",
                list(GRO4)).fetchall():
            print(f"      预热 2021-01: {r[0]:18s} 覆盖 {r[1]} 标的（应 >0）")
        # np_yoy 负净利置空计数披露（契约：如实计数，不 mock）
        r = con.execute(
            "select count(*) filter (where ttm_np <= 0), count(*) filter (where ttm_np is not null) "
            "from fl_dws_financial_ttm").fetchone()
        pct = 100.0 * r[0] / r[1] if r[1] else 0.0
        print(f"      np_yoy 置空披露：ttm_np ≤0 报告期 {r[0]}/{r[1]}（{pct:.1f}%，"
              "负净利同比失真 → NULL 不落行）")
    finally:
        con.close()

    print(f"完成，耗时 {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

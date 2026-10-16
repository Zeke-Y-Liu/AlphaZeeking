"""M7 T-F3：质量族 5 因子物化（qua_roe_ttm / qua_gross_margin / qua_accruals /
qua_ocf_to_np / qua_debt_to_asset）。

前置：T-F1 扩展科目已落位（ocf / cost_of_sales）；prepare TTM 阶梯已扩 ttm_ocf/ttm_cost。
动作（幂等）：
  1) factorlib.prepare：remote/repo → sandbox/data/fl.duckdb（重建 fl_dws_financial_ttm 含新列）
  2) full 物化 5 质量因子（假设 H-0011..H-0015 绑定）
  3) 数据质量核对：覆盖量级 / 量纲分位 / gross_margin 缺口披露（金融行业无营业成本）

运行：``cd alphapocket && /workspace/alphapocket-web/core/.venv/bin/python scripts/m7_qua_factors_m7.py``
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REPO = ROOT / "remote" / "repo"           # 物化数据面（defs 回落包内 factorlib/defs）
DB = ROOT / "sandbox" / "data" / "fl.duckdb"
QUA5 = ("qua_roe_ttm", "qua_gross_margin", "qua_accruals",
        "qua_ocf_to_np", "qua_debt_to_asset")


def main() -> int:
    import duckdb

    from factorlib.materialize import materialize
    from factorlib.prepare import prepare
    from factorlib.registry import load_factor

    t0 = time.time()
    print(f"[1/3] prepare {REPO} -> {DB}（TTM 阶梯扩 ttm_ocf/ttm_cost）")
    summary = prepare(REPO, DB)
    for k in sorted(summary):
        print(f"      {k:24s} {summary[k]:>8} 行")

    print("[2/3] full 物化 5 质量因子")
    for fid in QUA5:
        meta = load_factor(REPO, fid)
        s = materialize(meta, DB, mode="full")
        print(f"      {fid:18s} v{s.get('version', '?')} written={s.get('written')} "
              f"span={s.get('start')}..{s.get('end')} lineage={s.get('lineage_id', '')[:12]}…")

    print("[3/3] 数据质量核对")
    con = duckdb.connect(str(DB), read_only=True)
    try:
        for r in con.execute(
                "select factor_id, factor_version, count(*), count(distinct secid), "
                "min(dt), max(dt) from fl_factor_value_daily "
                f"where factor_id in ({','.join('?' * len(QUA5))}) "
                "group by 1, 2 order by 1", list(QUA5)).fetchall():
            print(f"      {r[0]:18s} v{r[1]} {r[2]:>8} 行 / {r[3]} 标的 / {r[4]}..{r[5]}")
        for r in con.execute(
                "select factor_id, factor_version, count(*) from fl_factor_lineage "
                f"where factor_id in ({','.join('?' * len(QUA5))}) "
                "group by 1, 2 order by 1, 2", list(QUA5)).fetchall():
            print(f"      lineage {r[0]:18s} v{r[1]} ×{r[2]}")
        # 量纲分位（ROE~小数、毛利率 0-1、应计~±小数、含金量~正数、资产负债率 0-1）
        for fid in QUA5:
            q = con.execute(
                "select quantile_cont(value, 0.1), median(value), quantile_cont(value, 0.9) "
                "from fl_factor_value_daily where factor_id = ? and value is not null",
                [fid]).fetchone()
            print(f"      {fid:18s} p10={q[0]:.4g} p50={q[1]:.4g} p90={q[2]:.4g}")
        # gross_margin 缺口披露：无营业成本科目标的数（如实，非 mock）
        r = con.execute(
            "select count(distinct f.secid) from fl_fin_report f "
            "where f.cost_of_sales is null "
            "and not exists (select 1 from fl_fin_report g "
            "                where g.secid = f.secid and g.cost_of_sales is not null)"
        ).fetchone()
        print(f"      gross_margin data_gap：cost_of_sales 全缺标的 {r[0]} 只（金融行业等，如实缺）")
    finally:
        con.close()

    print(f"完成，耗时 {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

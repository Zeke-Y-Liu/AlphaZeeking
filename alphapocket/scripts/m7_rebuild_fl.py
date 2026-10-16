"""M7 T-F1：fl.duckdb 全链路重建 + 存量 4 因子 full 重物化。

前置：m7_fetch_universe.py 已完成（remote/repo 56 标的行情/财务/分红/市值/日历就位）。
动作：
  1) factorlib.prepare：repo JSON → fl_* 输入表（drop+create+insert 幂等，扩列 + fl_dividend 新表）
  2) 存量 4 因子 materialize(mode="full")：M5 D.4 口径——输入指纹漂移放行、定义未变不升版，
     lineage 追加行留痕；v1 策略历史产物冻结不动。

运行：``cd alphapocket && /workspace/alphapocket-web/core/.venv/bin/python scripts/m7_rebuild_fl.py``
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REPO = ROOT / "remote" / "repo"
DB = ROOT / "sandbox" / "data" / "fl.duckdb"
FACTORS = ("val_pe_ttm", "mom_20d_fl", "sig_mom20_rank_fl", "eps_revise_fl")


def main() -> int:
    import duckdb

    from factorlib.materialize import materialize
    from factorlib.prepare import prepare
    from factorlib.registry import load_factor

    t0 = time.time()
    print(f"[1/2] prepare {REPO} → {DB}")
    summary = prepare(REPO, DB)
    for k in sorted(summary):
        print(f"      {k:24s} {summary[k]:>8} 行")

    print("[2/2] 存量 4 因子 full 重物化")
    for fid in FACTORS:
        meta = load_factor(REPO, fid)
        s = materialize(meta, DB, mode="full")
        print(f"      {fid:20s} v{s.get('version', '?')} written={s.get('written')} "
              f"span={s.get('start')}..{s.get('end')} lineage={s.get('lineage_id', '')[:12]}…")

    con = duckdb.connect(str(DB), read_only=True)
    try:
        print("[核对] 物化量级与 lineage：")
        for r in con.execute(
                "select factor_id, count(*), count(distinct secid), min(dt), max(dt) "
                "from fl_factor_value_daily where factor_id in (%s) "
                "group by factor_id order by factor_id" % ",".join("?" * len(FACTORS)),
                list(FACTORS)).fetchall():
            print(f"      {r[0]:20s} {r[1]:>8} 行 / {r[2]} 标的 / {r[3]}..{r[4]}")
        for r in con.execute(
                "select factor_id, factor_version, count(*) from fl_factor_lineage "
                "where factor_id in (%s) group by 1,2 order by 1,2" % ",".join("?" * len(FACTORS)),
                list(FACTORS)).fetchall():
            print(f"      lineage {r[0]:20s} v{r[1]} ×{r[2]}")
        n_dvd, n_no_ann = con.execute(
            "select count(*), count(*) filter (where announce_date is null) "
            "from fl_dividend").fetchone()
        print(f"[披露] fl_dividend {n_dvd} 行，缺公告日 {n_no_ann} 行"
              f"（{n_no_ann / n_dvd:.1%}，PIT 如实 NULL）" if n_dvd else "[披露] fl_dividend 空")
    finally:
        con.close()

    print(f"完成，耗时 {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

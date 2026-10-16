"""M7 T-F5：动量+低波红利族 6 因子物化（mom_12_1 / mom_6_1 / mom_industry_adj /
low_vol_252d / low_beta / div_payout_streak）。

前置：T-F1 行情 2017 起 + 分红历史（fl_dividend 1994 起）+ T-F5 行业 PIT
（dim/industry_sw.json → fl_industry_sw，51 标的全覆盖）。
动作（幂等）：
  1) factorlib.prepare：remote/repo → sandbox/data/fl.duckdb（重建 fl_* 输入表，
     含新增 fl_industry_sw 行业 PIT）
  2) full 物化 6 因子（假设 H-0020..H-0025 绑定）
  3) 数据质量核对：覆盖量级 / 量纲分位 / 预热锚点（2021-01 截面六因子覆盖 >0，
     契约「检验窗 2021-01 起全因子覆盖，无 M6 mom 族截断问题」）
     / streak 分布与无分红史计数 / 行业回退计数披露

运行：``cd alphapocket && /workspace/alphapocket-web/core/.venv/bin/python scripts/m7_mom_lowvol_factors_m7.py``
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REPO = ROOT / "remote" / "repo"           # 物化数据面（defs 回落包内 factorlib/defs）
DB = ROOT / "sandbox" / "data" / "fl.duckdb"
SIX = ("mom_12_1", "mom_6_1", "mom_industry_adj",
       "low_vol_252d", "low_beta", "div_payout_streak")


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

    print("[2/3] full 物化 6 动量/低波/红利因子")
    for fid in SIX:
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
                f"where factor_id in ({','.join('?' * len(SIX))}) "
                "group by 1, 2 order by 1", list(SIX)).fetchall():
            print(f"      {r[0]:18s} v{r[1]} {r[2]:>8} 行 / {r[3]} 标的 / {r[4]}..{r[5]}")
        # 量纲分位：动量 ~±小数；vol/beta ≥0；streak 整数 ≥0
        for fid in SIX:
            q = con.execute(
                "select quantile_cont(value, 0.1), median(value), quantile_cont(value, 0.9) "
                "from fl_factor_value_daily where factor_id = ? and value is not null",
                [fid]).fetchone()
            print(f"      {fid:18s} p10={q[0]:.4g} p50={q[1]:.4g} p90={q[2]:.4g}")
        # 预热锚点（契约：六因子 2021-01 截面全部有值 → 检验窗 2021-01 起无截断）
        for r in con.execute(
                "select factor_id, count(distinct secid) from fl_factor_value_daily "
                "where dt between '2021-01-01' and '2021-01-31' "
                f"and factor_id in ({','.join('?' * len(SIX))}) group by 1 order by 1",
                list(SIX)).fetchall():
            print(f"      预热 2021-01: {r[0]:18s} 覆盖 {r[1]} 标的（应 >0）")
        # streak 分布 + 无分红史计数披露（值 0 = 从未分红/未确认，真实信息非缺失）
        for r in con.execute(
                "select value, count(distinct secid) from fl_factor_value_daily "
                "where factor_id = 'div_payout_streak' and dt = "
                "(select max(dt) from fl_factor_value_daily where factor_id = 'div_payout_streak') "
                "group by 1 order by 1").fetchall():
            print(f"      streak 末日截面: 值={int(r[0])} 标的数={r[1]}")
        # 行业回退披露：当前 l1 分布中截面样本 <3 的组（回退路径真实触发）
        r = con.execute("""
            with j as (
                select m.dt, m.secid, i.l1_code
                from (select distinct dt, secid from fl_factor_value_daily
                      where factor_id = 'mom_industry_adj') m
                asof left join fl_industry_sw i on i.secid = m.secid and i.start_date <= m.dt
            )
            select l1_code, count(distinct secid) from j
            where l1_code is not null
            group by 1 having count(distinct secid) < 3 order by 2, 1
        """).fetchall()
        print(f"      行业回退披露：l1 组样本 <3 的组 = {r or '无'}"
              "（这些组标的的 mom_industry_adj 回退 mom_12_1 原值，非静默）")
    finally:
        con.close()

    print(f"完成，耗时 {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""M2 黄金样例测试（实施计划 §5 DoD-2）：估值 + 质量 + 过滤 11 因子，手工值误差 = 0。

合成库 G2（24 连续交易日 2020-12-16..2021-01-08 × 10 标的），手工推演基准：
- TTM 链规则同 val_pe_ttm（p0 = announce_date <= dt 中 (announce, period) 最大者；
  年报 = 全年 ytd；季报 = 本期 − 上年同期 + 上年年报；链缺任一期置空）；
- LF 类（pb/margin/accruals/ocf_to_np/debt_to_asset）= 最新公告报告的 ytd 值；
- ps_ttm = mktcap(亿元)*1e8 / rev_ttm（市值表日粒度合成，无 ASOF 缺口）；
- div_yield = 近 12 月已实施 DPS 之和 / close（ex_date ∈ (dt−1y, dt]）；
- 财务因子无公告无行；分母非正置空（bps<=0 / rev_ttm<=0 / np<=0 / ta<=0）。

标的覆盖矩阵（财务位点 2021-01-04..08）：
  A1 全科目年报型（01-05 公告 FY2020）      → 估值/质量全族有值
  A2 年报→季报切换（01-05 FY / 01-07 Q1）   → 公告日切换 + TTM 滚动
  A3 边界型：bps<0 / np<0 / roe_w 缺 / 负毛利 / 资不抵债 → 分母置空 + COALESCE 兜底
  A4 链断型：仅 2021Q1（缺同期/上年年报）   → TTM 族无行，LF 族有值
  A5 公告日边界：01-06 公告                 → 01-04/05 无行
  A6..A10 无财报（flt_st 专用：ST/*ST/S*ST/正常/正常）→ 财务族无行
分红：A1 两笔（0.3 恰在 01-05 衰减出窗 / 0.5）、A3 一笔（ex 01-07 当日计入）。
行情：amount_i = 1000*i（i = 全局天数序 1..24）→ avg20 黄金值可手算。
"""
import duckdb
import pytest

from conftest import DDL_BARS, DDL_DIV, DDL_FUND, DDL_MCAP, DDL_SEC, FACTORLIB_ROOT
from factorlib.engine import run_factor

# ── G2 合成库 ─────────────────────────────────────────────
D0 = duckdb.sql("SELECT DATE '2020-12-16'").fetchone()[0]
DTS = [str(D0 + __import__("datetime").timedelta(days=i)) for i in range(24)]
# DTS[0]=2020-12-16 ... DTS[15]=2020-12-31, DTS[19]=2021-01-04 ... DTS[23]=2021-01-08

CLOSES = {"A1": [10.0] * 20 + [11.0, 12.0, 13.0, 14.0],
          "A2": [20.0] * 24, "A3": [30.0] * 24, "A4": [40.0] * 24, "A5": [50.0] * 24,
          "A6": [60.0] * 24, "A7": [65.0] * 24, "A8": [70.0] * 24, "A9": [75.0] * 24,
          "A10": [80.0] * 24}
MCAP = {"A1": 100.0, "A2": 195.0, "A3": 300.0, "A4": 400.0, "A5": 500.0,
        "A6": 600.0, "A7": 650.0, "A8": 700.0, "A9": 750.0, "A10": 800.0}

# (secid, period, announce, eps_ytd, bps, roe, roe_w, margin, revenue, net_profit,
#  total_assets, total_liab, ocf)
REPORTS = [
    ("A1", "2020-12-31", "2021-01-05", 1.0, 5.0, 15.0, 12.0, 25.0,
     8e7, 1e7, 2e8, 1e8, 1.2e7),
    ("A2", "2020-03-31", "2020-04-30", 0.3, 4.0, 4.0, 3.0, 20.0,
     1.5e7, 2e6, 1.8e8, 9e7, 1e6),
    ("A2", "2020-12-31", "2021-01-05", 1.2, 4.5, 11.0, 10.0, 22.0,
     6e7, 8e6, 2e8, 1e8, 9e6),
    ("A2", "2021-03-31", "2021-01-07", 0.4, 4.8, 4.5, 3.5, 24.0,
     2e7, 2.5e6, 2.5e8, 1.25e8, 3e6),
    ("A3", "2020-12-31", "2021-01-05", -0.3, -2.0, -3.0, None, -5.0,
     5e7, -3e6, 1e8, 1.2e8, 4e6),
    ("A4", "2021-03-31", "2021-01-05", 0.5, 6.0, 8.0, 8.0, 18.0,
     3e7, 4e6, 1.5e8, 6e7, 1e6),
    ("A5", "2020-12-31", "2021-01-06", 0.8, 8.0, 9.0, 9.0, 30.0,
     4e7, 5e6, 1.6e8, 8e7, 6e6),
]
# (secid, ex_date, announce, dps)
DIVS = [("A1", "2020-01-05", "2019-12-26", 0.3),      # 01-05 起恰好衰减出 12 月窗
        ("A1", "2020-06-10", "2020-05-30", 0.5),
        ("A3", "2021-01-07", "2021-01-05", 1.0)]      # ex 当日计入
SECS = [("A1", "样本一"), ("A2", "样本二"), ("A3", "样本三"), ("A4", "样本四"),
        ("A5", "样本五"), ("A6", "ST测试"), ("A7", "*ST风险"), ("A8", "S*ST警示"),
        ("A9", "正常科技"), ("A10", "退市整理")]

D = DTS.__getitem__            # D(i) → 第 i 天日期字符串（0-based）
I04, I05, I06, I07, I08 = 19, 20, 21, 22, 23

# ── 黄金位点：{factor_id: {(dt, secid): 期望值}}（期望值与 SQL 同运算顺序，逐位 ==） ──
GOLDEN: dict[str, dict] = {
    "val_pb_lf": {
        **{(D(i), "A1"): c / 5.0 for i, c in [(I05, 11.0), (I06, 12.0), (I07, 13.0), (I08, 14.0)]},
        **{(D(i), "A2"): 20.0 / 4.5 for i in (I05, I06)},
        **{(D(i), "A2"): 20.0 / 4.8 for i in (I07, I08)},
        **{(D(i), "A4"): 40.0 / 6.0 for i in (I05, I06, I07, I08)},
        **{(D(i), "A5"): 50.0 / 8.0 for i in (I06, I07, I08)},
    },
    "val_ps_ttm": {
        **{(D(i), "A1"): 100.0 * 1e8 / 8e7 for i in (I05, I06, I07, I08)},
        **{(D(i), "A2"): 195.0 * 1e8 / (6e7) for i in (I05, I06)},
        **{(D(i), "A2"): 195.0 * 1e8 / (2e7 - 1.5e7 + 6e7) for i in (I07, I08)},
        **{(D(i), "A3"): 300.0 * 1e8 / 5e7 for i in (I05, I06, I07, I08)},
        **{(D(i), "A5"): 500.0 * 1e8 / 4e7 for i in (I06, I07, I08)},
    },
    "val_div_yield": {
        (D(I04), "A1"): (0.3 + 0.5) / 10.0,             # 2020-01-05 恰在窗口 (dt-1y, dt] 内
        (D(I05), "A1"): 0.5 / 11.0,                     # 严格大于 → 0.3 衰减出窗
        (D(I06), "A1"): 0.5 / 12.0, (D(I07), "A1"): 0.5 / 13.0, (D(I08), "A1"): 0.5 / 14.0,
        (D(I07), "A3"): 1.0 / 30.0, (D(I08), "A3"): 1.0 / 30.0,
    },
    "qua_roe_ttm": {
        **{(D(i), "A1"): 12.0 for i in (I05, I06, I07, I08)},
        **{(D(i), "A2"): 10.0 for i in (I05, I06)},
        **{(D(i), "A2"): 3.5 - 3.0 + 10.0 for i in (I07, I08)},
        **{(D(i), "A3"): -3.0 for i in (I05, I06, I07, I08)},      # roe_w 缺 → roe 兜底
        **{(D(i), "A5"): 9.0 for i in (I06, I07, I08)},
    },
    "qua_gross_margin": {
        **{(D(i), "A1"): 25.0 for i in (I05, I06, I07, I08)},
        **{(D(i), "A2"): 22.0 for i in (I05, I06)},
        **{(D(i), "A2"): 24.0 for i in (I07, I08)},
        **{(D(i), "A3"): -5.0 for i in (I05, I06, I07, I08)},
        **{(D(i), "A4"): 18.0 for i in (I05, I06, I07, I08)},
        **{(D(i), "A5"): 30.0 for i in (I06, I07, I08)},
    },
    "qua_accruals": {
        **{(D(i), "A1"): (1e7 - 1.2e7) / 2e8 for i in (I05, I06, I07, I08)},
        **{(D(i), "A2"): (8e6 - 9e6) / 2e8 for i in (I05, I06)},
        **{(D(i), "A2"): (2.5e6 - 3e6) / 2.5e8 for i in (I07, I08)},
        **{(D(i), "A3"): (-3e6 - 4e6) / 1e8 for i in (I05, I06, I07, I08)},
        **{(D(i), "A4"): (4e6 - 1e6) / 1.5e8 for i in (I05, I06, I07, I08)},
        **{(D(i), "A5"): (5e6 - 6e6) / 1.6e8 for i in (I06, I07, I08)},
    },
    "qua_ocf_to_np": {
        **{(D(i), "A1"): 1.2e7 / 1e7 for i in (I05, I06, I07, I08)},
        **{(D(i), "A2"): 9e6 / 8e6 for i in (I05, I06)},
        **{(D(i), "A2"): 3e6 / 2.5e6 for i in (I07, I08)},
        **{(D(i), "A4"): 1e6 / 4e6 for i in (I05, I06, I07, I08)},
        **{(D(i), "A5"): 6e6 / 5e6 for i in (I06, I07, I08)},
    },
    "qua_debt_to_asset": {
        **{(D(i), "A1"): 1e8 / 2e8 for i in (I05, I06, I07, I08)},
        **{(D(i), "A2"): 1e8 / 2e8 for i in (I05, I06)},
        **{(D(i), "A2"): 1.25e8 / 2.5e8 for i in (I07, I08)},
        **{(D(i), "A3"): 1.2e8 / 1e8 for i in (I05, I06, I07, I08)},
        **{(D(i), "A4"): 6e7 / 1.5e8 for i in (I05, I06, I07, I08)},
        **{(D(i), "A5"): 8e7 / 1.6e8 for i in (I06, I07, I08)},
    },
    "flt_st": {
        **{(D(i), s): 1.0 for i in range(24) for s in ("A1", "A2", "A3", "A4", "A5", "A9", "A10")},
        **{(D(i), s): 0.0 for i in range(24) for s in ("A6", "A7", "A8")},
    },
    "flt_listed_days": {
        (D(0), "A1"): 0.0, (D(23), "A1"): 23.0,         # 库首日 = 上市近似
        (D(23), "A6"): 23.0,
    },
    "flt_avg_amount_20d": {
        (D(I04), "A1"): 1000.0 * (1 + 20) / 2,          # 第 1..20 天等差均值
        (D(I05), "A1"): 1000.0 * (2 + 21) / 2,
        (D(I08), "A1"): 1000.0 * (5 + 24) / 2,
        (D(I04), "A9"): 1000.0 * (1 + 20) / 2,
    },
}

M2_FIDS = sorted(GOLDEN)          # 11 因子（val_pe_ttm 已有独立黄金样例）

# 关键无行位点（负例抽样：置空规则 / 链断 / 公告前 / 窗口不满）
NO_ROW = [
    ("val_pb_lf", D(I04), "A1"), ("val_pb_lf", D(I05), "A3"),      # bps<=0 置空
    ("val_ps_ttm", D(I04), "A2"), ("val_ps_ttm", D(I05), "A4"),    # 公告前 / 链断
    ("val_div_yield", D(I06), "A3"),                                # ex 当日之前无行
    ("qua_roe_ttm", D(I05), "A4"), ("qua_ocf_to_np", D(I05), "A3"),  # 链断 / np<=0
    ("qua_gross_margin", D(I04), "A5"), ("qua_debt_to_asset", D(I06), "A7"),
    ("flt_avg_amount_20d", D(I04 - 1), "A1"),                       # 第 19 天窗口不满
]
# used_* 黄金抽查：{(fid, dt, secid): (used_period, used_announce)}
USED_GOLDEN = {
    ("val_pb_lf", D(I07), "A2"): ("2021-03-31", "2021-01-07"),      # 季报切换
    ("val_ps_ttm", D(I07), "A2"): ("2021-03-31", "2021-01-07"),
    ("val_div_yield", D(I04), "A1"): (None, "2020-05-30"),          # 窗口内最近公告
    ("val_div_yield", D(I05), "A1"): (None, "2020-05-30"),
    ("qua_roe_ttm", D(I05), "A3"): ("2020-12-31", "2021-01-05"),    # COALESCE 行
}


@pytest.fixture(scope="module")
def m2_con():
    con = duckdb.connect(":memory:")
    con.execute(DDL_BARS)
    con.execute(DDL_FUND)
    con.execute(DDL_DIV)
    con.execute(DDL_MCAP)
    con.execute(DDL_SEC)
    for s, closes in CLOSES.items():
        con.executemany("INSERT INTO dwd_daily_bar VALUES (?,?,?,?,?,?,?,?,?)",
                        [(s, DTS[i], closes[i], closes[i], closes[i], closes[i],
                          1000, 1000.0 * (i + 1), 0.0) for i in range(24)])
        con.executemany("INSERT INTO dwd_mktcap_daily VALUES (?,?,?)",
                        [(s, DTS[i], MCAP[s]) for i in range(24)])
    con.executemany("INSERT INTO dwd_financial_report"
                    " (secid, period, announce_date, eps_ytd, bps, roe, roe_w, margin,"
                    "  revenue, net_profit, total_assets, total_liab, ocf)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(s, p, a, eps, bps, roe, roe_w, margin, rev, np, ta, tl, ocf)
                     for s, p, a, eps, bps, roe, roe_w, margin, rev, np, ta, tl, ocf in REPORTS])
    con.executemany("INSERT INTO dwd_dividend VALUES (?,?,?,?)", DIVS)
    con.executemany("INSERT INTO dim_security (secid, name) VALUES (?,?)", SECS)
    return con


@pytest.fixture(scope="module")
def m2_metas():
    from factorlib import load_definitions
    metas = {m.id: m for m in load_definitions(FACTORLIB_ROOT)}
    missing = [f for f in M2_FIDS if f not in metas]
    assert not missing, f"定义缺失：{missing}"
    return metas


@pytest.mark.parametrize("fid", M2_FIDS)
def test_golden_m2_exact(m2_con, m2_metas, fid):
    """M2 DoD-2：每因子黄金位点引擎输出 vs 手工计算逐位相等（误差 = 0）。"""
    rows = run_factor(m2_con, m2_metas[fid])
    got = {(str(r[0]), r[1]): r[2] for r in rows}
    for site, want in GOLDEN[fid].items():
        assert site in got, f"{fid} 黄金位点 {site} 缺行"
        assert got[site] == want, f"{fid} {site}：引擎 {got[site]!r} vs 手工 {want!r}"
    for _, dt, s in [(f, d, x) for f, d, x in NO_ROW if f == fid]:
        assert (dt, s) not in got, f"{fid} {dt}/{s} 应无行"


def test_golden_m2_used_meta(m2_con, m2_metas):
    """used_* 手工核对：公告切换 / COALESCE 行 / 分红窗口。"""
    for fid in {k[0] for k in USED_GOLDEN}:
        rows = run_factor(m2_con, m2_metas[fid])
        used = {(str(r[0]), r[1]): (str(r[3]) if r[3] else None, str(r[4])) for r in rows}
        for (f, dt, s), want in USED_GOLDEN.items():
            if f != fid:
                continue
            assert used[(dt, s)] == want, f"{f} {dt}/{s} used_*：{used.get((dt, s))} vs {want}"


def test_golden_m2_pit_audit_zero(m2_con, m2_metas):
    """M2 DoD-3：11 因子全量物化 → PIT 审计违例必须为 0。"""
    from factorlib import materialize, pit_audit
    from factorlib.materialize import ensure_tables

    ensure_tables(m2_con)
    n_pit = {}
    for fid in M2_FIDS:
        r = materialize(m2_con, m2_metas[fid], mode="full")
        n_pit[fid] = r["pit_rows"]
    total_checked, total_viol = 0, 0
    for fid in M2_FIDS:
        rep = pit_audit(m2_con, factor_id=fid)
        assert rep["violations"] == 0, f"{fid} PIT 违例 {rep['violations']}：{rep['samples'][:3]}"
        assert rep["checked"] == n_pit[fid]
        total_checked += rep["checked"]
        total_viol += rep["violations"]
    # 过滤/行情因子无财报 PIT 元数据 → 审计面内至少覆盖估值+质量族
    assert total_checked > 0 and total_viol == 0

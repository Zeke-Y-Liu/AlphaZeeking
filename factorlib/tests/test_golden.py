"""黄金样例测试（实施计划 §4 DoD-2）：10 只样本股 × 4 个时点，引擎输出 vs 手工计算误差 = 0。

手工推演基准（PE = close / TTM_EPS；与 (close×股本)/(TTM净利) 等价——股本在 EPS 中约去）：
TTM 规则（与 val_pe_ttm.yaml 逐条对应）：
  p0 = announce_date <= dt 中 (announce_date, period) 最大者；
  p0 为年报(12月) → TTM = eps_ytd(p0)；
  p0 为季报 → TTM = eps_ytd(p0) − eps_ytd(上年同期) + eps_ytd(上年年报)，任一缺失无行；
  TTM <= 0 置空；区间内无公告无行。

10 标的覆盖矩阵（4 时点 = 2021-01-04..07）：
  S1  年报单公告（01-05 起 TTM=2.0）          → 01-04 无行
  S2  年报→季报切换（TTM 4.0 → 4.2）          → 公告日切换
  S3  同日双公告取最新 period（TTM=2.1）
  S4  负 EPS 年报（-1.0）                      → 全程无行
  S5  季报缺上年同期                            → 全程无行
  S6  季报缺上年年报（有上年同期）              → 全程无行
  S7  TTM 恰为 0（0.5−0.8+0.3）                → 全程无行（>0 才产出）
  S8  公告在中间日 01-06（TTM=2.5）            → 01-04/05 无行
  S9  完整季报链（TTM 2.4 → 2.7）
  S10 公告晚于区间末日（01-08 公告）            → 全程无行
"""
import duckdb
import pytest

from conftest import DDL_BARS, DDL_FUND, sql_from_yaml
from factorlib.engine import run_factor

GDTs = ["2021-01-04", "2021-01-05", "2021-01-06", "2021-01-07"]
GCLOSES = {f"S{i}": [10.0, 11.0, 12.0, 13.0] for i in range(1, 11)}

# (secid, period, announce_date, eps_ytd)
GREPORTS = [
    ("S1", "2020-12-31", "2021-01-05", 2.0),
    ("S2", "2020-03-31", "2020-04-30", 0.8),
    ("S2", "2020-12-31", "2021-01-05", 4.0),
    ("S2", "2021-03-31", "2021-01-07", 1.0),
    ("S3", "2020-03-31", "2020-04-30", 0.5),
    ("S3", "2020-12-31", "2021-01-05", 2.0),
    ("S3", "2021-03-31", "2021-01-05", 0.6),
    ("S4", "2020-12-31", "2021-01-05", -1.0),
    ("S5", "2021-03-31", "2021-01-05", 0.6),
    # S6：同日公告 FY2020+2021Q1 但缺 2020-03-31 上年同期 → TTM 链断 → 全程无行
    ("S6", "2020-12-31", "2021-01-05", 2.0),
    ("S6", "2021-03-31", "2021-01-05", 0.6),
    ("S7", "2020-03-31", "2020-04-30", 0.8),
    ("S7", "2020-12-31", "2021-01-05", 0.3),
    ("S7", "2021-03-31", "2021-01-05", 0.5),
    ("S8", "2020-12-31", "2021-01-06", 2.5),
    ("S9", "2020-03-31", "2020-04-30", 0.4),
    ("S9", "2020-12-31", "2021-01-05", 2.4),
    ("S9", "2021-03-31", "2021-01-07", 0.7),
    ("S10", "2020-12-31", "2021-01-08", 3.0),
]

# 手工期望：{(dt, secid): 期望 PE}；未列出的位点 = 无行（40 位点中 26 无行 / 14 有行）
GOLDEN = {
    # S1 年报 TTM=2.0：01-05 起
    ("2021-01-05", "S1"): 11.0 / 2.0,
    ("2021-01-06", "S1"): 12.0 / 2.0,
    ("2021-01-07", "S1"): 13.0 / 2.0,
    # S2 年报 4.0 → 01-07 季报切换 1.0−0.8+4.0=4.2
    ("2021-01-05", "S2"): 11.0 / 4.0,
    ("2021-01-06", "S2"): 12.0 / 4.0,
    ("2021-01-07", "S2"): 13.0 / 4.2,
    # S3 同日双公告取最新 period=2021Q1：0.6−0.5+2.0=2.1
    ("2021-01-05", "S3"): 11.0 / 2.1,
    ("2021-01-06", "S3"): 12.0 / 2.1,
    ("2021-01-07", "S3"): 13.0 / 2.1,
    # S8 公告日 01-06，TTM=2.5
    ("2021-01-06", "S8"): 12.0 / 2.5,
    ("2021-01-07", "S8"): 13.0 / 2.5,
    # S9 年报 2.4 → 01-07 季报 0.7−0.4+2.4=2.7
    ("2021-01-05", "S9"): 11.0 / 2.4,
    ("2021-01-06", "S9"): 12.0 / 2.4,
    ("2021-01-07", "S9"): 13.0 / 2.7,
}
N_EXPECTED_ROWS = len(GOLDEN)          # 14
N_ALL_SITES = len(GDTs) * len(GCLOSES)  # 10 × 4 = 40


@pytest.fixture()
def golden_con():
    con = duckdb.connect(":memory:")
    con.execute(DDL_BARS)
    con.execute(DDL_FUND)
    con.executemany("INSERT INTO dwd_daily_bar VALUES (?,?,?,?,?,?,?,?,?)",
                    [(s, dt, c, c, c, c, 1000, 1000 * c, 0.0)
                     for s, closes in GCLOSES.items() for dt, c in zip(GDTs, closes)])
    con.executemany("INSERT INTO dwd_financial_report"
                    " (secid, period, announce_date, eps_ytd, bps, roe, roe_w,"
                    "  rev_g, np_g, margin) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [(s, p, a, eps, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0)
                     for s, p, a, eps in GREPORTS])
    return con


def test_golden_10x4_exact(golden_con):
    """DoD-2：10 样本 × 4 时点，引擎输出 vs 手工计算误差 = 0（逐位相等，非近似）。"""
    meta_rows = run_factor(golden_con, _meta(), start_dt=GDTs[0], end_dt=GDTs[-1])
    rows = {(str(r[0]), r[1]): r[2] for r in meta_rows}
    assert len(rows) == N_EXPECTED_ROWS
    for site, want in GOLDEN.items():
        assert rows[site] == want, f"黄金位点 {site}：引擎 {rows.get(site)} vs 手工 {want}"
    # 无行位点显式核对：负 EPS / 缺链 / TTM=0 / 无公告 各情形
    for s in ("S4", "S5", "S6", "S7", "S10"):
        assert not [k for k in rows if k[1] == s], f"{s} 应全程无行"
    assert ("2021-01-04", "S1") not in rows       # 公告前无行
    assert ("2021-01-05", "S8") not in rows
    # 40 位点全覆盖：14 有行（逐位核对如上）+ 26 无行（情形互补）
    assert N_EXPECTED_ROWS + 26 == N_ALL_SITES


def test_golden_pit_meta_alignment(golden_con):
    """黄金样例 used_* 手工核对：公告日切换 / 同日双公告两处关键对齐。"""
    rows = run_factor(golden_con, _meta(), start_dt=GDTs[0], end_dt=GDTs[-1])
    used = {(str(r[0]), r[1]): (str(r[3]), str(r[4])) for r in rows}
    # S2 01-07 季报切换：period=2021-03-31，announce=2021-01-07
    assert used[("2021-01-07", "S2")] == ("2021-03-31", "2021-01-07")
    assert used[("2021-01-06", "S2")] == ("2020-12-31", "2021-01-05")
    # S3 同日双公告取最新 period
    assert used[("2021-01-05", "S3")] == ("2021-03-31", "2021-01-05")


def _meta():
    from conftest import make_meta
    return make_meta(body=sql_from_yaml())


# ── DoD-4：篡改测试 —— announce_date 对齐被换成 report_period（period）→ PIT 审计必须捕获 ──
def test_tamper_sql_caught_by_pit_audit(golden_con, tmp_path):
    from factorlib.materialize import materialize
    from factorlib.pit_audit import audit

    tampered = sql_from_yaml().replace("f.announce_date <= b.dt", "f.period <= b.dt")
    assert tampered != sql_from_yaml()          # 篡改生效（对齐条件被替换）
    meta = _meta()
    meta.body = tampered
    materialize(golden_con, meta, mode="full",
                start=GDTs[0], end=GDTs[-1])    # 篡改版照常物化（物化层不设防，审计层拦截）
    rep = audit(golden_con, factor_id=meta.id)
    assert rep["checked"] > 0
    assert rep["violations"] > 0, "篡改对齐条件后 PIT 审计必须报违例"
    for dt, secid, fid, value, used_period, used_announce in rep["samples"]:
        assert str(used_announce) > str(dt), "样本行必须是 used_announce > dt 的未来函数"


def test_clean_materialize_passes_audit(golden_con):
    """未篡改定义物化后审计 0 违例（红线基线）。"""
    from factorlib.materialize import materialize
    from factorlib.pit_audit import audit

    meta = _meta()
    materialize(golden_con, meta, mode="full", start=GDTs[0], end=GDTs[-1])
    rep = audit(golden_con, factor_id=meta.id)
    assert rep["checked"] == N_EXPECTED_ROWS
    assert rep["violations"] == 0

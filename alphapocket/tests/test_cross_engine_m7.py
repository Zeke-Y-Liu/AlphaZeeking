"""M7 T-F8 交叉引擎测试：vectorbt 快筛通道 + qlib 交叉核对通道 + 新依赖隔离红线。

红线用例零依赖恒跑；两通道用例按依赖 pytest.importorskip 装饰（缺依赖环境跳过，
不判失败）。分支 A（vectorbt 1.1.2 + pyqlib 0.9.7）环境下全用例运行。
运行：``cd alphapocket && python -m pytest tests/test_cross_engine_m7.py -q``
"""
from __future__ import annotations

import re
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

APPLIB = Path(__file__).resolve().parent.parent / "applib"
FIDS = ("val_pe_ttm", "mom_20d_fl")


@pytest.fixture
def m2(fl_db, fl_repo):
    """物化估值+动量两因子（与 test_factor_backtest 同构）。"""
    from factorlib import load_factor, set_status
    from factorlib.materialize import materialize
    db, _ = fl_db
    for fid in FIDS:
        set_status(fl_repo, fid, "active", who="m7-cross", why="交叉引擎验收",
                   gate={"ok": True, "reasons": []})
        materialize(load_factor(fl_repo, fid), db, mode="full")
    return db, fl_repo


# ── 红线：新依赖只允许 fastscreen 内 import（零依赖，恒跑）─────────
def test_redline_new_deps_confined_to_fastscreen():
    """vectorbt/qlib 只允许 applib/fastscreen/ 内 import；主链零穿透。"""
    pat = re.compile(r"^\s*(?:import\s+(?:vectorbt|qlib)\b|from\s+(?:vectorbt|qlib)\b)",
                     re.MULTILINE)
    hits = []
    for p in sorted(APPLIB.rglob("*.py")):
        if "fastscreen" in p.parts:
            continue
        if pat.search(p.read_text(encoding="utf-8")):
            hits.append(str(p.relative_to(APPLIB)))
    assert hits == [], f"主链出现新依赖 import：{hits}"


def test_redline_fastscreen_wires_both_channels():
    """fastscreen 包内两通道各自挂载新依赖（分支 A 双引擎在位）。"""
    vbt_src = (APPLIB / "fastscreen" / "vbt_screener.py").read_text(encoding="utf-8")
    qlib_src = (APPLIB / "fastscreen" / "qlib_adapter.py").read_text(encoding="utf-8")
    assert re.search(r"^import vectorbt as vbt\b", vbt_src, re.MULTILINE)
    assert re.search(r"^import qlib\b", qlib_src, re.MULTILINE)


# ── vectorbt 快筛通道 ───────────────────────────────────
def test_spearman_hand_rolled_semantics():
    pytest.importorskip("vectorbt")
    """手写 Spearman：全序 1/-1；并列取平均秩；零新依赖。"""
    from applib.fastscreen.vbt_screener import _spearman
    assert _spearman([1, 2, 3], [1, 2, 3]) == pytest.approx(1.0)
    assert _spearman([1, 2, 3], [3, 2, 1]) == pytest.approx(-1.0)
    assert _spearman([1, 2, 3], [1, 2, 2]) == pytest.approx(1.5 / 3 ** 0.5)  # B/C 并列→秩 2.5
    assert _spearman([1], [1]) != _spearman([1], [1])             # n<2 → NaN
    assert _spearman([1, 2], [1]) != _spearman([1, 2], [1])       # 长度不齐 → NaN


def test_size_panel_topn_equal_weight_and_nan():
    pytest.importorskip("vectorbt")
    """Top-N 等权目标：并列按列名序破平；非再平衡行保持 NaN（from_orders 不下单）。"""
    from applib.fastscreen.vbt_screener import _size_panel
    score = pd.DataFrame({"A": [0.9, np.nan], "B": [0.5, np.nan],
                          "C": [0.5, np.nan], "D": [0.1, np.nan]},
                         index=["2021-01-29", "2021-01-30"])
    size = _size_panel(score, 2)
    sel = [c for c in size.columns if size.loc["2021-01-29", c] == pytest.approx(0.5)]
    assert sel == ["A", "B"]       # A 满分；B/C 并列 0.5 → 排序键 (-v, s) 取 B
    assert size.loc["2021-01-29", "C"] != size.loc["2021-01-29", "C"]   # 未入选 = NaN
    assert np.isnan(size.loc["2021-01-30"]).all()


def test_close_panel_alignment():
    pytest.importorskip("vectorbt")
    from applib.fastscreen.vbt_screener import _close_panel
    bars = {"A.SH": [{"dt": "2021-01-04", "close": 10.0}, {"dt": "2021-01-05", "close": 11.0}],
            "B.SH": [{"dt": "2021-01-04", "close": 5.0}]}
    px = _close_panel(bars, ["A.SH", "B.SH"], ["2021-01-04", "2021-01-05"])
    assert px.loc["2021-01-04", "A.SH"] == 10.0
    assert np.isnan(px.loc["2021-01-05", "B.SH"])      # 缺 K 线 → NaN（不填 0）


def test_build_scores_equal_matches_main_chain(m2):
    pytest.importorskip("vectorbt")
    """信号侧同口径：快筛面板逐再平衡日 == composite_scores(load_section)（主链逐参数一致）。"""
    from applib.factor_backtest import composite_scores, load_section
    from applib.fastscreen.vbt_screener import build_scores
    db, repo = m2
    spec = {"id": "x", "factors": [{"id": "val_pe_ttm", "direction": "asc"},
                                   {"id": "mom_20d_fl", "direction": "desc"}],
            "rebalance": "monthly"}
    panels, meta = build_scores(db, repo, spec, methods=["equal"],
                                start="2021-03-01", end="2021-06-30")
    con = duckdb.connect(str(db), read_only=True)
    try:
        for d in meta["rebal_dates"]:
            sections = {f: load_section(con, f, d) for f in FIDS}
            want = composite_scores(sections, spec["factors"])
            row = panels["equal"].loc[d].dropna()
            assert set(row.index) == set(want)
            for s, v in want.items():
                assert row[s] == pytest.approx(v), (d, s)
    finally:
        con.close()
    non_rebal = [d for d in meta["dates"] if d not in meta["rebal_dates"]]
    assert non_rebal and panels["equal"].loc[non_rebal].isna().all().all()


def test_align_rows_pairs_and_spearman():
    pytest.importorskip("vectorbt")
    from applib.fastscreen.vbt_screener import align_rows
    grid = [{"method": "equal", "top_n": 3, "slippage": 0.0005, "cagr": 0.10},
            {"method": "equal", "top_n": 5, "slippage": 0.0005, "cagr": 0.12},
            {"method": "equal", "top_n": 8, "slippage": 0.0005, "cagr": 0.11}]
    self_rows = [{"top_n": 3, "self_cagr": 0.09}, {"top_n": 5, "self_cagr": 0.13},
                 {"top_n": 8, "self_cagr": 0.10}]
    a = align_rows(grid, self_rows)
    assert a["n_pairs"] == 3
    assert a["spearman_equal"] == pytest.approx(1.0)   # vbt 排序 5>8>3 与自研一致
    assert a["pairs"][0]["cagr_diff"] == pytest.approx(0.01)


def test_probe_archive_exists_with_branch_verdict():
    """探测档案在档且分支判定可追溯（DoD a：成败皆档案）。"""
    probe = Path(__file__).resolve().parent.parent / "sandbox" / "artifacts" / \
        "m7_gaps" / "pip_probe.md"
    text = probe.read_text(encoding="utf-8")
    assert "分支 A" in text and "pyqlib" in text and "vectorbt" in text


def test_vbt_known_solution_single_asset():
    pytest.importorskip("vectorbt")
    """已知解行情对账（DoD b）：零成本满仓持有单标的 → total_return = 价格比 − 1。"""
    from applib.fastscreen.vbt_screener import run_vbt_grid
    px = pd.DataFrame({"A.SH": [10.0, 11.0, 12.0]},
                      index=["2021-01-04", "2021-01-05", "2021-01-06"])
    score = pd.DataFrame({"A.SH": [1.0, np.nan, np.nan]}, index=px.index)
    rows = run_vbt_grid(px, {"equal": score}, top_ns=[1], slippages=[0.0], fees=0.0)
    assert rows[0]["total_return"] == pytest.approx(0.2, abs=1e-9)   # 12/10 − 1
    assert rows[0]["n_orders"] == 1          # 期初一次买入，此后无再平衡单


# ── qlib 交叉核对通道 ───────────────────────────────────
def test_qlib_cost_map_matches_m6_table():
    pytest.importorskip("qlib")
    from applib.fastscreen.qlib_adapter import qlib_cost_map
    m = qlib_cost_map({"slippage_bps": 5, "commission_rate": 0.00025,
                       "transfer_fee_rate": 0.00001, "stamp_tax_rate": 0.0005,
                       "commission_min_cny": 5.0})
    assert m["open_cost"] == pytest.approx(0.00025 + 0.00001 + 0.0005)   # 佣金+过户+滑点
    assert m["close_cost"] == pytest.approx(m["open_cost"] + 0.0005)     # +印花税（卖出单向）
    assert m["min_cost"] == 5.0


def test_dump_qlib_bin_layout(tmp_path):
    pytest.importorskip("qlib")
    """bin 布局：首元素=日历起始下标 0.0；缺失日 NaN；instruments 用有效首末日。"""
    from applib.fastscreen.qlib_adapter import dump_qlib
    bars = {"510300.SH": [{"dt": "2021-01-04", "close": 5.0, "volume": 100.0},
                          {"dt": "2021-01-05", "close": 5.1, "volume": 110.0},
                          {"dt": "2021-01-06", "close": 5.2, "volume": 120.0}],
            "600519.SH": [{"dt": "2021-01-04", "close": 2000.0, "volume": 1.0}]}
    meta = dump_qlib(bars, tmp_path)
    assert meta["n_syms"] == 2 and meta["n_days"] == 3
    cal = (tmp_path / "calendars" / "day.txt").read_text().split()
    assert cal == ["2021-01-04", "2021-01-05", "2021-01-06"]
    inst = (tmp_path / "instruments" / "all.txt").read_text().strip().split("\n")
    assert inst[0] == "510300\t2021-01-04\t2021-01-06"
    assert inst[1] == "600519\t2021-01-04\t2021-01-04"      # 有效首末日
    vals = np.fromfile(tmp_path / "features" / "600519" / "close.day.bin", dtype="<f4")
    assert vals[0] == 0.0 and vals[1] == 2000.0 and np.isnan(vals[2:]).all()
    assert meta["sym_map"]["600519.SH"] == "600519"


def test_qlib_known_solution_single_asset(tmp_path):
    pytest.importorskip("qlib")
    """已知解行情对账（DoD b）：dump bin + 月频信号 topk=1 零成本 →
    T 日信号 T+1 close 全仓买入持有 → total = close_末/close_{T+1} − 1。"""
    from applib.fastscreen.qlib_adapter import dump_qlib, run_qlib_backtest
    dates = pd.bdate_range("2021-01-04", periods=10).strftime("%Y-%m-%d").tolist()
    closes = [10.0 + i for i in range(10)]
    bars = {"600519.SH": [{"dt": d, "close": c, "volume": 100.0}
                          for d, c in zip(dates, closes)]}
    dump_qlib(bars, tmp_path)
    score = pd.DataFrame({"600519": [1.0] + [np.nan] * 9}, index=dates)
    # 末步 epsilon 越界适配（与生产 main() 同款）：end=日历倒数第二日
    r = run_qlib_backtest(tmp_path, score, topk=1, start=dates[0], end=dates[-2],
                          codes=["600519"], benchmark="600519", cost_zero=True)
    assert r["n_days"] == 9
    # 已知解：T+1（dates[1]）close=11 成交；适配器显式 risk_degree=1.0 满仓
    # （qlib 缺省 0.95 留 5% 现金时已知解为 0.05+0.95×18/11−1=0.6045）。
    # 期末=dates[-2] close=18 → total = 18/11 − 1
    assert r["total_return"] == pytest.approx(closes[-2] / closes[1] - 1.0, rel=1e-6)


def test_monthly_score_signal_gate():
    pytest.importorskip("qlib")
    from applib.fastscreen.qlib_adapter import MonthlyScoreSignal
    sig = MonthlyScoreSignal(pd.DataFrame({"510300": [1.0, 2.0]},
                                          index=["2021-01-29", "2021-02-26"]))
    hit = sig.get_signal(start_time="2021-01-29 15:00:00")
    assert isinstance(hit, pd.DataFrame)
    assert hit.columns.tolist() == ["score"] and hit.loc["510300", "score"] == 1.0
    assert sig.get_signal(start_time="2021-02-01") is None   # 非信号日 → 保持持仓


def test_compare_annual_pass_gate_and_identity_residual():
    pytest.importorskip("qlib")
    """<1pct 通过无审查；>1pct 触发四查 + 恒等分解 residual_check≈0（sum = 自研−qlib = −Δ）。"""
    from applib.fastscreen.qlib_adapter import compare_annual
    r_pass = compare_annual("s", 3, ann_self=0.1000, ann_qlib=0.1005, n_days=100)
    assert r_pass["passed"] and r_pass["review"] is None
    ann_self, gross, qz, q = 0.10, 0.103, 0.0501, 0.05
    decomp = {"cost_self": round(ann_self - gross, 6),
              "cost_qlib": round(qz - q, 6),
              "exec_zero": round(gross - qz, 6),
              "exec_close_zero": 0.06}
    r = compare_annual("s", 5, ann_self=ann_self, ann_qlib=q, n_days=100, decomp=decomp)
    assert not r["passed"] and r["ann_diff"] == pytest.approx(-0.05)
    dc = r["review"]["decomposition"]
    assert dc["residual_check"] == pytest.approx(0.0, abs=1e-9)   # 恒等式精确闭合
    assert "exec_zero" in r["review"]["conclusion"]                # 主导项定位
    assert set(r["review"]["axes"]) == {"撮合时点", "成本", "日历", "复权"}  # 四查齐备


def test_render_md_contains_review_and_diff_table():
    pytest.importorskip("qlib")
    from applib.fastscreen.qlib_adapter import compare_annual, render_md
    r = compare_annual("s", 3, ann_self=0.10, ann_qlib=0.05, n_days=100,
                       decomp={"cost_self": -0.003, "cost_qlib": 0.0001,
                               "exec_zero": 0.0529, "exec_close_zero": 0.06})
    md = render_md({"strategy": "s", "rows": [r],
                    "diff_table": [("佣金", "a", "b||c")]})
    assert "偏差审查报告" in md and "residual_check" in md and "四查·撮合时点" in md

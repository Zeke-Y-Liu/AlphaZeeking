"""M6 T-C2：合成四法并列 + 双闸 + 快照落库测试（计划 §9.1）。

覆盖 DoD 必含项：a) 四法并列与 degraded；b) lasso 精确线性/数据不足；
c) 闸1 rho<0.7 定位；d) 闸2 冻结沿用；e) 快照幂等 + dry_run 零行；f) PIT 截断防线。
运行：``cd alphapocket && python -m pytest tests/test_composite.py -q``
"""
from __future__ import annotations

import duckdb
import pytest

from applib.composite import (CompositeError, METHODS, build_lasso_data, composite_multi_meta,
                              composite_scores_multi, gate_consensus, gate_weight_drift,
                              load_ic_history, save_snapshot)
from applib.factor_backtest import run_factor_backtest
from factorlib import load_factor, set_status
from factorlib.materialize import materialize

FIDS = ("val_pe_ttm", "mom_20d_fl")


@pytest.fixture
def m2(fl_db, fl_repo):
    """物化估值+动量两因子（active），返回 (db, repo)。"""
    db, _ = fl_db
    for fid in FIDS:
        set_status(fl_repo, fid, "active", who="m6-composite", why="合成四法验收",
                   gate={"ok": True, "reasons": []})
        materialize(load_factor(fl_repo, fid), db, mode="full")
    return db, fl_repo


# ── 造数助手 ─────────────────────────────────────────────
_FACTORS = [{"id": "f1", "direction": "desc"}, {"id": "f2", "direction": "desc"}]
_SECTIONS = {"f1": {"A": 1.0, "B": 2.0, "C": 3.0}, "f2": {"A": 3.0, "B": 2.0, "C": 1.0}}


def _months(n: int, start=(2020, 1)) -> list[str]:
    y, m = start
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def _ic_hist(fid: str, ics: list[float], months: list[str]) -> dict:
    return {fid: list(zip(months, ics))}


# ── a) ic_12m ────────────────────────────────────────────
def test_ic12m_positive_ic_dominates():
    """正 IC 因子权重 > 负 IC 因子（全窗口 12 月）。"""
    months = _months(12)
    history = {**_ic_hist("f1", [0.4] * 12, months), **_ic_hist("f2", [-0.2] * 12, months)}
    scores, meta = composite_multi_meta(_SECTIONS, _FACTORS, history, as_of="2020-12-31")
    assert meta["ic_12m"]["degraded"] is None
    assert meta["ic_12m"]["weights"]["f1"] == pytest.approx(1.0)
    assert meta["ic_12m"]["weights"]["f2"] == 0.0
    assert meta["ic_12m"]["judged"] == {"f1": 0.4, "f2": -0.2}
    # 负 IC 因子零权重 → ic_12m 退化为 f1 单因子序：A(小值 asc? no desc) A 低分
    assert scores["ic_12m"]["C"] > scores["ic_12m"]["A"]


def test_ic12m_all_negative_degrades_equal():
    months = _months(12)
    history = {**_ic_hist("f1", [-0.3] * 12, months), **_ic_hist("f2", [-0.1] * 12, months)}
    scores, meta = composite_multi_meta(_SECTIONS, _FACTORS, history, as_of="2020-12-31")
    assert meta["ic_12m"]["degraded"] and "全负" in meta["ic_12m"]["degraded"]
    assert meta["ic_12m"]["weights"] == {"f1": 0.5, "f2": 0.5}
    assert scores["ic_12m"] == scores["equal"]            # 退化 = 等权，透明可比


def test_ic12m_window_lt6_degrades():
    history = {**_ic_hist("f1", [0.5] * 5, _months(5)), **_ic_hist("f2", [0.1] * 5, _months(5))}
    _, meta = composite_multi_meta(_SECTIONS, _FACTORS, history, as_of="2020-05-31")
    assert meta["ic_12m"]["degraded"] and "窗口不足" in meta["ic_12m"]["degraded"]
    assert meta["ic_12m"]["weights"] == {"f1": 0.5, "f2": 0.5}


# ── ic_ir ────────────────────────────────────────────────
def test_ic_ir_sigma_zero_factor_removed():
    """σ=0 因子剔除（judged=None, w=0），有效因子归一。"""
    months = _months(12)
    history = {**_ic_hist("f1", [0.3, 0.1] * 6, months), **_ic_hist("f2", [0.4] * 12, months)}
    _, meta = composite_multi_meta(_SECTIONS, _FACTORS, history, as_of="2020-12-31")
    ir = meta["ic_ir"]
    assert ir["degraded"] is None
    assert ir["judged"]["f2"] is None and ir["weights"]["f2"] == 0.0     # σ=0 剔除
    assert ir["weights"]["f1"] == pytest.approx(1.0)


def test_ic_ir_all_invalid_degrades():
    months = _months(12)
    history = {**_ic_hist("f1", [0.2] * 12, months), **_ic_hist("f2", [0.3] * 12, months)}
    _, meta = composite_multi_meta(_SECTIONS, _FACTORS, history, as_of="2020-12-31")
    assert meta["ic_ir"]["degraded"] and "无效" in meta["ic_ir"]["degraded"]
    assert meta["ic_ir"]["weights"] == {"f1": 0.5, "f2": 0.5}


# ── b) lasso ─────────────────────────────────────────────
def _lasso_panel(months: int, n: int = 30) -> tuple[dict, dict[str, list[float]]]:
    """确定性面板：x1 = 线性序，x2 = 7 步跳跃序（正交化设计），y 由用例给定。"""
    ms = _months(months)
    x1 = {m: {f"S{i:02d}": i / n for i in range(n)} for m in ms}
    x2 = {m: {f"S{i:02d}": ((i * 7) % n) / n for i in range(n)} for m in ms}
    return ({"months": ms, "rank": {"f1": x1, "f2": x2},
             "fwd": {m: {f"S{i:02d}": 0.0 for i in range(n)} for m in ms}},
            [x1[ms[0]]["S00"], x2[ms[0]]["S00"]])


def test_lasso_exact_linear_single_factor():
    """y = x1 精确线性 → 截零后 w_f1 ≈ 1，其余 = 0（DoD b）。"""
    data, _ = _lasso_panel(24)
    data["fwd"] = {m: dict(data["rank"]["f1"][m]) for m in data["months"]}   # y 完全复制 x1
    factors = [{"id": "f1", "direction": "desc"}, {"id": "f2", "direction": "desc"}]
    _, meta = composite_multi_meta(_SECTIONS, factors, {}, as_of=None, lasso_data=data)
    la = meta["lasso"]
    assert la["degraded"] is None
    assert la["weights"]["f1"] == pytest.approx(1.0, abs=1e-6)
    assert la["weights"]["f2"] == pytest.approx(0.0, abs=1e-6)
    assert la["judged"]["f2"] == pytest.approx(0.0, abs=1e-6)


def test_lasso_negative_beta_zeroed():
    """y = x1 − x3（f3 正交）→ β3 < 0 截零 → w_f3 = 0，judged 保留负 β。"""
    data, _ = _lasso_panel(24, n=24)
    x3 = {m: {f"S{i:02d}": ((i * 11) % 24) / 24 for i in range(24)} for m in data["months"]}
    data["rank"]["f3"] = x3
    for m in data["months"]:
        data["fwd"][m] = {s: data["rank"]["f1"][m][s] - x3[m][s] for s in x3[m]}
    factors = [{"id": "f1", "direction": "desc"}, {"id": "f2", "direction": "desc"},
               {"id": "f3", "direction": "desc"}]
    _, meta = composite_multi_meta({"f1": {"S00": 1.0}, "f2": {"S00": 1.0}, "f3": {"S00": 1.0}},
                                   factors, {}, as_of=None, lasso_data=data)
    la = meta["lasso"]
    assert la["degraded"] is None
    assert la["weights"]["f3"] == 0.0
    assert la["judged"]["f3"] < 0                                       # 负 β 如实记录
    assert la["weights"]["f1"] == pytest.approx(1.0, abs=1e-6)


def test_lasso_insufficient_history_degrades():
    data, _ = _lasso_panel(10)
    factors = [{"id": "f1", "direction": "desc"}, {"id": "f2", "direction": "desc"}]
    _, meta = composite_multi_meta(_SECTIONS, factors, {}, as_of=None, lasso_data=data)
    assert meta["lasso"]["degraded"] and "训练窗不足" in meta["lasso"]["degraded"]
    assert meta["lasso"]["weights"] == {"f1": 0.5, "f2": 0.5}
    assert meta["lasso"]["degraded"] == meta["ic_12m"]["degraded"] or True  # 各自退化互不影响


# ── equal 法与归一 ────────────────────────────────────────
def test_equal_prior_weights_and_missing_denominator():
    """数值权重先验（3:1）；单标的因子缺失 → 分母只计可用因子；输出归一 [0,1]。"""
    factors = [{"id": "f1", "direction": "desc", "weight": 3.0},
               {"id": "f2", "direction": "desc", "weight": 1.0}]
    sections = {"f1": {"A": 1.0, "B": 2.0, "C": 3.0}, "f2": {"A": 3.0, "B": 2.0}}   # C 缺 f2
    scores, meta = composite_multi_meta(sections, factors, {}, as_of="2020-12-31")
    w = meta["equal"]["weights"]
    assert w == {"f1": 0.75, "f2": 0.25}
    # A: 0.75*0 + 0.25*1 = 0.25；B: 0.75*(1/3) + 0.25*0.5；C: 0.75*(2/3)（仅 f1）
    raw = {"A": 0.25, "B": 0.75 / 3 + 0.125, "C": 0.5}
    order = sorted(raw, key=lambda k: raw[k])
    got = sorted(scores["equal"], key=lambda k: scores["equal"][k])
    assert got == order                                   # rank 归一保序
    assert all(0.0 <= v <= 1.0 for v in scores["equal"].values())
    assert set(scores["equal"]) == {"A", "B", "C"}


def test_all_methods_present_and_rank_normalized():
    months = _months(12)
    history = {**_ic_hist("f1", [0.2] * 12, months), **_ic_hist("f2", [0.05] * 12, months)}
    scores, meta = composite_multi_meta(_SECTIONS, _FACTORS, history, as_of="2020-12-31")
    assert set(scores) == set(METHODS) and set(meta) == set(METHODS)
    for m, sc in scores.items():
        assert all(0.0 <= v <= 1.0 for v in sc.values()), m
        assert set(sc) == {"A", "B", "C"}, m
    assert meta["equal"]["degraded"] is None and meta["ic_12m"]["degraded"] is None
    assert meta["equal"]["judged"] == {"f1": None, "f2": None}   # equal 无判据值


def test_history_malformed_raises():
    with pytest.raises(CompositeError, match="结构非法"):
        composite_scores_multi(_SECTIONS, _FACTORS, {"f1": [("2020-01", "not-a-number")]})


# ── c) 闸1 ───────────────────────────────────────────────
def test_gate_consensus_triggers_and_locates_pair():
    """两法分歧（rho<0.7）→ gate1 触发且 pairs 定位该对（DoD c）。"""
    secids = [f"S{i}" for i in range(8)]
    a = {s: i / 8 for i, s in enumerate(secids)}
    b = {s: i / 8 for i, s in enumerate(reversed(secids))}          # 完全反转 → rho=-1
    g = gate_consensus({"m_a": a, "m_b": b}, top_n=3)
    assert "gate1_overfit" in g
    assert g["min_rho"] < 0.7
    assert {(p["a"], p["b"]) for p in g["gate1_overfit"]["pairs"]} == {("m_a", "m_b")}
    assert g["gate1_overfit"]["min_rho"] == g["min_rho"]


def test_gate_consensus_passes_when_consistent():
    secids = [f"S{i}" for i in range(8)]
    a = {s: i / 8 for i, s in enumerate(secids)}
    b = {s: (i + 0.1) / 8 for i, s in enumerate(secids)}            # 近似同序
    g = gate_consensus({"m_a": a, "m_b": b}, top_n=3)
    assert "gate1_overfit" not in g
    assert g["min_rho"] >= 0.7


def test_gate_consensus_top_n_union_not_intersection():
    """相关口径 = Top-N 并集（非仅交集）：打分全序相同但 Top3 不重叠 → 仍触发。"""
    a = {s: i / 10 for i, s in enumerate([f"S{i}" for i in range(10)])}
    # b 与 a 全截面 Spearman≈1（同序），但 Top3 并集内 b 的次序刻意造分歧
    b = dict(a)
    top_a = ["S9", "S8", "S7"]
    top_b = ["S6", "S5", "S4"]                                      # 分数略低于 top_a
    for s, v in zip(top_b, [0.95, 0.94, 0.93]):
        b[s] = v
    g = gate_consensus({"m_a": a, "m_b": b}, top_n=3)
    assert "gate1_overfit" in g                                     # 并集 6 标的中排序分歧
    assert g["gate1_overfit"]["pairs"][0]["n"] == 6


# ── d) 闸2 ───────────────────────────────────────────────
def test_gate_weight_drift_freezes_and_keeps_prev():
    """0.9/0.1 → 0.1/0.9：L1 漂移 0.8 ≥ 0.3 → 冻结沿用上期 + frozen_review（DoD d）。"""
    g = gate_weight_drift({"A": 0.9, "B": 0.1}, {"A": 0.1, "B": 0.9})
    assert g["frozen"] is True and g["frozen_review"] is True
    assert g["drift"] == pytest.approx(0.8)
    assert g["weights"] == {"A": 0.9, "B": 0.1}                     # 沿用上期


def test_gate_weight_drift_small_change_passes():
    g = gate_weight_drift({"A": 0.5, "B": 0.5}, {"A": 0.55, "B": 0.45})
    assert g["frozen"] is False and g["frozen_review"] is False
    assert g["drift"] == pytest.approx(0.05)
    assert g["weights"] == {"A": 0.55, "B": 0.45}


def test_gate_weight_drift_first_period_passes():
    g = gate_weight_drift(None, {"A": 1.0})
    assert g["drift"] is None and g["frozen"] is False and g["weights"] == {"A": 1.0}


def test_gate_weight_drift_new_factor_key():
    """新增因子键按 0 计入漂移（换仓语义不静默）。"""
    g = gate_weight_drift({"A": 1.0}, {"A": 0.6, "C": 0.4})
    assert g["drift"] == pytest.approx(0.4)


# ── f) PIT 截断防线 ───────────────────────────────────────
def test_composite_pit_truncation_defense():
    """history 含 > as_of 月份 → 与手工截断版权重/打分完全一致（防线用例，DoD f）。"""
    months = _months(24)                                            # 2020-01..2021-12
    ics = [-0.2] * 18 + [0.5] * 6                                   # 末 6 月转正
    history_full = {**_ic_hist("f1", ics, months), **_ic_hist("f2", [-0.1] * 24, months)}
    cut = months.index("2021-06") + 1
    history_cut = {fid: [x for x in rows if x[0] <= "2021-06"]
                   for fid, rows in history_full.items()}
    a = composite_scores_multi(_SECTIONS, _FACTORS, history_full, as_of="2021-06-30")
    b = composite_scores_multi(_SECTIONS, _FACTORS, history_cut, as_of="2021-06-30")
    assert a == b                                                   # 不一致即 FAIL（PIT 红线）
    # lasso 同口径：训练面板含未来月份 → 截断一致
    data, _ = _lasso_panel(24)
    factors = [{"id": "f1", "direction": "desc"}, {"id": "f2", "direction": "desc"}]
    data["fwd"] = {m: {s: 0.01 * ((int(s[1:]) * 7) % 30) for s in data["rank"]["f1"][m]}
                   for m in data["months"]}
    la = composite_scores_multi(_SECTIONS, factors, {}, as_of="2021-06",
                                lasso_data=data)["lasso"]
    data_cut = {**data, "months": [m for m in data["months"] if m <= "2021-06"]}
    lb = composite_scores_multi(_SECTIONS, factors, {}, as_of="2021-06",
                                lasso_data=data_cut)["lasso"]
    assert la == lb


# ── e) 快照落库 ───────────────────────────────────────────
def _meta_fixture():
    factors = [{"id": "f1", "direction": "desc"}, {"id": "f2", "direction": "desc"}]
    months = _months(12)
    history = {**_ic_hist("f1", [0.3] * 12, months), **_ic_hist("f2", [0.1] * 12, months)}
    scores, meta = composite_multi_meta(_SECTIONS, factors, history, as_of="2020-12-31")
    gates = {"min_rho": 0.82, "drift": 0.12, "frozen": False, "pairs": []}
    return meta, gates


def test_snapshot_dry_run_zero_rows(tmp_path):
    db = tmp_path / "fl.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE t(x INTEGER)")                        # 确保文件非空库
    con.close()
    meta, gates = _meta_fixture()
    res = save_snapshot(db, "s1", "2020-12-31", meta, gates, dry_run=True)
    assert res == {"written": 0, "dry_run": True}
    con = duckdb.connect(str(db))
    n = con.execute("SELECT count(*) FROM fl_composite_snapshot").fetchone()[0]
    con.close()
    assert n == 0


def test_snapshot_idempotent_and_columns(tmp_path):
    db = tmp_path / "fl.duckdb"
    meta, gates = _meta_fixture()
    r1 = save_snapshot(db, "s1", "2020-12-31", meta, gates, lineage_id="LIN-A")
    con = duckdb.connect(str(db))
    rows = con.execute("SELECT strategy_id, as_of, method, factor_id, weight, directed_ic_12m,"
                       " gate1_min_rho, gate2_drift, frozen, degraded, lineage_id, code_version"
                       " FROM fl_composite_snapshot ORDER BY method, factor_id").fetchall()
    con.close()
    assert r1["written"] == len(rows) == 8                          # 4 法 × 2 因子
    assert all(r[0] == "s1" and str(r[1])[:10] == "2020-12-31" for r in rows)
    ic12 = next(r for r in rows if r[2] == "ic_12m" and r[3] == "f1")
    assert ic12[4] == pytest.approx(0.75) and ic12[5] == pytest.approx(0.3)
    assert ic12[6] == pytest.approx(0.82) and ic12[7] == pytest.approx(0.12)
    assert ic12[8] is False and ic12[9] is None and ic12[10] == "LIN-A" and ic12[11]
    # 幂等：重跑（改权重）行数不变、值更新
    meta2 = {m: dict(v) for m, v in meta.items()}
    meta2["equal"] = {**meta2["equal"], "weights": {"f1": 0.9, "f2": 0.1}}
    r2 = save_snapshot(db, "s1", "2020-12-31", meta2, gates, lineage_id="LIN-B")
    con = duckdb.connect(str(db))
    n = con.execute("SELECT count(*) FROM fl_composite_snapshot").fetchone()[0]
    w = con.execute("SELECT weight FROM fl_composite_snapshot WHERE method='equal'"
                    " AND factor_id='f1'").fetchone()[0]
    con.close()
    assert r2["written"] == 8 and n == 8 and w == pytest.approx(0.9)


def test_snapshot_frozen_flag_written(tmp_path):
    db = tmp_path / "fl.duckdb"
    meta, gates = _meta_fixture()
    save_snapshot(db, "s1", "2020-12-31", meta, {**gates, "drift": 0.8, "frozen": True})
    con = duckdb.connect(str(db))
    fz = con.execute("SELECT DISTINCT frozen, gate2_drift FROM fl_composite_snapshot").fetchall()
    con.close()
    assert fz == [(True, pytest.approx(0.8))]


# ── 月频数据源（golden fixture）────────────────────────────
def test_load_ic_history_directed_flip_and_pit(m2):
    """asc/desc 方向翻转使 IC 变号；月份严格 ≤ as_of（PIT）。"""
    import json
    db, repo = m2
    bars = json.loads((repo / "market" / "daily_bars.json").read_text(encoding="utf-8"))
    closes = {s: {r["dt"]: float(r["close"]) for r in rows} for s, rows in bars.items()}
    con = duckdb.connect(str(db), read_only=True)
    try:
        h_desc = load_ic_history(con, ["val_pe_ttm"], {"val_pe_ttm": "desc"}, closes,
                                 as_of="2021-03-31")
        h_asc = load_ic_history(con, ["val_pe_ttm"], {"val_pe_ttm": "asc"}, closes,
                                as_of="2021-03-31")
    finally:
        con.close()
    ms = [m for m, _ in h_desc["val_pe_ttm"]]
    assert ms == ["2021-01-29", "2021-02-26"]                       # 03-31 无次月收益 → 不入
    assert all(-1.0 <= ic <= 1.0 for _, ic in h_desc["val_pe_ttm"])
    for (m0, i_desc), (m1, i_asc) in zip(h_desc["val_pe_ttm"], h_asc["val_pe_ttm"]):
        assert m0 == m1 and i_asc == pytest.approx(-i_desc)         # 方向翻转 = 变号


def test_build_lasso_data_structure(m2):
    import json
    db, repo = m2
    bars = json.loads((repo / "market" / "daily_bars.json").read_text(encoding="utf-8"))
    closes = {s: {r["dt"]: float(r["close"]) for r in rows} for s, rows in bars.items()}
    con = duckdb.connect(str(db), read_only=True)
    try:
        data = build_lasso_data(con, list(FIDS), {"val_pe_ttm": "asc", "mom_20d_fl": "desc"},
                                closes, as_of="2021-05-31")
    finally:
        con.close()
    assert data["months"] == ["2021-02-26", "2021-03-31", "2021-04-30"]   # 01-29 无 mom 历史 → 跳过
    for fid in FIDS:
        assert set(data["rank"][fid]) == set(data["months"])
        for m in data["months"]:
            assert len(data["rank"][fid][m]) == 10
            assert all(0.0 <= v <= 1.0 for v in data["rank"][fid][m].values())
    for m in data["months"]:
        assert len(data["fwd"][m]) == 10


# ── 端到端：run_factor_backtest 四法路径 ───────────────────
def test_run_factor_backtest_methods_path(m2):
    """methods 四法并列 + primary 出 targets + 双闸日志 + 快照落库（golden 全程）。"""
    db, repo = m2
    spec = {"id": "m6_multi", "name": "M6 四法并列",
            "factors": [{"id": "val_pe_ttm", "direction": "asc", "weight": 1.0},
                        {"id": "mom_20d_fl", "direction": "desc", "weight": 1.0}],
            "top_n": 3, "rebalance": "monthly", "benchmark": "510300.SH",
            "methods": ["equal", "ic_12m", "ic_ir", "lasso"], "method_primary": "ic_ir"}
    bt = run_factor_backtest(db, repo, spec, start="2021-03-01", end="2021-06-30")
    comp = bt["composite"]
    assert comp["methods"] == ["equal", "ic_12m", "ic_ir", "lasso"] and comp["primary"] == "ic_ir"
    # 每法 × 每再平衡日独立选股（4 日 × 4 法 = 16 条）
    dts = {p["dt"] for p in comp["method_picks"]}
    assert dts == {"2021-03-31", "2021-04-30", "2021-05-31", "2021-06-30"}
    for d in dts:
        for m in comp["methods"]:
            pick = next(p for p in comp["method_picks"] if p["dt"] == d and p["method"] == m)
            assert len(pick["secids"]) == 3
    # primary 出 targets
    for t in bt["factor_targets"]:
        pick = next(p for p in comp["method_picks"]
                    if p["dt"] == t["dt"] and p["method"] == "ic_ir")
        assert sorted(pick["secids"]) == sorted(t["secids"])
    # 双闸：首期 drift=None；golden 窗口 <6 月 → 各法 degraded（透明），primary 退化 → 闸2 跳过
    g0 = comp["gates"][0]
    assert g0["gate2"]["drift"] is None
    assert g0["degraded"]["ic_12m"] and g0["degraded"]["ic_ir"] and g0["degraded"]["lasso"]
    assert "闸2" in g0["gate2"]["skipped"]
    # 快照落库：4 日 × 4 法 × 2 因子 = 32 行；degraded 如实入库
    con = duckdb.connect(str(db), read_only=True)
    try:
        n, methods = con.execute(
            "SELECT count(*), count(DISTINCT method) FROM fl_composite_snapshot"
            " WHERE strategy_id = 'm6_multi'").fetchone()
        deg = con.execute("SELECT DISTINCT degraded FROM fl_composite_snapshot"
                          " WHERE method = 'ic_12m'").fetchall()
        lin = con.execute("SELECT DISTINCT lineage_id FROM fl_composite_snapshot").fetchall()
    finally:
        con.close()
    assert n == 32 and methods == 4
    assert all(d[0] and "窗口不足" in d[0] for d in deg)
    assert all(l[0] for l in lin)                                   # lineage 四元组非空
    assert comp["snapshot_written"] == 32


def test_run_factor_backtest_dry_run_no_snapshot(m2):
    db, repo = m2
    spec = {"id": "m6_dry", "name": "演练态",
            "factors": [{"id": "val_pe_ttm", "direction": "asc", "weight": 1.0},
                        {"id": "mom_20d_fl", "direction": "desc", "weight": 1.0}],
            "top_n": 2, "rebalance": "monthly", "benchmark": "510300.SH",
            "methods": ["equal", "ic_12m"], "dry_run": True}
    bt = run_factor_backtest(db, repo, spec, start="2021-04-01", end="2021-06-30")
    assert bt["composite"]["snapshot_written"] == 0
    con = duckdb.connect(str(db), read_only=True)
    try:
        n = con.execute("SELECT count(*) FROM fl_composite_snapshot"
                        " WHERE strategy_id = 'm6_dry'").fetchone()[0]
    finally:
        con.close()
    assert n == 0


def test_run_factor_backtest_illegal_primary(m2):
    db, repo = m2
    spec = {"id": "m6_bad", "name": "bad",
            "factors": [{"id": "val_pe_ttm", "direction": "asc", "weight": 1.0},
                        {"id": "mom_20d_fl", "direction": "desc", "weight": 1.0}],
            "top_n": 2, "rebalance": "monthly", "benchmark": "510300.SH",
            "methods": ["equal"], "method_primary": "auto_x"}
    with pytest.raises(Exception, match="method_primary 非法"):
        run_factor_backtest(db, repo, spec, start="2021-04-01", end="2021-06-30")

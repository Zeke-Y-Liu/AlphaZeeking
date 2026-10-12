"""M6 合成打分测试（实施计划 §9.1 + DoD 5：四法并列输出，双闸告警逻辑单测）。

合成库 C（8 标的 B01..B08 × 33 月，月末日截面）：
- F_good：value = k → 对齐 IC 恒 +1（打分方向锚）；
- F_copy：value = 2k+1 → 与 F_good 完全同秩（共线克隆，lasso 稀疏锚）；
- F_ind：循环轮换 → 近似独立（滚动权重对照）。
"""
import calendar
from datetime import date
from pathlib import Path

import duckdb
import pytest

from factorlib.composite import (CompositeError, GATE_CONSISTENCY_MIN,
                                 GATE_WEIGHT_DRIFT_MAX, ROLLING_MONTHS,
                                 StrategyDSL, _rolling_weights, aligned_z,
                                 composite_scores, consistency_gate, lasso_fit,
                                 load_strategies, forward_panels,
                                 weight_stability_gate)
from factorlib.evaluate import _EVAL_DDLS
from factorlib.registry import STATUS_ACTIVE, STATUS_DRAFT

PKG = Path(__file__).resolve().parents[1]      # factorlib/（load_strategies 的 root）

SEC = [f"B{k:02d}" for k in range(1, 9)]
R = {s: 0.01 * k for k, s in enumerate(SEC, start=1)}
MES = [f"{date(y, m, calendar.monthrange(y, m)[1]).isoformat()}"
       for y, m in [(2021, m) for m in range(1, 13)]
       + [(2022, m) for m in range(1, 13)]
       + [(2023, m) for m in range(1, 10)]]


def _fv(fid: str, i: int, k: int) -> float:
    if fid == "F_good":
        return float(k)
    if fid == "F_copy":
        return float(2 * k + 1)
    if fid == "F_ind":
        return float((k - 1 + i) % 8 + 1)
    raise KeyError(fid)


class _Meta:
    def __init__(self, fid, direction=1):
        self.id, self.direction, self.status = fid, direction, STATUS_DRAFT
        self.name, self.version, self.family = fid, "1.0.0", "test"


@pytest.fixture()
def cp_con():
    con = duckdb.connect(":memory:")
    for ddl in _EVAL_DDLS:
        con.execute(ddl)
    con.execute("""CREATE TABLE factor_value_daily
                   (dt DATE, secid TEXT, factor_id TEXT, value DOUBLE)""")
    for s in SEC:
        c, closes = 100.0, []
        for _ in MES:
            c *= 1 + R[s]
            closes.append(c)
        con.executemany("INSERT INTO dwd_daily_bar VALUES (?,?,?,?,?,?,?,?,?)",
                        [(s, dt, c, c * 1.01, c * 0.99, c, 100000.0, 100000.0 * c, R[s])
                         for dt, c in zip(MES, closes)])
    con.executemany("INSERT INTO dwd_daily_bar VALUES (?,?,?,?,?,?,?,?,?)",
                    [("510300.SH", dt, 100.0, 100.0, 100.0, 100.0, 1000.0,
                      100000.0, 0.0) for dt in MES])
    con.executemany("INSERT INTO dim_security VALUES (?,?,?,?,?)",
                    [(s, s, "XSHG", "STOCK", "IND1") for s in SEC])
    con.executemany("INSERT INTO factor_value_daily (dt, secid, factor_id, value)"
                    " VALUES (?,?,?,?)",
                    [(dt, s, fid, _fv(fid, i, k))
                     for fid in ("F_good", "F_copy", "F_ind")
                     for i, dt in enumerate(MES) for k, s in enumerate(SEC, start=1)])
    return con


@pytest.fixture()
def metas():
    return {f: _Meta(f) for f in ("F_good", "F_copy", "F_ind")}


# ── DSL 解析 ──
def test_load_strategies():
    strategies = load_strategies(PKG)
    assert {"val_single_v1", "pe_roe_v1", "multi_resonance_v1"} <= set(strategies)
    v1 = strategies["val_single_v1"]
    assert v1.mode == "single" and v1.factors == [{"id": "val_ps_ttm", "weight": "equal"}]
    mr = strategies["multi_resonance_v1"]
    assert mr.methods == ["equal", "ic_12m", "ic_ir", "lasso"] and mr.top_n == 3
    assert strategies["pe_roe_v1"].methods == ["rank_product"]


def test_load_strategies_rejects(tmp_path):
    for name, body, pat in [
        ("bad1", "id: bad1\nfactors: [{id: x, weight: equal}]\n", "缺少必填字段"),
        ("bad2", "id: bad2\nfactors: [{id: x, weight: equal}, {id: x, weight: equal}]\n"
                 "top_n: 3\nmethods: [equal]\n", "重复"),
        ("bad3", "id: bad3\nfactors: [{id: x, weight: equal}]\ntop_n: 3\nmethods: [nope]\n",
         "未知合成法"),
    ]:
        d = tmp_path / name
        (d / "strategies").mkdir(parents=True)
        (d / "strategies/one.yaml").write_text(body, encoding="utf-8")
        with pytest.raises(CompositeError, match=pat):
            load_strategies(d)


# ── 方向对齐 zscore ──
def test_aligned_z_direction(cp_con, metas):
    F = aligned_z(cp_con, metas, ["F_good"], MES[0], MES[-1])["F_good"]
    z = F[MES[5]]
    assert max(z, key=z.get) == "B08"          # direction=+1：值大 → z 高
    metas["F_good"].direction = -1
    z2 = aligned_z(cp_con, metas, ["F_good"], MES[0], MES[-1])["F_good"][MES[5]]
    assert max(z2, key=z2.get) == "B01"        # direction=-1：对齐后反转


# ── 四法并列输出（DoD 5 前半） ──
def test_four_methods_end_to_end(cp_con, metas):
    st = load_strategies(PKG)["multi_resonance_v1"]
    # 合成因子替换真实因子（等结构演练）
    st.factors = [{"id": "F_good", "weight": "auto_ic_12m"},
                  {"id": "F_copy", "weight": "auto_ic_12m"},
                  {"id": "F_ind", "weight": "auto_ic_12m"}]
    rep = composite_scores(cp_con, st, metas, MES[0], MES[-1])
    assert set(rep["scores"]) == {"equal", "ic_12m", "ic_ir", "lasso"}
    assert rep["window"]["n_months"] == len(MES)
    assert rep["factor_status"] == {"F_good": "draft", "F_copy": "draft",
                                    "F_ind": "draft"}      # 诚实标注
    # equal 法打分方向锚：MES[16] 处 i≡0 (mod 8) → F_ind ≡ F_good，
    # 三因子秩全同 → B08（F_good 值最大）打分最高
    top = max(rep["scores"]["equal"][MES[16]], key=rep["scores"]["equal"][MES[16]].get)
    assert top == "B08"
    # ic_12m 权重方向锚：F_good IC=+1 → 权重最大
    w = rep["weights"]["ic_12m"][MES[20]]
    assert w["F_good"] >= w["F_ind"]
    # 闸结构齐全
    assert rep["gate_consistency"]["threshold"] == GATE_CONSISTENCY_MIN
    assert rep["gate_weight_stability"]["drifts"]


def test_single_mode_equal_weights(cp_con, metas):
    st = load_strategies(PKG)["val_single_v1"]
    st.factors = [{"id": "F_good", "weight": "equal"}]
    rep = composite_scores(cp_con, st, metas, MES[0], MES[-1])
    z = aligned_z(cp_con, metas, ["F_good"], MES[0], MES[-1])["F_good"][MES[5]]
    assert rep["scores"]["equal"][MES[5]] == pytest.approx(z)   # 权重 1.0 = z 本身
    assert rep["weights"]["equal"][MES[5]] == {"F_good": 1.0}


# ── lasso 稀疏锚（共线克隆 → 压缩冗余系数） ──
def test_lasso_sparsify_collinear(cp_con, metas):
    st = StrategyDSL(id="t_lasso", mode="composite", top_n=3,
                     factors=[{"id": "F_good", "weight": "equal"},
                              {"id": "F_copy", "weight": "equal"}],
                     methods=["lasso"])
    rep = composite_scores(cp_con, st, metas, MES[0], MES[-1])
    w = rep["weights"]["lasso"][MES[-1]]
    assert min(w.values()) < 0.1               # 共线双因子至少一个被压稀
    assert rep["scores"]["lasso"][MES[-1]]      # 打分仍可用


# ── 滚动权重防未来函数 ──
def test_rolling_weights_no_lookahead(cp_con, metas):
    mes, fwd = forward_panels(cp_con, MES[0], MES[-1])
    F = aligned_z(cp_con, metas, ["F_good", "F_ind"], MES[0], MES[-1])
    w = _rolling_weights("ic_12m", mes, F, fwd, ["F_good", "F_ind"])
    assert w[mes[0]] == {"F_good": 0.5, "F_ind": 0.5}     # 首月无历史 → equal 起步
    assert w[mes[ROLLING_MONTHS]]["F_good"] > w[mes[ROLLING_MONTHS]]["F_ind"]


def test_negative_ic_weight_clamped_zero(cp_con, metas):
    """不赌反向：负 IC 因子权重 0（_normalize_pos clamp），不产生负权重。"""
    metas["F_bad"] = _Meta("F_bad")                        # direction=+1
    cp_con.executemany(
        "INSERT INTO factor_value_daily (dt, secid, factor_id, value) VALUES (?,?,?,?)",
        [(dt, s, "F_bad", float(9 - k))                    # 与 F_good 反向 → IC = -1
         for dt in MES for k, s in enumerate(SEC, start=1)])
    mes, fwd = forward_panels(cp_con, MES[0], MES[-1])
    F = aligned_z(cp_con, metas, ["F_good", "F_bad"], MES[0], MES[-1])
    w = _rolling_weights("ic_12m", mes, F, fwd, ["F_good", "F_bad"])
    assert w[mes[-1]] == {"F_good": 1.0, "F_bad": 0.0}


# ── 闸 1 一致性（DoD 5：告警逻辑单测） ──
def test_gate1_consistency():
    g = consistency_gate({"only_one": {}})                 # 法数 < 2 → 不适用
    assert g["triggered"] is False and g["pass"] is True
    sec = [f"S{i}" for i in range(8)]
    sa = {f"2021-01-01": {s: float(i) for i, s in enumerate(sec)}}       # 升序
    sb = {f"2021-01-01": {s: float(7 - i) for i, s in enumerate(sec)}}   # 反序
    g = consistency_gate({"m_a": sa, "m_b": sb})
    assert g["triggered"] is True and g["pass"] is False
    assert "过拟合告警" in g["note"] and g["mean_rho"] == pytest.approx(-1.0)
    g2 = consistency_gate({"m_a": sa, "m_b": {f"2021-01-01": dict(sa["2021-01-01"])}})
    assert g2["pass"] is True and g2["mean_rho"] == pytest.approx(1.0)


# ── 闸 2 权重稳定（DoD 5：freeze_review 单测） ──
def test_gate2_weight_drift():
    g = weight_stability_gate({"equal": {"d1": {"A": 0.5, "B": 0.5},
                                         "d2": {"A": 0.5, "B": 0.5}}})
    assert g["pass"] is True and g["drifts"]["equal"] == 0.0
    g = weight_stability_gate({"ic_12m": {"d1": {"A": 0.5, "B": 0.5},
                                          "d2": {"A": 0.05, "B": 0.95}}})
    assert g["triggered"] is True and g["pass"] is False
    assert g["review_months"] == ["ic_12m:d2"]             # L1 漂移 0.9 ≥ 0.3
    assert "freeze_review" in g["note"]

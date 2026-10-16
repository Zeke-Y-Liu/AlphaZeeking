"""M4 治理关卡测试（实施计划 §7 DoD）：注入测试 ④/⑤ + walk-forward 手工锚 + 端到端。

合成库 G（8 标的 B01..B08 × 33 个月末 2021-01..2023-09，基准 510300.SH 恒 100）：
- 月收益 r_k = 0.01·k（B01→1% … B08→8%），月末复利 close；
- F_perfect：value = k（恒定）→ 对齐 IC 恒 +1.0 → 三关全过；
- F_2021only：前 12 月 value = k（IC +1），此后 13−k（IC −1）→ 时间关打回（DoD 注入 ④）；
- F_clone：F_perfect 仿制（每 3 月交换 B01/B02 秩）→ ρ̄≈0.99 ≥ 0.6 → 转替代通道（DoD 注入 ⑤）；
- F_indep：前 6 月 value = k，此后循环轮换 ((k−1+i) mod 8)+1 → OOS 衰减 ≥ 50% → 时间关打回；
- 行业：4 行业 × 2 标的（中性化轴组内非退化；市值表为空 → size 轴降级）。
"""
import calendar
from datetime import date

import duckdb
import pytest

from factorlib.evaluate import _EVAL_DDLS
from factorlib.governance import (bonferroni, corr_matrix, deflated_sharpe,
                                  incremental_gate, month_ic, multi_test, norm_ppf,
                                  normal_cdf, run_governance_on_con, temporal_gate,
                                  walk_forward, winsorize_cross, yearly_contribution)

SEC = [f"B{k:02d}" for k in range(1, 9)]
R = {s: 0.01 * k for k, s in enumerate(SEC, start=1)}
MES = [f"{date(y, m, calendar.monthrange(y, m)[1]).isoformat()}"
       for y, m in [(2021, m) for m in range(1, 13)]
       + [(2022, m) for m in range(1, 13)]
       + [(2023, m) for m in range(1, 10)]]                 # 33 个月末
BENCH = "510300.SH"
INDUSTRY = {s: f"IND{(k - 1) % 4 + 1}" for k, s in enumerate(SEC, start=1)}
WF, LB = (6, 3), 3


def closes_series(s: str) -> list[float]:
    out, c = [], 100.0
    for _ in MES:
        c *= 1 + R[s]
        out.append(c)
    return out


def _fv(fid: str, i: int, k: int) -> float:
    """第 i 月（0-based）标的 k 的因子值（各注入因子语义，见模块 docstring）。"""
    if fid == "F_perfect":
        return float(k)
    if fid == "F_2021only":
        return float(k) if i < 12 else float(13 - k)
    if fid == "F_clone":
        k2 = k
        if i % 3 == 0 and k == 1:
            k2 = 2
        elif i % 3 == 0 and k == 2:
            k2 = 1
        return float(k2)
    if fid == "F_indep":
        return float(k) if i < 6 else float((k - 1 + i) % 8 + 1)
    raise KeyError(fid)


@pytest.fixture()
def gov_con():
    con = duckdb.connect(":memory:")
    for ddl in _EVAL_DDLS:
        con.execute(ddl)
    con.execute("""CREATE TABLE factor_value_daily
                   (dt DATE, secid TEXT, factor_id TEXT, value DOUBLE)""")
    con.execute("""CREATE TABLE factor_pit_meta
                   (dt DATE, secid TEXT, factor_id TEXT, used_period DATE,
                    used_announce DATE, data_staleness INTEGER)""")
    for s in SEC:
        con.executemany("INSERT INTO dwd_daily_bar VALUES (?,?,?,?,?,?,?,?,?)",
                        [(s, dt, c, c, c, c, 1000, 1000 * c, 0.0)
                         for dt, c in zip(MES, closes_series(s))])
    con.executemany("INSERT INTO dwd_daily_bar VALUES (?,?,?,?,?,?,?,?,?)",
                    [(BENCH, dt, 100.0, 100.0, 100.0, 100.0, 1000, 100000.0, 0.0)
                     for dt in MES])
    con.executemany("INSERT INTO dim_security VALUES (?,?,?,?,?)",
                    [(s, s, "XSHG" if k < 5 else "XSHE", "STOCK", INDUSTRY[s])
                     for k, s in enumerate(SEC, start=1)])
    rows = [(dt, s, fid, _fv(fid, i, k))
            for fid in ("F_perfect", "F_2021only", "F_clone", "F_indep")
            for i, dt in enumerate(MES) for k, s in enumerate(SEC, start=1)]
    con.executemany("INSERT INTO factor_value_daily (dt, secid, factor_id, value)"
                    " VALUES (?,?,?,?)", rows)
    return con


class _Meta:
    def __init__(self, fid, direction=1):
        self.id, self.direction, self.type = fid, direction, "alpha"
        self.name, self.version, self.family, self.engine = fid, "1.0.0", "test", "synthetic"


METAS = {fid: _Meta(fid) for fid in ("F_perfect", "F_2021only", "F_clone", "F_indep")}


# ── ⑥ 统计原语 ──
def test_norm_ppf_roundtrip():
    assert norm_ppf(0.975) == pytest.approx(1.959964, abs=1e-5)
    for p in (0.01, 0.1, 0.3, 0.5, 0.7, 0.9, 0.99):
        assert normal_cdf(norm_ppf(p)) == pytest.approx(p, abs=1e-7)
    with pytest.raises(ValueError):
        norm_ppf(0.0)


def test_bonferroni():
    assert bonferroni(20) == pytest.approx(0.0025)
    assert bonferroni(10, alpha0=0.01) == pytest.approx(0.001)
    with pytest.raises(ValueError):
        bonferroni(0)


def test_deflated_sharpe():
    r0 = deflated_sharpe(0.0, 60, n_trials=1, sr_var=None)
    assert r0["sr0"] == 0.0 and r0["psr"] == pytest.approx(0.5)
    assert deflated_sharpe(0.3, 60, 1)["psr"] > deflated_sharpe(0.0, 60, 1)["psr"]
    # 有 trial 方差 → SR0 > 0 → 同 SR 下 PSR 更低（多重检验惩罚方向正确）
    ra = deflated_sharpe(0.3, 60, 20, sr_var=0.04)
    assert ra["sr0"] > 0
    assert ra["psr"] < deflated_sharpe(0.3, 60, 20, sr_var=None)["psr"]
    assert deflated_sharpe(0.5, 1, 1)["psr"] is None            # T < 2


# ── ④b 参数原语 ──
def test_winsorize_cross():
    vals = {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0, "e": 100.0}
    w = winsorize_cross(vals, 0.2)
    assert w["e"] == pytest.approx(23.2)                        # q(0.8) = 4 + 0.2·96
    assert w["a"] == pytest.approx(1.8)                         # q(0.2) = 1 + 0.8·1
    assert winsorize_cross(vals, 0.0) == vals


def test_month_ic(gov_con):
    from factorlib.governance import ic_series
    metas = METAS
    ser = ic_series(gov_con, metas["F_perfect"], MES[0], MES[-1])
    assert len(ser) == 32                                       # 33 月 − 1 前瞻窗
    assert all(r["ic"] == 1.0 for r in ser)
    ser_neg = ic_series(gov_con, _Meta("F_perfect", direction=-1),
                        MES[0], MES[-1])
    assert all(r["ic"] == -1.0 for r in ser_neg)
    assert month_ic({f"S{i}": float(i) for i in range(1, 3)},
                    {f"S{i}": 0.1 for i in range(1, 3)}, 1)[0] is None      # n < 3
    ic, n, degen = month_ic({f"S{i}": float(i) for i in range(1, 5)},
                            {f"S{i}": 0.1 * i for i in range(1, 5)}, 1,
                            industry={"S1": "A", "S2": "B", "S3": "C", "S4": "D"},
                            neutralize=True)
    assert degen is True                                        # 每组 1 只 → 退化


# ── ④a 时间稳健手工锚 ──
def test_walk_forward_hand():
    ics = [{"dt": f"m{i:02d}", "ic": 1.0 if i < 15 else 0.5, "n": 5}
           for i in range(30)]
    wf = walk_forward(ics, train_min=6, step=6)
    assert wf["n_folds"] == 4                                   # te = 6,12,18,24
    assert wf["folds"][0]["decay"] == pytest.approx(0.0)        # train 1.0 / valid 1.0
    assert wf["folds"][2]["decay"] == pytest.approx(1 - 0.5 / ((15 + 3 * 0.5) / 18))
    # 池化：valid = ics[6:30]（9×1 + 15×0.5），train = 最末窗 ics[:24]（15 + 9×0.5）
    assert wf["valid_ic_mean"] == pytest.approx((9 + 15 * 0.5) / 24)
    assert wf["train_ic_mean"] == pytest.approx((15 + 9 * 0.5) / 24)
    assert wf["decay_pooled"] == pytest.approx(1 - 0.6875 / 0.8125)
    assert walk_forward(ics[:5], train_min=6, step=6)["n_folds"] == 0
    with pytest.raises(ValueError):
        walk_forward(ics, train_min=0, step=6)


def test_yearly_contribution():
    ics = ([{"dt": f"2021-{m:02d}-28", "ic": 1.0, "n": 5} for m in range(1, 13)]
           + [{"dt": f"2022-{m:02d}-28", "ic": 1.0, "n": 5} for m in range(1, 13)])
    yc = yearly_contribution(ics)
    assert yc["years"]["2021"]["share"] == pytest.approx(0.5)
    assert yc["max_share"] == pytest.approx(0.5)
    neg = [{"dt": "2021-01-28", "ic": 1.0, "n": 5},
           {"dt": "2022-01-28", "ic": -2.0, "n": 5}]
    assert yearly_contribution(neg)["max_share"] is None        # 总 IC ≤ 0 → share 无意义


def test_temporal_gate_injection():
    """DoD 注入 ④：只在前期单边有效的因子 → 时间关打回；完美因子通过且 lockbox 只报告。"""
    bad = ([{"dt": f"2021-{m:02d}-28", "ic": 1.0, "n": 5} for m in range(1, 13)]
           + [{"dt": f"2022-{m:02d}-28", "ic": -1.0, "n": 5} for m in range(1, 13)]
           + [{"dt": f"2023-{m:02d}-28", "ic": -1.0, "n": 5} for m in range(1, 6)])
    g = temporal_gate(bad, train_min=6, step=3, lockbox_months=3)
    assert g["pass"] is False
    good = ([{"dt": f"2021-{m:02d}-28", "ic": 1.0, "n": 5} for m in range(1, 13)]
            + [{"dt": f"2022-{m:02d}-28", "ic": 1.0, "n": 5} for m in range(1, 13)]
            + [{"dt": f"2023-{m:02d}-28", "ic": 1.0, "n": 5} for m in range(1, 6)])
    g2 = temporal_gate(good, train_min=6, step=3, lockbox_months=3)
    assert g2["pass"] is True
    assert g2["walk_forward"]["decay_pooled"] == pytest.approx(0.0)
    assert g2["lockbox"]["n"] == 3 and g2["lockbox"]["mean_ic"] == 1.0
    assert g2["yearly"]["max_share"] == pytest.approx(12 / 26)


# ── ⑤ 增量价值（DoD 注入：仿制因子转替代通道） ──
def test_corr_and_incremental(gov_con):
    # 回归锚：日频长表插入非月末行 → 相关矩阵必须仍只取月末截面（§7 月度口径）
    gov_con.executemany(
        "INSERT INTO factor_value_daily (dt, secid, factor_id, value) VALUES (?,?,?,?)",
        [("2021-01-15", s, fid, 1.0)
         for fid in ("F_perfect", "F_clone") for s in ("B01", "B02", "B03")])
    corr = corr_matrix(gov_con, ["F_perfect", "F_clone", "F_indep"], MES[0], MES[-1])
    pair = corr["pairs"]["F_perfect|F_clone"]
    assert pair["mean_rho"] == pytest.approx(0.9918, abs=1e-3)  # 29 月中 10 月交换秩
    assert pair["mean_rho"] >= 0.6
    assert corr["pairs"]["F_perfect|F_indep"]["mean_rho"] < 0.6
    ig = incremental_gate("F_clone", corr)
    assert ig["pass"] is False and ig["route"] == "alternative"
    assert "替代版本" in ig["reason"]
    assert incremental_gate("F_indep", corr)["pass"] is True


# ── ⑥ 多重检验触发 ──
def test_multi_test_threshold():
    mt = multi_test([{"id": f"F{i}", "ic_ir": 0.5, "n_months": 60} for i in range(9)])
    assert mt["triggered"] is False and mt["n"] == 9
    mt2 = multi_test([{"id": f"F{i}", "ic_ir": 0.3 + 0.01 * i, "n_months": 60}
                      for i in range(20)])
    assert mt2["triggered"] is True
    assert mt2["bonferroni_alpha"] == pytest.approx(0.0025)
    assert len(mt2["rows"]) == 20 and mt2["sr_var"] > 0


# ── 端到端（合成库 4 因子全裁决） ──
def test_run_governance_end_to_end(gov_con, tmp_path):
    rep = run_governance_on_con(gov_con, METAS, wf=WF, lockbox_months=LB,
                                out_dir=tmp_path, materialize_values=False)
    v = {x["factor_id"]: x for x in rep["verdicts"]}
    assert v["F_perfect"]["overall"] == "pass"
    assert v["F_clone"]["overall"] == "alternative"             # DoD 注入 ⑤
    assert v["F_2021only"]["overall"] == "fail"                 # DoD 注入 ④
    assert v["F_indep"]["overall"] == "fail"
    assert v["F_2021only"]["time_gate"]["pass"] is False
    assert v["F_perfect"]["regime_gate"]["pass"] is True
    assert v["F_perfect"]["regime_gate"]["axes"]["size"]["status"] == "degraded"
    assert v["F_perfect"]["pit"]["violations"] == 0
    assert rep["multi_test"]["triggered"] is False              # 4 < 20
    md = (tmp_path / "m4_governance.md").read_text(encoding="utf-8")
    for sec in ("## 裁决看板", "## 逐因子明细", "## 相关性矩阵看板", "## ⑥ 多重检验",
                "## 口径与局限"):
        assert sec in md
    assert (tmp_path / "m4_governance.json").exists()


def test_run_governance_missing_factor(gov_con):
    with pytest.raises(ValueError, match="未注册"):
        run_governance_on_con(gov_con, METAS, factor_ids=["nope"])

"""M4 稳健性三关 + 增量价值 + 多重检验测试（期望值独立手工推演）。

关键推演：
- walk-forward 40 月（lockbox=min(12, 40//4)=10 → core 30 月）：train24/valid6/step6
  → 恰 1 折（valid = m24..m29）；is_mean=0.1、oos=0.05 → decay=0.5；
  年度贡献 2022/2023 各 1.2、2024 0.3 → top share = 1.2/2.7 = 44.4%
- 单边注入（2022 年独高 0.5、其余 0.01；48 月 lockbox 12 → core 36 月）：
  top_year_share = 6.0/6.24 = 96.2% → 打回
- Bonferroni：t=2.0 → p=0.0455，N=5 → p_adj=0.2275
- DSR：N=1 退化为 PSR(0)；试验数增大 → SR₀ 升 → DSR 单调降
运行：``cd alphapocket && python -m pytest tests/test_factor_governance.py -q``
"""
from __future__ import annotations

import duckdb
import pytest

from applib.factor_governance import (
    bonferroni, cross_factor_corr, deflated_sharpe, gate_incremental,
    gate_param_robustness, gate_time_robustness, regime_segments, walk_forward,
)
from factorlib import load_factor, set_status
from factorlib.materialize import materialize


def _series(n: int, fn) -> list[tuple[str, float]]:
    """n 个月末（2021-01 起连续月）× 生成函数（月序 → ic）。"""
    from datetime import date
    out, y, m = [], 2021, 1
    for i in range(n):
        out.append((f"{y:04d}-{m:02d}-28", fn(i)))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


# ── ④.1 时间稳健 ───────────────────────────────────────
def test_walk_forward_hand_computed():
    """40 月、lockbox=10 → core 30 月：恰 1 折；衰减 0.5；年度份额 44.4%。"""
    ser = _series(40, lambda i: 0.1 if i < 24 else 0.05)
    wf = walk_forward(ser, "desc", train_months=24, valid_months=6, step_months=6,
                      lockbox_months=12)
    assert wf["n_months"] == 40 and wf["n_lockbox_months"] == 10
    assert wf["n_folds"] == 1
    assert wf["folds"][0]["train_mean"] == pytest.approx(0.1)
    assert wf["folds"][0]["valid_mean"] == pytest.approx(0.05)
    assert wf["folds"][0]["decay"] == pytest.approx(0.5)
    assert wf["is_mean"] == pytest.approx(0.1) and wf["oos_mean"] == pytest.approx(0.05)
    assert wf["decay"] == pytest.approx(0.5)
    assert wf["year_sum"] == {"2021": 1.2, "2022": 1.2, "2023": 0.3}
    assert wf["top_year_share"] == pytest.approx(1.2 / 2.7, abs=1e-3)
    assert len(wf["lockbox"]["months"]) == 10 and wf["lockbox"]["n"] == 10
    g = gate_time_robustness(wf)
    assert g["pass"] is True and g["reasons"] == []


def test_walk_forward_rejects_single_year_factor():
    """注入测试（协议 DoD）：只在单一年度有效的合成因子 → ④ 正确打回。"""
    ser = _series(48, lambda i: 0.5 if 12 <= i < 24 else 0.01)   # 2022 独高
    wf = walk_forward(ser, "desc", train_months=24, valid_months=6, step_months=6)
    g = gate_time_robustness(wf)
    assert g["pass"] is False
    assert any("年度贡献集中" in r for r in g["reasons"])
    assert wf["top_year_share"] == pytest.approx(6.0 / 6.24, abs=1e-3)
    assert any("衰减" in r for r in g["reasons"])                # 样本外塌陷也触发


def test_walk_forward_asc_direction_flip():
    """asc 因子：IC 全负但定向为正 → 同等 pass（方向语义经翻转进入判定）。"""
    ser = _series(40, lambda i: -0.1 if i < 24 else -0.05)
    wf = walk_forward(ser, "asc", train_months=24, valid_months=6, step_months=6)
    assert wf["is_mean"] == pytest.approx(0.1) and wf["decay"] == pytest.approx(0.5)
    assert gate_time_robustness(wf)["pass"] is True


def test_walk_forward_lockbox_excluded_and_reported():
    ser = _series(40, lambda i: 0.2 if i >= 30 else 0.1)         # lockbox 段独高
    wf = walk_forward(ser, "desc", train_months=24, valid_months=6, step_months=6)
    assert wf["lockbox"]["directed_mean"] == pytest.approx(0.2)  # 仅登记
    g = gate_time_robustness(wf)
    assert "lockbox" in g and "只验证一次" in g["lockbox"]["note"]


# ── ⑥ 多重检验 ─────────────────────────────────────────
def test_bonferroni_hand_computed():
    r = bonferroni(2.0, 5)
    assert r["p"] == pytest.approx(0.0455003, abs=1e-5)          # 2(1−Φ(2))
    assert r["p_adj"] == pytest.approx(0.2275013, abs=1e-5)
    assert bonferroni(0.5, 20)["p_adj"] == 1.0                   # cap 1
    with pytest.raises(Exception, match="n_trials"):
        bonferroni(2.0, 0)


def test_deflated_sharpe_properties():
    """N=1 退化为 PSR(0)（手工值）；试验数增大 → SR₀ 升 → DSR 单调降。"""
    sr, n = 0.2, 60
    one = deflated_sharpe(sr, n, 1)
    # PSR(0)：Φ(SR·√(T−1)/√(1−γ₃SR+((γ₄−1)/4)SR²))，γ₃=0,γ₄=3 → Φ(0.2·√59/√1.02)
    from statistics import NormalDist
    assert one["dsr"] == pytest.approx(NormalDist().cdf(0.2 * 59 ** 0.5 / 1.02 ** 0.5),
                                       rel=1e-6)
    assert one["sr0"] == 0.0
    d5, d50 = deflated_sharpe(sr, n, 5), deflated_sharpe(sr, n, 50)
    assert d5["sr0"] > one["sr0"] and d50["sr0"] > d5["sr0"]
    assert d50["dsr"] < d5["dsr"] <= one["dsr"]
    with pytest.raises(Exception):
        deflated_sharpe(sr, 1, 5)                                # 样本不足


# ── ④.3 环境稳健（合成基准精确控制牛熊）────────────────
def _flat_days(n: int) -> list[str]:
    from datetime import date, timedelta
    d, out = date(2021, 1, 4), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def test_regime_segments_bull_bear_split():
    """序列 A 全程水平 100 → 满窗 MA=close → 全判熊，负 IC 段落入失效环境标签；
    序列 B 先 250 日 100 后 100 日 400 → 满窗后 close > MA 判牛（bear 仅剩满窗首日）；
    截面日早于 MA 满窗 → unknown 不计分段；空行情报错。"""
    days = _flat_days(350)
    # 序列 A：全程 100 → close=MA → 非 bull → bear；directed IC 为负 → bear 记失效
    closes_a = {d: 100.0 for d in days}
    seg = regime_segments(closes_a, "BMK", [(d, -0.01) for d in days], "desc")
    assert seg["segments"]["bear"]["n"] > 0 and seg["segments"]["bull"]["n"] == 0
    assert seg["failed_environments"] == ["bear"]
    # 序列 B：前 250 日 100、后 100 日 400 → 满窗后 close 400 > MA(≤220) → bull
    closes_b = {d: (100.0 if i < 250 else 400.0) for i, d in enumerate(days)}
    seg = regime_segments(closes_b, "BMK", [(d, 0.01) for d in days], "desc")
    assert seg["segments"]["bull"]["n"] >= 50
    assert seg["segments"]["bear"]["n"] > 0                       # 满窗首日 close=MA → 判熊
    # unknown：截面日早于 MA 满窗 → 不计分段
    seg = regime_segments(closes_b, "BMK", [(days[0], 0.01), (days[1], 0.01)], "desc")
    assert seg["n_unknown_regime"] == 2
    with pytest.raises(Exception, match="无行情"):
        regime_segments({}, "BMK", [], "desc")


# ── ⑤ 增量价值（golden 端到端）─────────────────────────
@pytest.fixture
def m4(fl_db, fl_repo):
    db, _ = fl_db
    set_status(fl_repo, "val_pe_ttm", "active", who="m4", why="治理验收",
               gate={"ok": True, "reasons": []})       # M5：测试夹具显式门禁结论
    materialize(load_factor(fl_repo, "val_pe_ttm"), db, mode="full")
    return db, fl_repo


def test_incremental_gate_rejects_copy_factor(m4):
    """注入测试（协议 DoD）：val_pe_ttm 的仿制因子 ρ=1.0 → ⑤ 转替代通道。"""
    db, _ = m4
    con = duckdb.connect(str(db))
    try:
        con.execute("""INSERT INTO fl_factor_value_daily
                       SELECT dt, secid, 'val_pe_ttm_copy', value, factor_version,
                              'copy-lineage' FROM fl_factor_value_daily
                       WHERE factor_id = 'val_pe_ttm'""")
    finally:
        con.close()
    con = duckdb.connect(str(db), read_only=True)
    try:
        corr = cross_factor_corr(con, ["val_pe_ttm", "val_pe_ttm_copy"])
    finally:
        con.close()
    assert corr["matrix"][0][1] == pytest.approx(1.0)
    g = gate_incremental(corr, "val_pe_ttm_copy")
    assert g["pass"] is False and any("替代版本" in r for r in g["reasons"])
    # 正交因子（真值 vs 随机翻转）→ 不触发
    con = duckdb.connect(str(db))
    try:
        con.execute("""INSERT INTO fl_factor_value_daily
                       SELECT dt, secid, 'val_pe_anti', -value, factor_version,
                              'anti-lineage' FROM fl_factor_value_daily
                       WHERE factor_id = 'val_pe_ttm'""")
    finally:
        con.close()
    con = duckdb.connect(str(db), read_only=True)
    try:
        corr2 = cross_factor_corr(con, ["val_pe_ttm", "val_pe_anti"])
    finally:
        con.close()
    assert corr2["matrix"][0][1] == pytest.approx(-1.0)
    assert gate_incremental(corr2, "val_pe_anti")["pass"] is False   # |ρ| 同样 ≥ 0.6


# ── ④.2 参数网格（golden 端到端：变换链变体）───────────
def test_param_grid_transform_variants(m4):
    """机制验证：变体经临时 db 物化（主库零污染）、IC 重跑、同 transform → 方向不变。"""
    from applib.factor_governance import param_grid_eval
    db, repo = m4
    meta = load_factor(repo, "val_pe_ttm")
    variants = [{"label": "zscore", "transform": ["zscore"]},
                {"label": "rank_pct", "transform": ["rank_pct"]}]
    grid = param_grid_eval(db, repo, meta, variants, horizons=(3,),
                           start="2021-03-01", end="2021-06-30")
    assert len(grid["rows"]) == 2
    base = grid["base"]["reports"][3]["ic"]["directed_mean"]
    for r in grid["rows"]:
        assert r["directed_mean"] == pytest.approx(base, rel=1e-6)   # 保序变换 → IC 不变
    g = gate_param_robustness(grid)
    assert g["pass"] is True and g["reasons"] == []
    assert not list(repo.parent.glob("_grid_val_pe_ttm.duckdb"))      # 临时库已清理


def test_param_grid_repl_miss_raises(m4):
    from applib.factor_governance import GateError, param_grid_eval
    db, repo = m4
    meta = load_factor(repo, "val_pe_ttm")
    with pytest.raises(GateError, match="未命中"):
        param_grid_eval(db, repo, meta,
                        [{"label": "bad", "replace": [["不存在TOKEN", "x"]]}],
                        horizons=(3,), start="2021-03-01", end="2021-06-30")

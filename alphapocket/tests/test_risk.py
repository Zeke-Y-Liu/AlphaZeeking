"""P0 风险模型最小集测试（《黑箱框架对照与实施计划》P0 0.1–0.4 DoD）

- 0.1 波动目标：窗口不足不缩放 / 无前视（大跳变当日不含自身）/ 敞口夹逼
- 0.2 等风险：波动倒数权重归一 + 单票上限 water-filling；风险贡献方差下降
- 0.3 敞口：三维度（行业/规模/波动）逐月偏离 → σ
- 0.4 限额：单票 / 单行业 / 高波动降权硬规则生效
- 端到端：run_risk_model 总装 + 三格式产物落盘
运行：``cd alphapocket && python -m pytest tests/ -q``
"""
from __future__ import annotations

import datetime as dt
import math

import pytest

from applib import risk as RISK


# ── 0.1 波动率目标制 ────────────────────────────────────
def test_vol_target_no_lookahead_and_clip():
    # 20 日零波动窗口后接一根 +50% 巨跳；跳变当日 scale 只能由「跳变之前」决定
    rets = [0.0] * 20 + [0.5] + [0.0] * 20
    scales = RISK.vol_target_scale(rets, target_vol=0.15, window=20, min_scale=0.20)
    assert all(s == 1.0 for s in scales[:20])            # 窗口不足 → 不缩放
    assert scales[20] == 1.0                             # 零波动（跳变前）→ 满仓，未含跳变
    assert scales[21] == pytest.approx(0.20)             # 含跳变 → 波动极高 → 触下限
    assert all(0.20 <= s <= 1.0 for s in scales)         # 敞口夹逼在 [min, max]


# ── 0.2 等风险加权 ──────────────────────────────────────
def test_inverse_vol_weights_norm_and_cap():
    w = RISK.inverse_vol_weights({"A": 0.2, "B": 0.4})
    assert w["A"] == pytest.approx(2 / 3) and w["B"] == pytest.approx(1 / 3)
    assert sum(w.values()) == pytest.approx(1.0)

    # 3 资产（cap 可行域 [1/3,1]）：低波动 A 触顶 0.4，超额均分给 B/C
    wc = RISK.inverse_vol_weights({"A": 0.1, "B": 0.2, "C": 0.2}, cap=0.4)
    assert wc["A"] == pytest.approx(0.4)
    assert wc["B"] == pytest.approx(0.3) and wc["C"] == pytest.approx(0.3)
    assert sum(wc.values()) == pytest.approx(1.0)


def test_inverse_vol_weights_missing_vol_fallback():
    w = RISK.inverse_vol_weights({"A": 0.2, "B": None, "C": 0.4})
    assert sum(w.values()) == pytest.approx(1.0)
    assert w["B"] == pytest.approx(w["A"])               # 缺失波动按截面（上）中位数补


def test_risk_contrib_var_equal_risk_lower():
    vols = {"A": 0.1, "B": 0.4}
    eq_var = RISK.risk_contrib_var({"A": 0.5, "B": 0.5}, vols)
    er_var = RISK.risk_contrib_var(RISK.inverse_vol_weights(vols), vols)
    assert eq_var > 0
    assert er_var == pytest.approx(0.0, abs=1e-12)       # 等风险 → 风险贡献完全均衡
    assert er_var < eq_var


# ── 0.4 限额规则 ────────────────────────────────────────
def test_sector_of_primary_bucket():
    assert RISK.sector_of("汽车零部件·轮胎") == "汽车零部件"
    assert RISK.sector_of(None) == "未分类"


def test_apply_limits_max_single():
    w = {"A": 0.4, "B": 0.2, "C": 0.2, "D": 0.2}
    out, binding = RISK.apply_limits(w, {}, {}, {"max_single": 0.25})
    assert not binding or {"max_single"} <= {b["rule"] for b in binding}
    assert {b["rule"] for b in binding} == {"max_single"}
    assert max(out.values()) == pytest.approx(0.25)
    assert sum(out.values()) == pytest.approx(1.0)       # 超额在未触限证券间再分配


def test_apply_limits_max_industry():
    w = {"A": 0.3, "B": 0.3, "C": 0.4}
    sectors = {"A": "IND1", "B": "IND1", "C": "IND2"}
    out, binding = RISK.apply_limits(w, sectors, {}, {"max_industry": 0.5})
    assert {b["rule"] for b in binding} == {"max_industry"}
    ind = {}
    for s, v in out.items():
        ind[sectors[s]] = ind.get(sectors[s], 0.0) + v
    assert max(ind.values()) == pytest.approx(0.5)


def test_apply_limits_vol_downweight():
    w = {"A": 0.25, "B": 0.25, "C": 0.25, "D": 0.25}
    out, binding = RISK.apply_limits(
        w, {}, {"A": 0.60, "B": 0.10, "C": 0.10, "D": 0.10},
        {"vol_downweight": True, "vol_threshold": 0.5, "vol_factor": 0.7})
    assert {b["rule"] for b in binding} == {"vol_downweight"}
    assert out["A"] == pytest.approx(0.25 * 0.7)         # 高波动降权
    assert out["B"] == pytest.approx(0.25)               # 低波动不动
    assert sum(out.values()) < 1.0                       # 降权部分留现金（不重归一）


# ── 端到端：总装 + 三维度 + 三格式产物 ──────────────────
def _synth_bars(n: int = 520) -> dict:
    d0 = dt.date(2021, 1, 4)
    out = {}
    for k, amp in enumerate((0.003, 0.012, 0.025)):      # 三档波动，供等风险分辨
        px, arr = 100.0, []
        for i in range(n):
            r = amp * math.sin((i + k * 3) / 9.0)
            px *= 1 + r
            arr.append({"dt": (d0 + dt.timedelta(days=i)).isoformat(),
                        "open": round(px, 4), "high": round(px * 1.01, 4),
                        "low": round(px * 0.99, 4), "close": round(px, 4),
                        "volume": 1, "amount": 1.0, "pct": r})
        out[f"T{k}.XX"] = arr
    return out


def _ctx():
    bars = _synth_bars()
    secids = [f"T{k}.XX" for k in range(3)]
    pf = {"id": "TST", "name": "风险测试组合",
          "positions": [{"secid": s, "name": s} for s in secids]}
    dims = {s: {"industry": ind} for s, ind in zip(secids, ["行业甲·子", "行业乙·子", "行业甲·子"])}
    mcaps = {s: [["2021-01-04", v]] for s, v in zip(secids, [500.0, 100.0, 200.0])}
    return bars, pf, dims, mcaps, secids


def test_drawdown_episodes_peak_before_trough():
    # 先深跌、后长期新高、再浅回撤：旧实现会把历史最深谷错配到末端峰（peak 晚于 trough）
    navs = [1.0]
    for _ in range(5):
        navs.append(navs[-1] * 0.9)                  # 深跌至 ~0.59
    while navs[-1] < 1.5:
        navs.append(navs[-1] * 1.05)                 # 长期新高（修复）
    navs += [navs[-1] * 0.97, navs[-1] * 0.9]        # 未修复浅回撤
    series = [(f"d{i:03d}", v) for i, v in enumerate(navs)]
    eps = RISK.drawdown_episodes(series, top=3)
    assert eps and all(e["peak"] < e["trough"] for e in eps)   # 峰必早于谷
    assert eps[0]["depth"] < 0


def test_run_risk_model_end_to_end(tmp_path):
    bars, pf, dims, mcaps, secids = _ctx()
    rep = RISK.run_risk_model(bars, pf, dims, mcaps)
    assert set(rep["dod"]) == {"p0_1", "p0_2", "p0_3", "p0_4"}
    assert rep["secids"] == secids
    # 0.3 三维度可输出
    cur = rep["p0_3_exposure"]["current"]
    assert {"industry", "size", "vol"} <= set(cur)
    assert set(cur["industry"]["active"]) <= {"行业甲", "行业乙"}
    assert cur["size"]["active"] is not None             # 市值齐备 → 规模维度可算
    # 0.2 A/B 均带风险贡献方差读数
    assert "rc_var" in rep["p0_2_equal_risk"]["equal"]
    assert "rc_var" in rep["p0_2_equal_risk"]["equal_risk"]
    # 渲染与落盘
    assert RISK.render_md(rep) and RISK.render_html(rep)
    paths = RISK.write_all(rep, tmp_path / "risk_test")
    for k in ("json", "md", "html"):
        assert (tmp_path / "risk_test" / f"risk.{k}").exists()
    assert set(paths) == {"json", "md", "html"}


def test_exposure_report_size_dim_degraded_without_market_cap():
    bars, pf, dims, _, _ = _ctx()
    rep = RISK.exposure_report(bars, pf, dims, {})       # 无市值 → 规模维度降级
    assert rep["current"]["size"]["active"] is None
    assert rep["dod"]["ok"] is True                      # 降级不阻断敞口报告

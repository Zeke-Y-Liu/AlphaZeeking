"""P1 因子补齐测试（《黑箱框架对照与实施计划》P1 1.1–1.4 DoD）

- 1.1 反转族：``rev`` 超跌为正 / 样本不足降级；行业中性化
- 1.2 品质族：资产负债率 / 应计质量 / 自由现金流率比值 + 科目缺失降级
- 1.3 量价情绪：随引擎 B 窗口计算（``compute_beta`` 字段）
- 1.4 条件门：``ab_gate`` 仅在「多信号一致」看空时触发软衰减（≥2 卖出信号，权重 ×gate_damp）
- 增量价值关：截面相关工具（缺失因子 → 0 观测，守住品质因子数据缺口回归）
- A/B 对照：回撤改善符号（更接近 0 = 改善）
运行：``cd alphapocket && python -m pytest tests/ -q``
"""
from __future__ import annotations

import datetime as dt

import pytest

from applib import backtest as BT
from applib import factor_eval as FE
from applib import factors as F


# ── 合成数据 ────────────────────────────────────────────
def _bars(n: int, drift: float = 0.0005, base: float = 100.0) -> list[dict]:
    d0 = dt.date(2022, 1, 3)
    px, arr = base, []
    for i in range(n):
        px *= 1 + drift * (1 + 0.3 * ((i % 5) - 2))     # 加点噪声，避免常数列
        arr.append({"dt": (d0 + dt.timedelta(days=i)).isoformat(),
                    "open": round(px, 4), "high": round(px * 1.01, 4), "low": round(px * 0.99, 4),
                    "close": round(px, 4), "volume": 1000, "amount": 1000.0, "pct": drift})
    return arr


def _report(**over) -> dict:
    r = {"period": "2022-06-30", "announce_date": "2022-08-31", "eps_ytd": 2.0, "bps": 10.0,
         "roe": 20.0, "margin": 15.0, "rev_g": 10.0, "np_g": 12.0,
         "total_assets": 1000.0, "total_liab": 400.0, "ocf": 250.0, "capex": 50.0,
         "revenue": 800.0, "net_profit": 200.0}
    r.update(over)
    return r


# ── 1.2 品质族 ──────────────────────────────────────────
def test_quality_factors_ratios():
    q = F.quality_factors(_report())
    assert q["qua_leverage"] == pytest.approx(40.0)      # 400/1000×100
    assert q["qua_accrual"] == pytest.approx(1.25)       # 250/200
    assert q["qua_fcf"] == pytest.approx(25.0)           # (250−50)/800×100


def test_quality_factors_missing_degrades():
    assert F.quality_factors(None) == {"qua_leverage": None, "qua_accrual": None, "qua_fcf": None}
    # 仅财务摘要（无三表科目）→ 全 None（P1.2 数据缺口场景）
    q = F.quality_factors({"period": "2022-06-30"})
    assert all(v is None for v in q.values())


def test_compute_alpha_exposes_p1_fields():
    bars = _bars(400)
    reports = [_report()]
    a = F.compute_alpha("T.XX", bars, reports, [], as_of=bars[-1]["dt"])
    for k in ("rev_20d", "rev_60d", "qua_leverage", "qua_accrual", "qua_fcf"):
        assert k in a
    assert a["qua_leverage"] == pytest.approx(40.0)


# ── 1.1 反转 / 中性化 ───────────────────────────────────
def test_rev_sign_and_insufficient():
    up = [{"dt": f"d{i}", "close": 100.0 * (1.01 ** i)} for i in range(30)]
    down = [{"dt": f"d{i}", "close": 100.0 * (0.99 ** i)} for i in range(30)]
    assert F.rev("T", up, 20) < 0                        # 上涨 → 反转因子为负
    assert F.rev("T", down, 20) > 0                      # 超跌 → 为正（预期均值回复）
    assert F.rev("T", up, 60) is None                    # 样本不足 → None


def test_industry_neutralize():
    vals = {"A": 2.0, "B": 4.0, "C": 10.0}
    ind = {"A": "X", "B": "X", "C": "Y"}
    out = F.industry_neutralize(vals, ind)
    assert out["A"] == pytest.approx(-1.0) and out["B"] == pytest.approx(1.0)   # X 组去均值
    assert out["C"] == pytest.approx(0.0)                                        # 单只成组 → 0


# ── 1.4 条件门：多信号一致才剔除 ────────────────────────
def _two_stock_ctx():
    bars = {"A.XX": _bars(60), "B.XX": _bars(60, drift=0.0008)}
    pf = {"id": "TST", "name": "门测试", "positions": [
        {"secid": "A.XX", "name": "A"}, {"secid": "B.XX", "name": "B"}]}
    return bars, pf


def _patched_beta(n_sell_a: int):
    def fake_beta(secid, bs, as_of=None, **kw):
        if secid == "A.XX":
            return {"signals": [{"type": "卖出预警"}] * n_sell_a}
        return {"signals": []}
    return fake_beta


def test_ab_gate_multi_signal_dampens(monkeypatch):
    bars, pf = _two_stock_ctx()
    monkeypatch.setattr(BT.F, "compute_beta", _patched_beta(2))     # A 多信号一致看空
    res = BT.run_backtest(bars, pf, rebalance="monthly", ab_gate=True, gate_damp=0.5)
    g = res["gate"]
    assert g["gate_damp"] == 0.5 and g["redistribute"] is False
    assert g["n_flagged"] >= 1
    assert 0.0 < g["avg_invested_ratio"] < 1.0                     # 软衰减 → 降敞口但非清仓
    assert g["n_fully_derisked"] == 0
    assert all("B.XX" not in e["flagged"] for e in g["events"])    # B 从不触发
    assert all(e["flagged"] == ["A.XX"] for e in g["events"] if e["flagged"])


def test_ab_gate_single_signal_not_triggered(monkeypatch):
    bars, pf = _two_stock_ctx()
    monkeypatch.setattr(BT.F, "compute_beta", _patched_beta(1))     # 单一卖出信号 → 不触发
    res = BT.run_backtest(bars, pf, rebalance="monthly", ab_gate=True)
    assert res["gate"]["n_flagged"] == 0
    assert res["gate"]["avg_invested_ratio"] == pytest.approx(1.0)
    assert all(e["flagged"] == [] for e in res["gate"]["events"])


def test_ab_gate_hard_exclude_with_redistribute(monkeypatch):
    bars, pf = _two_stock_ctx()
    monkeypatch.setattr(BT.F, "compute_beta", _patched_beta(2))
    res = BT.run_backtest(bars, pf, rebalance="monthly", ab_gate=True,
                          gate_damp=0.0, gate_redistribute=True)
    g = res["gate"]
    assert g["gate_damp"] == 0.0 and g["redistribute"] is True
    assert g["n_flagged"] >= 1
    # 硬剔除 + 再分配 → 满仓（A 归零，额度给 B），不再出现「单票 100% 以外」的现金滞留
    assert all(abs(e["invested_ratio"] - 1.0) < 1e-6 for e in g["events"])
    assert g["n_fully_derisked"] == 0


def test_ab_gate_default_matches_anchor():
    bars, pf = _two_stock_ctx()
    base = BT.run_backtest(bars, pf, rebalance="monthly")
    assert base["ab_gate"] is False and base["gate"] is None


# ── 增量价值关工具 ──────────────────────────────────────
def _corr_ctx():
    bars = {s: _bars(420, drift=0.0003 * (k + 1)) for k, s in enumerate(("A.XX", "B.XX", "C.XX"))}
    fund = {s: {"reports": [], "dividends": []} for s in bars}
    return bars, fund


def test_incremental_identical_factor_fails():
    bars, fund = _corr_ctx()
    base = {"id": "mom_12_1", "type": "alpha", "name": "动量"}
    inc = FE.incremental_checks(base, ["mom_12_1"], {"factors": [base]},
                                list(bars), bars, fund)
    row = inc["rows"][0]
    assert row["n_dates"] > 0
    assert row["mean_rho"] == pytest.approx(1.0)         # 与自身 → ρ=1
    assert row["pass"] is False                          # 超阈值 → 拒绝（重复暴露）


def test_incremental_missing_factor_zero_dates():
    bars, fund = _corr_ctx()
    base = {"id": "mom_12_1", "type": "alpha", "name": "动量"}
    miss = {"id": "qua_absent", "type": "alpha", "name": "缺失"}
    inc = FE.incremental_checks(base, ["qua_absent"], {"factors": [base, miss]},
                                list(bars), bars, fund)
    row = inc["rows"][0]
    assert row["n_dates"] == 0 and row["mean_rho"] is None
    assert row["pass"] is False                          # 无有效取值 → 不通过（数据缺口即失败）


# ── A/B 对照：回撤符号 ──────────────────────────────────
def test_ab_compare_drawdown_sign(monkeypatch):
    def fake_run(bars, pf, **kw):
        if kw.get("ab_gate"):                            # 增强：收益更低 + 回撤更深
            m = {"total_return": 0.05, "cagr": 0.02, "ann_vol": 0.20,
                 "sharpe": 0.20, "max_dd": -0.15, "calmar": 0.15}
            return {"strategy_metrics": m, "gate": {"events": []}}
        m = {"total_return": 0.10, "cagr": 0.05, "ann_vol": 0.20,
             "sharpe": 0.40, "max_dd": -0.10, "calmar": 0.50}
        return {"strategy_metrics": m}

    monkeypatch.setattr(FE.BT, "run_backtest", fake_run)
    r = FE.ab_compare({}, {"id": "P", "name": "组合"})
    assert r["dd_improvement"] == pytest.approx(-0.05)   # 更深回撤 → 负（恶化）
    assert r["dod"]["dd_ok"] is False and r["dod"]["ok"] is False
    assert "恶化" in r["dod"]["detail"]


def test_run_p1_eval_end_to_end():
    bars, fund = _corr_ctx()
    base = {"id": "mom_12_1", "type": "alpha", "name": "动量"}
    leverage = {"id": "qua_leverage", "type": "alpha", "name": "资产负债率"}
    registry = {"factors": [base, leverage]}

    def loader(pid):
        return {"id": pid, "name": pid,
                "positions": [{"secid": s, "name": s} for s in bars]}

    rep = FE.run_p1_eval(["TST"], bars, fund, registry, loader)
    assert set(rep["dod"]) == {"incremental", "ab_gate", "ok"}
    assert rep["incremental"]["base"] == "mom_12_1"
    assert FE.render_md(rep) and FE.render_html(rep)


# ── M1 动态权重通道：rule_fn（与 ab_gate 互斥） ──────────
def test_rule_fn_identity_matches_equal_weight_anchor():
    """恒等 rule_fn（返回等权）→ 与默认等权回测逐位一致（主循环/成本语义零偏移）。"""
    bars, pf = _two_stock_ctx()
    base = BT.run_backtest(bars, pf, rebalance="monthly")
    secids = [p["secid"] for p in pf["positions"]]
    ruled = BT.run_backtest(bars, pf, rebalance="monthly",
                            rule_fn=lambda d: {s: 1.0 / len(secids) for s in secids})
    assert ruled["rule_fn"] is True and ruled["rule"]["n_rebalances"] == base["n_rebalances"] + 1
    assert ruled["series"] == base["series"]                       # 逐日 NAV 逐位一致
    assert ruled["strategy_metrics"] == base["strategy_metrics"]
    assert ruled["cost"] == base["cost"]


def test_rule_fn_daily_rotation_manual_nav():
    """daily 再平衡 + 零成本 → NAV = ∏(1+Σw_d·r_d) 手工逐位推演（A 前半全仓 → B 后半全仓）。"""
    bars = {"A.XX": _bars(20, drift=0.002), "B.XX": _bars(20, drift=-0.001)}
    pf = {"id": "ROT", "name": "轮动", "positions": [
        {"secid": "A.XX", "name": "A"}, {"secid": "B.XX", "name": "B"}]}
    dates = sorted({r["dt"] for r in bars["A.XX"]})
    half = dates[len(dates) // 2]

    def rule(d):
        return {"A.XX": 1.0, "B.XX": 0.0} if d < half else {"A.XX": 0.0, "B.XX": 1.0}

    res = BT.run_backtest(bars, pf, rebalance="daily", cost_bps=0, rule_fn=rule,
                          benchmark_secid=None)
    rmap = {s: {r["dt"]: r["pct"] for r in arr} for s, arr in bars.items()}
    nav, want = 1.0, []
    w = rule(dates[0])                    # 期初建仓：当日生效
    for d in dates:                       # 手工推演：当日收益用昨日收盘再平衡后的权重
        pr = sum(w[s] * (rmap[s].get(d) or 0.0) for s in w)
        nav *= 1 + pr
        want.append((d, nav))
        w = rule(d)                       # 收盘再平衡 → 次日生效（收盘价执行口径）
    assert res["series"] == want          # 逐位一致（零成本 + 每日回目标 → 无漂移）
    assert all(e["invested_ratio"] == 1.0 for e in res["rule"]["events"])


def test_rule_fn_cash_remaining():
    """Σw < 1 留现金语义：invested_ratio 登记 0.5，pr = 0.5·rA 手工逐位一致。"""
    bars = {"A.XX": _bars(10, drift=0.003), "B.XX": _bars(10, drift=0.0)}
    pf = {"id": "CSH", "name": "半仓", "positions": [
        {"secid": "A.XX", "name": "A"}, {"secid": "B.XX", "name": "B"}]}
    dates = sorted({r["dt"] for r in bars["A.XX"]})
    res = BT.run_backtest(bars, pf, rebalance="daily", cost_bps=0,
                          rule_fn=lambda d: {"A.XX": 0.5}, benchmark_secid=None)
    rmap = {s: {r["dt"]: r["pct"] for r in arr} for s, arr in bars.items()}
    nav, want = 1.0, []
    for d in dates:                       # 权重恒定 → 无时序歧义：pr = 0.5·rA
        nav *= 1 + 0.5 * (rmap["A.XX"].get(d) or 0.0)
        want.append((d, nav))
    assert res["series"] == want
    assert res["rule"]["avg_invested_ratio"] == 0.5


def test_rule_fn_validation_rejects():
    """契约校验：负权重 / 杠杆 / 零持仓 / 非 dict / 未声明标的 → 显式报错不静默修正。"""
    bars, pf = _two_stock_ctx()
    cases = [
        (lambda d: {"A.XX": -0.5, "B.XX": 1.0}, "负权重"),
        (lambda d: {"A.XX": 1.2, "B.XX": 0.3}, "杠杆"),
        (lambda d: {"A.XX": 0.0, "B.XX": 0.0}, "无有效持仓"),
        (lambda d: [("A.XX", 0.5)], "须返回"),
        (lambda d: {"A.XX": 0.5, "X.XX": 0.5}, "未声明标的"),
        (lambda d: {"A.XX": True, "B.XX": 0.5}, "非数值"),
    ]
    for fn, kw in cases:
        with pytest.raises(ValueError, match=kw):
            BT.run_backtest(bars, pf, rebalance="daily", rule_fn=fn)


def test_rule_fn_mutually_exclusive():
    bars, pf = _two_stock_ctx()
    with pytest.raises(ValueError, match="互斥"):
        BT.run_backtest(bars, pf, rule_fn=lambda d: {"A.XX": 0.5, "B.XX": 0.5}, ab_gate=True)
    with pytest.raises(ValueError, match="互斥"):
        BT.run_backtest(bars, pf, rule_fn=lambda d: {"A.XX": 0.5, "B.XX": 0.5},
                        risk_limits={"max_single": 0.4})


def test_rule_fn_default_off():
    """默认不传 rule_fn → 通道关闭，产物结构不含 rule 事件（锚点不受影响）。"""
    bars, pf = _two_stock_ctx()
    res = BT.run_backtest(bars, pf, rebalance="monthly")
    assert res["rule_fn"] is False and res["rule"] is None

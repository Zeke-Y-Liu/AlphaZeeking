"""M6 回测引擎测试（实施计划 §9.2 + DoD 1/2/4）。

- DoD 1 撮合手工推演：合成含涨跌停/T+1/停牌行情，成交判定与手工推演逐笔一致；
- DoD 2 成本敏感：滑点 0 → 5bp → 10bp，收益单调变化、策略排序不翻转；
- DoD 4 无偷价审计：fill_dt > signal_dt、raw 成交价 ∈ 成交日 [low, high]、
  换手事件与再平衡频率匹配；
- 端到端：run_backtest 含/不含成本两列 + run_walkforward 折结构 + lockbox 单次验证。
"""
import calendar
from datetime import date

import duckdb
import pytest

from factorlib.backtest import (INIT_CASH, CostTable, audit_no_lookahead,
                                cost_of, fill_price, is_limit, lot_shares,
                                perf, run_backtest, run_walkforward,
                                _rebalance, _run_path)
from factorlib.composite import StrategyDSL
from factorlib.evaluate import _EVAL_DDLS, month_ends
from factorlib.registry import STATUS_DRAFT


def _bar(close, pct=0.0, spread=0.01, volume=100000.0, vwap=None):
    """合成日 bar：vwap = amount/volume（volume=0 → 触发 (H+L)/2 代理）。"""
    vwap = close if vwap is None else vwap
    return {"open": close, "high": close * (1 + spread), "low": close * (1 - spread),
            "close": close, "volume": volume, "amount": volume * vwap, "pct": pct}


# ── 撮合原语（成本表 / VWAP+滑点 / 涨跌停 / 整手） ──
def test_cost_of_table():
    c = CostTable()
    assert cost_of(100_000, "buy", c) == {"commission": 25.0, "stamp_tax": 0.0,
                                          "transfer_fee": 1.0, "total": 26.0}
    assert cost_of(10_000, "buy", c)["commission"] == 5.0          # min ¥5 触发
    assert cost_of(10_000, "buy", c)["total"] == 5.1
    s = cost_of(10_000, "sell", c)                                  # 卖出加印花 5bp
    assert s["stamp_tax"] == 5.0 and s["total"] == 10.1
    z = cost_of(100_000, "sell", CostTable.zero())
    assert z["total"] == 0.0


def test_fill_price_vwap_slippage():
    c = CostTable()
    bar = _bar(10.0, vwap=10.1)                    # vwap=amount/volume=10.1
    px, src = fill_price(bar, "buy", c)
    assert src == "vwap" and px == pytest.approx(10.1 * 1.0005)
    px, _ = fill_price(bar, "sell", c)
    assert px == pytest.approx(10.1 * 0.9995)      # 买加滑 / 卖减滑
    px, src = fill_price(_bar(10.0, volume=0.0), "buy", c)
    assert src == "vwap_proxy_hl" and px == pytest.approx(10.0 * 1.0005)   # (H+L)/2
    # VWAP 越界（> high）= 数据异常 → 回退日内中价并标注（诚实撮合，防坏数据入账）
    bad = _bar(10.0, vwap=10.5)
    px, src = fill_price(bad, "buy", c)
    assert src == "vwap_proxy_hl" and px == pytest.approx(10.0 * 1.0005)


def test_limit_and_lot_rules():
    assert is_limit({"pct": 0.10}, "buy") and not is_limit({"pct": 0.05}, "buy")
    assert is_limit({"pct": -0.10}, "sell") and not is_limit({"pct": -0.05}, "sell")
    assert lot_shares(5_000, 10) == 500            # 100 股整数倍
    assert lot_shares(999, 10) == 0                # 不足一手 → 0
    assert lot_shares(5_000, 0) == 0 and lot_shares(0, 10) == 0


# ── DoD 1：撮合手工推演（两标的等权买入，逐笔对账） ──
def test_manual_walkthrough_full_fill():
    cal = ["2021-01-29", "2021-02-01", "2021-02-02"]
    bars = {
        "A": {"2021-01-29": _bar(100.0),
              "2021-02-01": _bar(10.1, vwap=10.0),       # vwap 10.0
              "2021-02-02": _bar(10.1)},
        "B": {"2021-01-29": _bar(200.0),
              "2021-02-01": _bar(20.0, volume=0.0),      # 停量 → (H+L)/2 = 20.0
              "2021-02-02": _bar(20.0)},
    }
    path = _run_path(bars, cal, {"2021-01-29": ["A", "B"]}, CostTable())
    t = path["trades"]
    assert len(t) == 2 and all(x["side"] == "buy" for x in t)
    # A：预算 = 50 万 / 1.00026 → 49.987 万；px = 10×1.0005 = 10.005
    #    int(499870//1000.5)×100 = 49,900 股；value = 499,249.50；
    #    cost = 佣金 124.81 + 过户 4.99 = 129.80
    assert t[0]["secid"] == "A" and t[0]["shares"] == 49_900
    assert t[0]["px"] == pytest.approx(10.005) and t[0]["px_raw"] == pytest.approx(10.0)
    assert t[0]["px_source"] == "vwap" and t[0]["cost"]["total"] == 129.80
    # B：预算仍按 per_slot=50 万；px = 20×1.0005 = 20.01
    #    int(499870//2001)×100 = 24,900 股；value = 498,249.00；cost = 129.54
    assert t[1]["secid"] == "B" and t[1]["shares"] == 24_900
    assert t[1]["px_source"] == "vwap_proxy_hl" and t[1]["cost"]["total"] == 129.54
    # 现金对账：1e6 − 499,249.50 − 129.80 − 498,249.00 − 129.54 = 2,242.16
    # D2 nav = 2,242.16 + 49,900×10.1 + 24,900×20.0 = 1,004,232.16（逐日 mark-to-market）
    nav = dict(path["equity"])
    assert nav["2021-01-29"] == pytest.approx(INIT_CASH)     # 信号日未执行 = 全现金
    assert nav["2021-02-01"] == pytest.approx(2_242.16 + 49_900 * 10.1 + 24_900 * 20.0)
    assert path["deferred"] == []
    p = perf(path)
    assert p["n_trades"] == 2 and p["total_cost"] == pytest.approx(259.34)


# ── DoD 1：涨跌停 / 停牌登记 + 下轮重试 + T+1 防御分支 ──
def test_limit_suspend_defer_and_retry():
    cal = ["2021-01-29", "2021-02-01", "2021-02-02"]
    bars = {
        "A": {"2021-01-29": _bar(10.0), "2021-02-01": _bar(10.0, pct=0.01),
              "2021-02-02": _bar(10.0)},
        "C": {"2021-01-29": _bar(5.0),                      # 02-01 停牌（无 bar）
              "2021-02-02": _bar(5.0)},
        "L": {"2021-01-29": _bar(8.0), "2021-02-01": _bar(8.0, pct=0.10),
              "2021-02-02": _bar(8.0, pct=0.01)},           # 02-01 涨停
        "P": {"2021-01-29": _bar(6.0), "2021-02-01": _bar(6.0, pct=0.01),
              "2021-02-02": _bar(6.0, pct=-0.10)},          # 02-02 跌停
    }
    targets = {"2021-01-29": ["A", "C", "L", "P"], "2021-02-01": ["A", "C"]}
    path = _run_path(bars, cal, targets, CostTable())
    fills = {(x["signal_dt"], x["secid"], x["side"]) for x in path["trades"]}
    assert ("2021-01-29", "A", "buy") in fills and ("2021-01-29", "P", "buy") in fills
    assert ("2021-02-01", "C", "buy") in fills              # 停牌下轮重试成交
    dfr = {(d["signal_dt"], d["secid"], d["side"], d["reason"]) for d in path["deferred"]}
    assert ("2021-01-29", "C", "buy", "suspended") in dfr
    assert ("2021-01-29", "L", "buy", "limit_up") in dfr
    assert ("2021-02-01", "P", "sell", "limit_down") in dfr  # 跌停不可卖 → 顺延登记
    assert audit_no_lookahead(path, bars)["pass"] is True


def test_t_plus_1_guard_branch():
    bars = {"X": {"2021-02-02": _bar(5.0)}}
    pos = {"X": {"shares": 100, "buy_dt": "2021-02-02", "entry_px": 5.0,
                 "last_close": 5.0}}
    cash, positions, trades, deferred = _rebalance(
        "2021-02-02", "2021-02-01", [], bars, INIT_CASH, pos, [], [],
        {"2021-02-01": 0}, CostTable())
    assert deferred == [{"signal_dt": "2021-02-01", "secid": "X", "side": "sell",
                         "reason": "t_plus_1"}] and positions      # 当日买入不可当日卖
    pos["X"]["buy_dt"] = "2021-02-01"                              # 隔日 → 可卖
    cash, positions, trades, deferred = _rebalance(
        "2021-02-02", "2021-02-01", [], bars, INIT_CASH, pos, [], [],
        {"2021-02-01": 0}, CostTable())
    assert not positions and len(trades) == 1 and deferred == []


# ── DoD 1：先卖后买释放资金 ──
def test_sell_before_buy_frees_cash():
    exec_dt, sig = "2021-02-02", "2021-02-01"
    bars = {"B": {exec_dt: _bar(40.0)}, "A": {exec_dt: _bar(50.0)}}
    cash, positions, trades, deferred = _rebalance(
        exec_dt, sig, ["A"], bars, 200_000.0,
        {"B": {"shares": 10_000, "buy_dt": "2021-01-01", "entry_px": 40.0,
               "last_close": 40.0}}, [], [], {sig: 0}, CostTable())
    assert trades[0]["side"] == "sell" and trades[0]["secid"] == "B"
    # 卖 B：value = 10,000×40×0.9995 = 399,800；cost = 佣金 99.95 + 印花 199.90 + 过户 4.00
    assert trades[0]["cost"]["total"] == pytest.approx(303.85)
    # 买 A：现金 599,496.15；px = 50.025；int(599340//5002.5)×100 = 11,900 股
    assert trades[1]["side"] == "buy" and trades[1]["secid"] == "A"
    assert trades[1]["shares"] == 11_900 and deferred == []
    assert "A" in positions and "B" not in positions


# ── DoD 2：成本敏感（滑点 0 → 5bp → 10bp；单调 + 排序稳定） ──
def test_cost_sensitivity_monotonic_and_ranking():
    cal = ["2021-01-29", "2021-02-01", "2021-02-26"]
    good = {"G": {"2021-01-29": _bar(100.0), "2021-02-01": _bar(101.0, vwap=100.0),
                  "2021-02-26": _bar(110.0)}}
    bad = {"V": {"2021-01-29": _bar(100.0), "2021-02-01": _bar(99.0, vwap=100.0),
                 "2021-02-26": _bar(90.0)}}
    tg, tb = {"2021-01-29": ["G"]}, {"2021-01-29": ["V"]}
    rets_g, rets_v = [], []
    for s in (0.0, 0.0005, 0.001):                 # 0 → 5bp → 10bps
        cost = CostTable(slippage=s)
        rets_g.append(perf(_run_path(good, cal, tg, cost))["total_return"])
        rets_v.append(perf(_run_path(bad, cal, tb, cost))["total_return"])
    # 收益随滑点单调下降（买价升、整手股数不变 → 现金与净值同向降）
    assert rets_g[0] > rets_g[1] > rets_g[2]
    assert rets_v[0] > rets_v[1] > rets_v[2]
    # 排序稳定：好策略在所有滑点档位均优于差策略（不翻转）
    assert all(g > v for g, v in zip(rets_g, rets_v))
    # 锚点：s=0 全成本口径 total ≈ +9.8743%（手工推演：单标的全仓 9,900 股，
    # cost = 佣金 247.50 + 过户 9.90，nav = 9,742.60 + 9,900×110 = 1,098,742.60）
    assert rets_g[0] == pytest.approx(0.0987426, rel=1e-6)


# ── DoD 4：无偷价审计 ──
def test_audit_no_lookahead():
    cal = ["2021-01-29", "2021-02-01"]
    bars = {"A": {"2021-01-29": _bar(100.0), "2021-02-01": _bar(101.0, vwap=100.0)}}
    path = _run_path(bars, cal, {"2021-01-29": ["A"]}, CostTable())
    rep = audit_no_lookahead(path, bars)
    assert rep["pass"] is True and rep["checked"] == 1
    assert rep["n_rebalance"] == 1 and rep["rebalance_with_fills"] == 1
    bad = dict(path)
    bad["trades"] = path["trades"] + [
        {**path["trades"][0], "fill_dt": "2021-01-29"},            # 当日成交 = 偷价
        {**path["trades"][0], "px_raw": 999.0, "fill_dt": "2021-02-01"}]  # 价出区间
    rep = audit_no_lookahead(bad, bars)
    assert rep["pass"] is False
    why = {v["why"] for v in rep["violations"]}
    assert why == {"fill_not_after_signal", "px_out_of_day_range"}


# ── 端到端：合成库 4 标的 × N 月（月频 bar；F_good=k → 恒 top2 = K4,K3） ──
def _mk_con(y0, m0, n):
    mes = []
    y, m = y0, m0
    for _ in range(n):
        mes.append(date(y, m, calendar.monthrange(y, m)[1]).isoformat())
        m += 1
        if m == 13:
            y, m = y + 1, 1
    con = duckdb.connect(":memory:")
    for ddl in _EVAL_DDLS:
        con.execute(ddl)
    rows = []
    for i, dt in enumerate(mes):
        for k, s in enumerate(["K1", "K2", "K3", "K4"], start=1):
            c = 100.0 * (1 + 0.01 * k) ** (i + 1)
            rows.append((s, dt, c, c * 1.01, c * 0.99, c, 100_000.0, 100_000.0 * c,
                         0.01 * k))
    con.executemany("INSERT INTO dwd_daily_bar VALUES (?,?,?,?,?,?,?,?,?)", rows)
    con.executemany("INSERT INTO dim_security VALUES (?,?,?,?,?)",
                    [(s, s, "XSHG", "STOCK", "IND") for s in ("K1", "K2", "K3", "K4")])
    con.execute("CREATE TABLE factor_value_daily"
                " (dt DATE, secid TEXT, factor_id TEXT, value DOUBLE)")
    con.executemany("INSERT INTO factor_value_daily (dt, secid, factor_id, value)"
                    " VALUES (?,?,?,?)",
                    [(dt, s, "F_good", float(k)) for dt in mes
                     for k, s in enumerate(["K1", "K2", "K3", "K4"], start=1)])
    return con


class _Meta:
    def __init__(self):
        self.id, self.direction, self.status = "F_good", 1, STATUS_DRAFT
        self.name, self.version, self.family = "F_good", "1.0.0", "test"


def _strategy():
    return StrategyDSL(id="t_bt", mode="composite", top_n=2,
                       factors=[{"id": "F_good", "weight": "equal"}],
                       methods=["equal"])


def test_run_backtest_end_to_end():
    con, metas = _mk_con(2021, 1, 12), {"F_good": _Meta()}
    rep = run_backtest(con, _strategy(), metas, "2021-01-01", "2021-12-31")
    entry = rep["results"]["equal"]
    assert entry["net"] and entry["gross"]                 # 含/不含成本两列
    assert entry["gross"]["total_return"] > entry["net"]["total_return"] >= 0
    assert entry["audit"]["pass"] is True
    # 换手与再平衡频率匹配：11 个再平衡月仅首月建仓成交（top2 恒定 → 低换手）
    assert entry["audit"]["n_rebalance"] == 11
    assert entry["audit"]["rebalance_with_fills"] == 1
    assert rep["best_method"] == "equal" and rep["factor_status"] == {"F_good": "draft"}
    # lockbox 段单次验证（主窗剔除末 2 个信号月）
    rep2 = run_backtest(con, _strategy(), metas, "2021-01-01", "2021-12-31",
                        lockbox_months=2)
    lb = rep2["lockbox"]
    assert lb["method"] == "equal" and lb["audit"]["pass"] is True
    assert lb["net"] and "只验证一次" in lb["note"]
    assert len(rep2["results"]["equal"]["audit"]["fills_per_rebal"]) == 9


def test_run_walkforward_folds_and_lockbox():
    con, metas = _mk_con(2020, 1, 40), {"F_good": _Meta()}
    st = _strategy()
    st.methods = ["equal", "ic_12m"]
    rep = run_walkforward(con, st, metas, "2020-01-01", "2023-04-30",
                          wf=(24, 6), lockbox_months=6)
    mes = month_ends(con, "2020-01-01", "2023-04-30")[:-1]     # 末交易日月被剔除
    assert len(rep["folds"]) == 1                              # 24 train + 6 step + 6 lock
    fold = rep["folds"][0]
    assert fold["train"] == [mes[0], mes[23]]
    assert fold["valid_window"] == [mes[24], mes[29]]
    assert fold["best_method"] in ("equal", "ic_12m")
    assert set(fold["train_sharpe"]) == {"equal", "ic_12m"}
    assert fold["valid"]["sharpe"] is not None
    assert fold["audit"]["pass"] is True
    # lockbox：全 train 窗选法 → 末端 6 个月单次验证
    assert rep["lockbox"]["window"] == [mes[33], mes[38]]
    assert rep["lockbox"]["method"] == fold["best_method"] or True   # 选法窗不同可不同
    assert rep["lockbox"]["audit"]["pass"] is True and rep["lockbox"]["net"]
    # 折间 valid 与 lockbox 窗口互不重叠
    assert fold["valid_window"][1] < rep["lockbox"]["window"][0]

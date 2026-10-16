"""M6 T-C3 撮合精验测试：次日 VWAP + 成本表 + 交易约束 + 无偷价审计。

手工推演（golden fixture，期望值独立于被测代码）：三标的行情——
A 正常成交（02-01 VWAP=10.2）、B 一字涨停 defer 1 日（02-02 VWAP=22.8）、
C 停牌 skip（02-01 volume=0）；02-28 清仓决策（03-01 VWAP A=10.4 / B=23.0）。
费用表 commission 0.1%（无最低/印花/过户/滑点）→ 全链路现金逐笔可手工对账：
买入 A 50,000 股 ×10.2 = 510,000（费 510）、B 12,500 股 ×22.8 = 285,000（费 285）；
卖出 A 520,000（费 520）、B 287,500（费 287.5）→ 期末现金 1,010,897.5。
运行：``cd alphapocket && python -m pytest tests/test_matching.py -q``
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from applib import backtest as BT
from applib.factor_backtest import FactorBacktestError, run_factor_backtest
from factorlib import load_factor, set_status
from factorlib.materialize import materialize


def _bar(dt, o, h, l, c, vol=1000, amt=None):
    """合成 bar：amt 缺省 = vol × (o+h+l+c)/4（与 VWAP 无关，仅占位）。"""
    return {"dt": dt, "open": o, "high": h, "low": l, "close": c,
            "volume": vol, "amount": amt if amt is not None else vol * (o + h + l + c) / 4,
            "pct": None}


def _bars_3() -> dict:
    """三标的手工推演行情（A 正常 / B 一字板 / C 停牌）。"""
    return {
        "600001.SH": [
            _bar("2024-01-30", 10.0, 10.2, 9.9, 10.0),
            _bar("2024-01-31", 10.0, 10.2, 9.9, 10.0),
            _bar("2024-02-01", 10.0, 10.4, 9.8, 10.2, amt=10200),      # VWAP=10.2
            _bar("2024-02-28", 10.2, 10.3, 10.1, 10.2),
            _bar("2024-03-01", 10.3, 10.5, 10.2, 10.4, amt=10400),     # VWAP=10.4
        ],
        "600002.SH": [
            _bar("2024-01-30", 20.0, 20.4, 19.8, 20.0),
            _bar("2024-01-31", 20.0, 20.4, 19.8, 20.0),
            _bar("2024-02-01", 22.0, 22.0, 22.0, 22.0, vol=100, amt=2200),   # 一字涨停
            _bar("2024-02-02", 22.4, 23.0, 22.0, 22.8, amt=22800),     # VWAP=22.8
            _bar("2024-02-28", 22.8, 22.9, 22.7, 22.8),
            _bar("2024-03-01", 22.9, 23.1, 22.8, 23.0, amt=23000),     # VWAP=23.0
        ],
        "600003.SH": [
            _bar("2024-01-30", 40.0, 40.5, 39.5, 40.0),
            _bar("2024-01-31", 40.0, 40.5, 39.5, 40.0),
            _bar("2024-02-01", 40.0, 40.0, 40.0, 40.0, vol=0, amt=0),  # 停牌
            _bar("2024-02-28", 40.0, 40.4, 39.6, 40.0),
        ],
    }


def _pf3() -> dict:
    return {"id": "match3", "name": "撮合精验",
            "positions": [{"secid": "600001.SH", "name": "A"},
                          {"secid": "600002.SH", "name": "B"},
                          {"secid": "600003.SH", "name": "C"}]}


RULE3 = {"2024-01-31": {"600001.SH": 0.5, "600002.SH": 0.25, "600003.SH": 0.25},
         "2024-02-28": {}, "2024-03-01": {}}

CT_A = {"commission_rate": 0.001, "commission_min_cny": 0.0,
        "stamp_tax_rate": 0.0, "transfer_fee_rate": 0.0, "slippage_bps": 0.0}
CONS_A = {"t_plus_1": False, "lot_size": 100, "limit_move": "defer", "suspension": "skip"}


def _run3(**kw) -> dict:
    base = dict(rebalance="monthly", benchmark_secid="", rule_fn=lambda d: RULE3.get(d, {}),
                exec_mode="next_vwap", cost_table=CT_A, constraints=CONS_A,
                capital=1_000_000.0)
    base.update(kw)
    return BT.run_backtest(_bars_3(), _pf3(), **base)


def _bars_one(px=10.0, buy_vwap=None, sell_vwap=10.5) -> dict:
    """单标的四日行情：01-31 决策（close=px）→ 02-01 买入 → 02-28 清仓决策 → 03-01 卖出。"""
    bv = px if buy_vwap is None else buy_vwap
    return {"600100.SH": [
        _bar("2024-01-31", px, px * 1.01, px * 0.99, px),
        _bar("2024-02-01", bv, bv * 1.01, bv * 0.99, bv, vol=10000, amt=bv * 10000),
        _bar("2024-02-28", px, px * 1.01, px * 0.99, px),
        _bar("2024-03-01", sell_vwap, sell_vwap * 1.01, sell_vwap * 0.99, sell_vwap,
             vol=10000, amt=sell_vwap * 10000),
    ]}


_PF1 = {"id": "match1", "name": "单标的", "positions": [{"secid": "600100.SH", "name": "X"}]}


def _rule_one(w: float):
    return lambda d: {"600100.SH": w} if d == "2024-01-31" else {}


def _run_one(bars=None, w=0.98, ct=None, cons=None, capital=100_000.0,
             px=10.0, buy_vwap=10.2, **kw) -> dict:
    base = dict(rebalance="monthly", benchmark_secid="", rule_fn=_rule_one(w),
                exec_mode="next_vwap", cost_table=ct, constraints=cons, capital=capital)
    base.update(kw)
    return BT.run_backtest(bars or _bars_one(px=px, buy_vwap=buy_vwap), _PF1, **base)


# ── 单元：成本表 order_fees ─────────────────────────────
def test_fees_buy_min_boundary():
    """佣金 min¥5 边界：amt×0.025% < 5 → 取 5；=5 与 >5 按比例。"""
    assert BT.order_fees(19_999.0, "buy")["commission"] == 5.0          # 4.99975 → min
    assert BT.order_fees(20_000.0, "buy")["commission"] == 5.0          # 边界恰等
    assert BT.order_fees(100_000.0, "buy")["commission"] == 25.0        # 万2.5 → 25
    assert BT.order_fees(100_000.0, "buy", {"commission_min_cny": 0.0})["commission"] == 25.0


def test_fees_sell_stamp_and_transfer():
    """印花税只计卖出（stamp_tax_side="sell" 缺省）；过户费双边。"""
    b = BT.order_fees(100_000.0, "buy")
    s = BT.order_fees(100_000.0, "sell")
    assert b["stamp_tax"] == 0.0 and s["stamp_tax"] == 50.0             # 0.05% 卖出
    assert b["transfer_fee"] == 1.0 and s["transfer_fee"] == 1.0        # 0.001% 双边
    assert s["commission"] == 25.0


def test_fees_custom_table_and_side():
    ct = {"commission_rate": 0.0, "commission_min_cny": 0.0, "stamp_tax_rate": 0.001,
          "stamp_tax_side": "buy", "transfer_fee_rate": 0.0}
    assert BT.order_fees(10_000.0, "buy", ct) == {"commission": 0.0, "stamp_tax": 10.0,
                                                  "transfer_fee": 0.0}
    assert BT.order_fees(10_000.0, "sell", ct)["stamp_tax"] == 0.0      # side=buy → 卖出免


# ── 单元：成交价 / 整手 / 可成交日 ──────────────────────
def test_fill_price_vwap_avg4_close():
    assert BT._fill_price({"volume": 1000, "amount": 10200})[0] == pytest.approx(10.2)
    assert BT._fill_price({"volume": 1000, "amount": 10200})[1] == "vwap"
    r = {"open": 10.0, "high": 10.4, "low": 9.6, "close": 10.0, "volume": 100, "amount": 0}
    assert BT._fill_price(r) == (10.0, "avg4")                          # amount=0 → avg4 兜底
    assert BT._fill_price({"close": 10.0}) == (10.0, "close")
    assert BT._fill_price({})[0] is None


def test_lot_round_semantics():
    assert BT._lot_round(31_499.9, 100) == 31_400                       # 向下取整手
    assert BT._lot_round(31_400.0, 100) == 31_400
    assert BT._lot_round(250.0, 100) == 200
    assert BT._lot_round(31_499.0, 0) == 31_499                         # lot=0 → 整股
    assert BT._lot_round(-3.0, 0) == 0


def _urow(dt, vol=1000, hl=None):
    h, l = hl if hl else (11.0, 9.0)
    return {"dt": dt, "volume": vol, "high": h, "low": l, "close": 10.0}


def test_find_exec_normal_and_limit_defer():
    rows = [_urow("2024-01-02"), _urow("2024-01-03")]
    r, i = BT._find_exec(rows, "2024-01-01", direction="buy", t_plus_1=False,
                         earliest_sell=None, limit_pol="defer", susp_pol="defer")
    assert (r["dt"], i) == ("2024-01-02", 0)
    one_line = [_urow("2024-01-02", hl=(10.0, 10.0)), _urow("2024-01-03")]
    r, i = BT._find_exec(one_line, "2024-01-01", direction="buy", t_plus_1=False,
                         earliest_sell=None, limit_pol="defer", susp_pol="defer")
    assert (r["dt"], i) == ("2024-01-03", 1)                            # 一字板 defer 1 日


def test_find_exec_skip_policies():
    one_line = [_urow("2024-01-02", hl=(10.0, 10.0))]
    assert BT._find_exec(one_line, "2024-01-01", direction="buy", t_plus_1=False,
                         earliest_sell=None, limit_pol="skip",
                         susp_pol="defer") == (None, "limit_move")
    susp = [_urow("2024-01-02", vol=0)]
    assert BT._find_exec(susp, "2024-01-01", direction="buy", t_plus_1=False,
                         earliest_sell=None, limit_pol="defer",
                         susp_pol="skip") == (None, "suspension")


def test_find_exec_suspension_defer_and_cap():
    rows = [_urow("2024-01-02", vol=0), _urow("2024-01-03")]
    r, i = BT._find_exec(rows, "2024-01-01", direction="buy", t_plus_1=False,
                         earliest_sell=None, limit_pol="defer", susp_pol="defer")
    assert (r["dt"], i) == ("2024-01-03", 1)                            # 停牌 defer
    cap = [_urow(f"2024-01-{d:02d}", hl=(10.0, 10.0)) for d in range(2, 8)]   # 6 连一字板
    assert BT._find_exec(cap, "2024-01-01", direction="buy", t_plus_1=False,
                         earliest_sell=None, limit_pol="defer",
                         susp_pol="defer") == (None, "defer_cap")       # 超 5 日上限


def test_find_exec_t_plus_1_blocks_early_sell():
    rows = [_urow("2024-01-02"), _urow("2024-01-03"), _urow("2024-01-04")]
    kw = dict(direction="sell", t_plus_1=True, earliest_sell="2024-01-03",
              limit_pol="defer", susp_pol="defer")
    r, i = BT._find_exec(rows, "2024-01-01", **kw)
    assert (r["dt"], i) == ("2024-01-03", 1)                            # 01-02 被 T+1 挡下
    kw2 = dict(kw, earliest_sell="2024-01-05")
    assert BT._find_exec(rows, "2024-01-01", **kw2) == (None, "defer_cap")
    kw3 = dict(direction="sell", t_plus_1=False, earliest_sell="2024-01-05",
               limit_pol="defer", susp_pol="defer")
    assert BT._find_exec(rows, "2024-01-01", **kw3)[0]["dt"] == "2024-01-02"   # 关闭则不挡


# ── 集成：三标的手工推演对账（DoD a）────────────────────
def test_manual_reconciliation_3_targets():
    """逐笔成交价、股数、费用、现金与手工表 bit 级一致（正常/一字板 defer/停牌 skip）。"""
    bt = _run3()
    os_ = bt["exec_orders"]
    assert [(o["secid"], o["direction"], o["exec_dt"], o["shares"]) for o in os_] == [
        ("600001.SH", "buy", "2024-02-01", 50_000),
        ("600002.SH", "buy", "2024-02-02", 12_500),
        ("600001.SH", "sell", "2024-03-01", 50_000),
        ("600002.SH", "sell", "2024-03-01", 12_500)]
    assert os_[0]["price_base"] == pytest.approx(10.2)
    assert os_[0]["basis"] == "vwap" and os_[0]["defer_days"] == 0
    assert os_[0]["fees"] == {"commission": 510.0, "stamp_tax": 0.0, "transfer_fee": 0.0}
    assert os_[1]["defer_days"] == 1 and os_[1]["exec_dt"] == "2024-02-02"
    assert os_[1]["price_base"] == pytest.approx(22.8)                  # 顺延日 VWAP 重定价
    assert os_[1]["fees"] == {"commission": 285.0, "stamp_tax": 0.0, "transfer_fee": 0.0}
    assert os_[2]["price_base"] == pytest.approx(10.4)
    assert os_[2]["fees"] == {"commission": 520.0, "stamp_tax": 0.0, "transfer_fee": 0.0}
    assert os_[3]["price_base"] == pytest.approx(23.0)
    assert os_[3]["fees"] == {"commission": 287.5, "stamp_tax": 0.0, "transfer_fee": 0.0}
    # 停牌标的按 skip 政策放弃目标权重
    assert bt["exec_skipped"] == [{"dt": "2024-01-31", "secid": "600003.SH",
                                   "direction": "buy", "reason": "suspension",
                                   "defer_days": None}]
    # 净值锚点（手工现金对账）
    s = dict(bt["series"])
    assert s["2024-01-30"] == pytest.approx(1.0, abs=1e-9)
    assert s["2024-02-01"] == pytest.approx(0.99949, abs=1e-9)          # 489,490+510,000
    assert s["2024-02-02"] == pytest.approx(0.999205, abs=1e-9)         # 204,205+510,000+285,000
    assert s["2024-03-01"] == pytest.approx(1.0108975, abs=1e-9)        # 期末现金 1,010,897.5
    # 审计与计数
    assert bt["no_lookahead"]["check"] == "pass"
    assert bt["no_lookahead"]["n_orders"] == 4
    assert bt["no_lookahead"]["n_deferred"] == 1 and bt["no_lookahead"]["n_skipped"] == 1
    assert bt["cost"]["n_cancelled"] == 0 and bt["n_rebalances"] == 3
    assert bt["exec_mode"] == "next_vwap" and bt["benchmark"] is None


def test_rebalance_events_and_turnover():
    bt = _run3()
    ev = bt["cost"]["events"]
    assert [e["dt"] for e in ev] == ["2024-01-31", "2024-02-28", "2024-03-01"]
    assert ev[0]["n_orders"] == 2 and ev[0]["n_skipped"] == 1
    assert ev[0]["turnover"] == pytest.approx(0.5)                      # 单边换手 100%/2
    assert ev[1]["n_orders"] == 2 and ev[1]["n_skipped"] == 0
    assert ev[2]["n_orders"] == 0


# ── 集成：成本（DoD b）──────────────────────────────────
def test_cost_boundary_10w_and_dual_track():
    """¥10 万买入：佣金万2.5、卖出印花税、过户费双边；gross−net = Σ费用（零滑点口径）。"""
    bt = _run_one(ct={"slippage_bps": 0.0}, cons={"t_plus_1": False, "lot_size": 100})
    os_ = bt["exec_orders"]
    assert os_[0]["shares"] == 9_800                                    # 98,000/10.2 向下整手
    assert os_[0]["fees"] == {"commission": 24.99, "stamp_tax": 0.0, "transfer_fee": 0.9996}
    assert os_[0]["amount"] == pytest.approx(99_960.0)
    assert os_[1]["shares"] == 9_800
    assert os_[1]["fees"] == {"commission": 25.725, "stamp_tax": 51.45, "transfer_fee": 1.029}
    assert os_[1]["amount"] == pytest.approx(102_900.0)
    assert bt["cost"]["total_fees"] == pytest.approx(104.1936, abs=1e-6)
    assert bt["cost"]["gross_minus_net"] == pytest.approx(0.001041936, abs=1e-9)
    assert bt["cost"]["gross_minus_net"] == pytest.approx(
        bt["cost"]["total_fees"] / 100_000.0, abs=1e-9)                 # 双轨差 = Σ费用/本金
    assert bt["cost"]["net_final"] == pytest.approx(1.028358064, abs=1e-9)
    assert bt["cost"]["gross_final"] == pytest.approx(1.0294, abs=1e-9)
    assert dict(bt["series"])["2024-03-01"] == pytest.approx(1.028358064, abs=1e-9)


def test_zero_cost_dual_track_identity():
    """成本表全零 → 双轨逐位一致（gross_minus_net = 0）。"""
    ct = {"commission_rate": 0.0, "commission_min_cny": 0.0, "stamp_tax_rate": 0.0,
          "transfer_fee_rate": 0.0, "slippage_bps": 0.0}
    bt = _run_one(ct=ct, cons={"t_plus_1": False, "lot_size": 100})
    assert bt["series"] == bt["series_gross"]
    assert bt["cost"]["gross_minus_net"] == 0.0
    assert bt["cost"]["total_fees"] == 0.0
    assert bt["series_gross"][-1][1] == pytest.approx(1.0294, abs=1e-9)  # 102,940/100,000


# ── 集成：滑点单调（DoD c）──────────────────────────────
def test_slippage_monotone():
    """0→2→5→10bps：同一路径下策略终值严格不增。"""
    ct = {"commission_rate": 0.0, "commission_min_cny": 0.0, "stamp_tax_rate": 0.0,
          "transfer_fee_rate": 0.0}
    finals = []
    for bps in (0.0, 2.0, 5.0, 10.0):
        bt = _run_one(w=0.9, ct={**ct, "slippage_bps": bps},
                      cons={"t_plus_1": False, "lot_size": 100})
        finals.append(bt["series"][-1][1])
    assert finals[0] > finals[1] > finals[2] > finals[3]
    assert finals[0] == pytest.approx(1.027, abs=1e-9)                  # 102,700/100,000
    assert finals[3] == pytest.approx(1.025137, abs=1e-9)               # 102,513.7/100,000


# ── 集成：整手（DoD d）──────────────────────────────────
def test_lot_rounding_residual_to_cash():
    """250,000/8 = 31,250 股：lot=100 → 31,200（残余进 cash）；lot=0 → 31,250。"""
    common = dict(w=0.25, ct={"commission_rate": 0.0, "commission_min_cny": 0.0,
                              "stamp_tax_rate": 0.0, "transfer_fee_rate": 0.0,
                              "slippage_bps": 0.0}, capital=1_000_000.0)
    bars = _bars_one(px=8.0)
    bt_lot = _run_one(bars=bars, cons={"t_plus_1": False, "lot_size": 100}, **common)
    bt_free = _run_one(bars=bars, cons={"t_plus_1": False, "lot_size": 0}, **common)
    assert bt_lot["exec_orders"][0]["shares"] == 31_200
    assert bt_lot["exec_orders"][0]["shares"] % 100 == 0
    assert bt_free["exec_orders"][0]["shares"] == 31_250
    # 买入当日按 close=8.0 估值：净值 = 1.0（残余现金 + 持仓市值恰为本金）
    assert dict(bt_lot["series"])["2024-02-01"] == pytest.approx(1.0, abs=1e-9)
    assert dict(bt_free["series"])["2024-02-01"] == pytest.approx(1.0, abs=1e-9)


def test_insufficient_cash_shrink_deterministic():
    """资金不足 → 按整手向下缩减（99,000 本金 → 9,700 股），确定性且不弃单。"""
    bt = _run_one(w=1.0, buy_vwap=10.2,
                  ct={"commission_rate": 0.0, "commission_min_cny": 0.0,
                      "stamp_tax_rate": 0.0, "transfer_fee_rate": 0.0, "slippage_bps": 0.0},
                  cons={"t_plus_1": False, "lot_size": 100}, capital=99_000.0)
    os_ = bt["exec_orders"]
    assert os_[0]["shares"] == 9_700            # 9,900→100,980 / 9,800→99,960 均超 99,000
    assert not any(x["reason"] == "insufficient_cash" for x in bt["exec_skipped"])


# ── 集成：无偷价审计（DoD e）────────────────────────────
def test_no_lookahead_violation_injection(monkeypatch):
    """注入 price=次日 high×1.01 → 审计 FAIL 并列违例样本（不静默放行）。"""
    monkeypatch.setattr(BT, "_fill_price",
                        lambda row: (row["high"] * 1.01, "vwap"))
    bt = _run_one(ct={"slippage_bps": 0.0, "commission_rate": 0.0, "commission_min_cny": 0.0,
                      "stamp_tax_rate": 0.0, "transfer_fee_rate": 0.0},
                  cons={"t_plus_1": False, "lot_size": 100})
    nl = bt["no_lookahead"]
    assert nl["check"] == "fail" and nl["n_violations"] >= 1
    v = nl["violations"][0]
    assert v["price"] == pytest.approx(v["high"] * 1.01)                # price > high
    assert v["price"] > v["high"]


def test_avg4_basis_and_audit_pass():
    """amount 缺失 → avg4 兜底且 basis 标注；avg4 ∈ [low, high] 审计通过。"""
    bars = _bars_one(px=10.0)
    bars["600100.SH"][1] = _bar("2024-02-01", 10.0, 10.4, 9.6, 10.0, vol=10000, amt=0.0)
    bt = _run_one(bars=bars, ct={"slippage_bps": 0.0, "commission_rate": 0.0,
                                 "commission_min_cny": 0.0, "stamp_tax_rate": 0.0,
                                 "transfer_fee_rate": 0.0},
                  cons={"t_plus_1": False, "lot_size": 100})
    o = bt["exec_orders"][0]
    assert o["basis"] == "avg4" and o["price_base"] == pytest.approx(10.0)
    assert bt["no_lookahead"]["check"] == "pass"


def test_t_plus_1_integration_monthly_same_orders():
    """月频下 T+1 不改变既有路径（卖出决策晚于 earliest_sell）——约束不误伤。"""
    cons = {"t_plus_1": True, "lot_size": 100}
    bt_on = _run_one(ct=CT_A, cons=cons, capital=1_000_000.0)
    bt_off = _run_one(ct=CT_A, cons={"t_plus_1": False, "lot_size": 100}, capital=1_000_000.0)
    key = lambda o: (o["secid"], o["direction"], o["exec_dt"], o["shares"])
    assert [key(o) for o in bt_on["exec_orders"]] == [key(o) for o in bt_off["exec_orders"]]
    assert bt_on["no_lookahead"]["check"] == "pass"


def test_benchmark_and_excess_next_vwap():
    """基准买入持有（pct 驱动）与超额收益口径。"""
    bars = _bars_one(px=10.0, buy_vwap=10.2)
    pcts = {"2024-01-31": 0.001, "2024-02-01": 0.002, "2024-02-28": -0.001, "2024-03-01": 0.003}
    bars["510300.SH"] = [{"dt": d, "open": 1, "high": 1, "low": 1, "close": 1,
                          "volume": 1, "amount": 1, "pct": p} for d, p in pcts.items()]
    bt = _run_one(bars=bars, ct=CT_A, cons=CONS_A, capital=1_000_000.0,
                  benchmark_secid="510300.SH")
    assert bt["benchmark"]["name"] == "沪深300ETF"
    prod = 1.0
    for p in pcts.values():
        prod *= (1 + p)
    first = 1 + pcts["2024-01-31"]      # _metrics 口径：total = nav[-1]/nav[0]−1（首日为基期）
    assert bt["benchmark"]["metrics"]["total_return"] == pytest.approx(prod / first - 1,
                                                                       abs=1e-12)
    assert bt["excess"]["total"] == pytest.approx(
        bt["strategy_metrics"]["total_return"] - bt["benchmark"]["metrics"]["total_return"],
        abs=1e-12)
    assert len(bt["benchmark"]["series"]) == len(bt["series"])


# ── 兼容：close 缺省路径（DoD f）────────────────────────
def test_close_mode_default_untouched_and_invalid_mode():
    """缺省 exec_mode="close" 不产出任何撮合新字段；未知 exec_mode 报错。"""
    bt = BT.run_backtest(_bars_one(), _PF1, rebalance="monthly", benchmark_secid="",
                         rule_fn=_rule_one(0.98))
    assert "exec_orders" not in bt and "series_gross" not in bt
    assert "no_lookahead" not in bt and "exec_mode" not in bt
    with pytest.raises(ValueError, match="exec_mode"):
        BT.run_backtest(_bars_one(), _PF1, exec_mode="wizard", rule_fn=_rule_one(0.98))


# ── 集成：factor_backtest 透传 ──────────────────────────
FIDS = ("val_pe_ttm", "mom_20d_fl")

SPEC_BASE = {"id": "fl_match", "name": "低PE Top3",
             "factors": [{"id": "val_pe_ttm", "direction": "asc", "weight": 1.0},
                         {"id": "mom_20d_fl", "direction": "desc", "weight": 1.0}],
             "top_n": 3, "rebalance": "monthly", "benchmark": "510300.SH"}
SPEC_NV = {**SPEC_BASE, "exec": "next_day_vwap",
           "constraints": {"t_plus_1": False, "limit_move": "defer",
                           "suspension": "defer", "lot_size": 100},
           "capital": 1_000_000}


@pytest.fixture
def m2(fl_db, fl_repo):
    db, _ = fl_db
    for fid in FIDS:
        set_status(fl_repo, fid, "active", who="m2-bridge", why="回测对接验收",
                   gate={"ok": True, "reasons": []})
        materialize(load_factor(fl_repo, fid), db, mode="full")
    return db, fl_repo


def _add_intraday_range(repo: Path) -> None:
    """golden 行情为平 OHLC（high==low 一字板）→ 在 tmp 副本上加日内振幅与量额，
    close/pct 不动（选股阶梯与 IC 历史不受影响），使次日 VWAP 可成交。"""
    p = repo / "market" / "daily_bars.json"
    bars = json.loads(p.read_text(encoding="utf-8"))
    for rows in bars.values():
        for r in rows:
            c = r["close"]
            r["open"] = round(c * 0.99, 4)
            r["high"] = round(c * 1.01, 4)
            r["low"] = round(c * 0.98, 4)
            r["volume"], r["amount"] = 10000, c * 10000
    p.write_text(json.dumps(bars, ensure_ascii=False), encoding="utf-8")


def test_factor_backtest_next_vwap_passthrough(m2):
    """spec.exec=next_day_vwap → 撮合新路径激活、审计 pass、双轨与成本表落场。"""
    db, repo = m2
    _add_intraday_range(repo)
    bt = run_factor_backtest(db, repo, SPEC_NV, start="2021-03-01", end="2021-06-30")
    assert bt["exec_mode"] == "next_vwap"
    assert bt["no_lookahead"]["check"] == "pass"
    assert bt["no_lookahead"]["n_orders"] > 0
    assert bt["cost"]["n"] == bt["no_lookahead"]["n_orders"]
    assert bt["cost"]["gross_final"] >= bt["cost"]["net_final"]         # 零成本轨不低于净轨
    assert bt["cost_table"]["commission_rate"] == 0.00025
    assert all(o["basis"] == "vwap" for o in bt["exec_orders"])
    # close 不变 → 选股阶梯不变（与 M2 golden 一致）
    targets = {t["dt"]: sorted(t["secids"]) for t in bt["factor_targets"]}
    for secids in targets.values():
        assert secids == ["600001.SH", "600002.SH", "600003.SH"]


def test_factor_backtest_close_default_and_invalid_exec(m2):
    """缺省 exec=close 保持旧行为（无撮合新字段）；非法 exec 显式报错。"""
    db, repo = m2
    _add_intraday_range(repo)
    bt = run_factor_backtest(db, repo, SPEC_BASE, start="2021-03-01", end="2021-06-30")
    assert "exec_orders" not in bt and "exec_mode" not in bt
    assert "series_gross" not in bt and "no_lookahead" not in bt
    with pytest.raises(FactorBacktestError, match="exec"):
        run_factor_backtest(db, repo, {**SPEC_BASE, "exec": "wizard"},
                            start="2021-03-01", end="2021-06-30")

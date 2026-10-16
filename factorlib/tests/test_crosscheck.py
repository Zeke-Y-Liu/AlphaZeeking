"""M7 三引擎交叉核对测试（实施计划 §10.2 + DoD：年化差异 < 1pct）。

- 零成本一致性：合成库（VWAP = close）下 bt gross vs qlib 位零成本 total 一致
  （整手取整残差 < 0.1pct，其余逐段对齐）；
- 净成本残差：表驱动成本 vs 单边率 → 年化差异 < 1pct；
- proportional_path 语义：monthly 每月等权重置 / target 集合不变纯漂移；
  首点建仓不丢收益（回归锚：calendar 首点无收益键也必须保留）。
"""
import calendar as _cal
from datetime import date

import duckdb

from factorlib.backtest import CostTable, run_backtest
from factorlib.composite import StrategyDSL
from factorlib.crosscheck import crosscheck, qlib_path
from factorlib.evaluate import _EVAL_DDLS
from factorlib.fastscreen import proportional_path
from factorlib.registry import STATUS_DRAFT


def _mk_con(y0, m0, n):
    """合成库：月频 bar（VWAP = close）× 4 标的（F_good=k → 恒 top2 = K4,K3）。"""
    mes = []
    y, m = y0, m0
    for _ in range(n):
        mes.append(date(y, m, _cal.monthrange(y, m)[1]).isoformat())
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
    return StrategyDSL(id="t_xchk", mode="composite", top_n=2,
                       factors=[{"id": "F_good", "weight": "equal"}],
                       methods=["equal"])


def test_qlib_path_zero_cost_matches_backtrader():
    """零成本 + VWAP=close 合成数据：两实现 total_return 残差 < 0.3pct
    （残差来源 = 撮合器整手向下取整的现金闲置，~0.4% 仓位 × 持有期收益）。"""
    con, metas = _mk_con(2021, 1, 12), {"F_good": _Meta()}
    st = _strategy()
    bt = run_backtest(con, st, metas, "2021-01-01", "2021-12-31", cost=CostTable.zero())
    q = qlib_path(con, st, metas, "2021-01-01", "2021-12-31", cost_rate=0.0)
    bt_total = bt["results"]["equal"]["gross"]["total_return"]
    q_total = q["per_method"]["equal"]["total_return"]
    assert abs(bt_total - q_total) < 3e-3
    assert bt["results"]["equal"]["audit"]["pass"] is True


def test_crosscheck_residual_under_threshold():
    """净成本残差 < 1pct（表驱动 vs 单边率 + 滑点口径差，低换手下远小于阈值）。"""
    con, metas = _mk_con(2021, 1, 12), {"F_good": _Meta()}
    rep = crosscheck(con, metas, {"t_xchk": _strategy()}, "2021-01-01", "2021-12-31")
    row = rep["t_xchk"]["methods"]["equal"]
    assert row["diff"] is not None and abs(row["diff"]) < 0.01
    assert row["review_triggered"] is False
    assert rep["t_xchk"]["review_required"] is False


def test_proportional_path_target_pure_drift():
    """target 语义：集合不变 → 首月建仓后纯漂移（turn 仅首月 1.0）。"""
    rets = {"m2": {"A": 0.10, "B": -0.05}, "m3": {"A": 0.05, "B": 0.02}}
    p = proportional_path({"m1": ["A", "B"]}, rets, 0.0,
                          rebalance="target", calendar=["m1", "m2", "m3"])
    eq = dict(p["equity"])
    assert abs(eq["m1"] - 1.0) < 1e-12                     # 首点建仓（nav=1，零成本）
    assert abs(eq["m2"] - 1.025) < 1e-9                    # r = .5×.1 − .5×.05
    # m3：漂移权重 w_A = .5×1.1/1.025, w_B = .5×.95/1.025
    w_a = 0.5 * 1.10 / 1.025
    r3 = w_a * 0.05 + (1 - w_a) * 0.02
    assert abs(eq["m3"] - 1.025 * (1 + r3)) < 1e-9
    assert abs(p["turnover"] - 1.0 / (3 / 12)) < 1e-9      # 仅首月单边 1.0


def test_proportional_path_monthly_reweight():
    """monthly 语义：每月强制重置等权（turn 含月度偏离修正）。"""
    rets = {"m2": {"A": 0.10, "B": -0.05}}
    p = proportional_path({"m1": ["A", "B"], "m2": ["A", "B"]}, rets, 0.0,
                          rebalance="monthly", calendar=["m1", "m2"])
    eq = dict(p["equity"])
    assert abs(eq["m2"] - 1.025) < 1e-9                    # 等权重置不改变当月收益
    # 月度重置换手：(|1/2 − w_A| + |1/2 − w_B|)/2，w_A = .5×1.1/1.025（年化 ×12/2）
    w_a = 0.5 * 1.10 / 1.025
    assert abs(p["turnover"] - (1.0 + abs(0.5 - w_a)) / (2 / 12)) < 1e-9


def test_proportional_path_keeps_calendar_head():
    """回归锚：calendar 首点无收益键（month_returns 首月末无键）也必须保留——
    首点 = 建仓月，丢点会把首月收益并入次月（M7 曾因此与撮合器时间轴错位）。"""
    rets = {"m2": {"A": 0.10, "B": -0.05}}
    p = proportional_path({"m1": ["A", "B"]}, rets, 0.0,
                          rebalance="target", calendar=["m1", "m2"])
    assert [m for m, _ in p["equity"]] == ["m1", "m2"]

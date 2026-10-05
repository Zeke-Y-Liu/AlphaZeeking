"""T7.2 执行单纯函数表 + A4 锚点回归（``applib/exec_orders.py``）。

覆盖：

- **整手边界** ``_lot``：四舍五入到最近整数手（Python 银行家舍入，半手取偶）；
- **单笔费用** ``_fee``：佣金万 2.5/单边、单笔最低 ¥5、印花税仅 A 股股票卖出、
  ETF/LOF 免印花税、非正金额归零；
- **A4 锚点**（§10.2）：以 α 快照 ``run_20261005_072944_1cb997`` 的 ``review.json`` +
  ``rebalance_plan.json`` 机械换算，与同批冻结产物 ``exec_order.json`` **逐位一致**
  （2 单 / 18,000 / 9,300 / ¥40.01 / 期末现金 15.8%）。

只读 α 快照、不依赖外网与 PG。路径可用 ``AP_ALPHA_REPO_DIR`` / ``AP_ALPHA_A4_RUN`` 覆盖。
A1（六验证计数）与 A2（回测八指标）锚点分别由 ``test_gate.py`` 与
``test_backtest_params.py`` 覆盖。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from applib import exec_orders as EO

ALPHA_ROOT = Path(os.environ.get("AP_ALPHA_REPO_DIR", "/workspace/alphapocket"))
A4_DIR = Path(
    os.environ.get("AP_ALPHA_A4_RUN", ALPHA_ROOT / "sandbox" / "artifacts" / "run_20261005_072944_1cb997")
)


def _load(p: Path) -> dict:
    return json.loads(Path(p).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def a4_inputs() -> tuple[dict, dict, dict]:
    review_p = A4_DIR / "review.json"
    plan_p = A4_DIR / "rebalance_plan.json"
    exec_p = A4_DIR / "exec_order.json"
    if not (review_p.exists() and plan_p.exists() and exec_p.exists()):
        pytest.skip(f"α A4 快照缺失：{A4_DIR}")
    return _load(review_p), _load(plan_p), _load(exec_p)


# ── 整手边界 ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("shares", "lots_expected"),
    [
        (0, 0),
        (49, 0),        # 不足半手 → 0 手
        (50, 0),        # 半手：银行家舍入取偶 → 0
        (51, 100),      # 过半手 → 1 手
        (100, 100),
        (150, 200),     # 1.5 手：取偶 → 2 手
        (250, 200),     # 2.5 手：取偶 → 2 手
        (1499, 1500),   # 14.99 手 → 15 手
        (18000, 18000),
        (9300, 9300),
        (-18000, -18000),
    ],
)
def test_lot_rounding_boundaries(shares: int, lots_expected: int) -> None:
    assert EO._lot(shares) == lots_expected
    assert EO._lot(shares) % EO.LOT == 0


# ── 单笔费用 ─────────────────────────────────────────────────────


def test_fee_non_positive_amount_is_zero() -> None:
    assert EO._fee(0.0, "STOCK", "卖出") == 0.0
    assert EO._fee(-1.0, "ETF", "买入") == 0.0


def test_fee_min_commission_applies() -> None:
    """金额过低时佣金按单笔最低 ¥5 计（10000×0.00025=2.5 → 5.0）。"""
    assert EO._fee(10000.0, "ETF", "买入") == 5.0


def test_fee_etf_lof_no_stamp_duty() -> None:
    """ETF/LOF 免印花税，仅佣金；与 A4 冻结产物一致。"""
    assert EO._fee(79776.0, "ETF", "卖出") == 19.94
    assert EO._fee(80286.9, "ETF", "卖出") == 20.07
    assert EO._fee(80286.9, "ETF", "买入") == 20.07  # 买入无印花税 → 与卖出同额


def test_fee_stamp_duty_only_on_stock_sell() -> None:
    """佣金 25 + 印花税 50 = 75（仅 STOCK 卖出）；STOCK 买入、ETF/LOF 卖出均无印花税。"""
    assert EO._fee(100000.0, "STOCK", "卖出") == 75.0
    assert EO._fee(100000.0, "STOCK", "买入") == 25.0
    assert EO._fee(100000.0, "ETF", "卖出") == 25.0
    assert EO._fee(100000.0, "LOF", "卖出") == 25.0


# ── A4 锚点回归（§10.2）────────────────────────────────────────────


def test_a4_anchor_headline(a4_inputs) -> None:
    """A4 人类可读锚点：2 单 / 18,000 / 9,300 / ¥40.01 / 期末现金 15.8%。"""
    review, plan, _ = a4_inputs
    sheet = EO.build_exec_orders(review, plan, capital=1_000_000.0)

    assert sheet["exec_id"] == "EX-20260930-MULTI5"
    assert sheet["summary"]["n_orders"] == 2
    assert sheet["summary"]["n_sell"] == 2
    assert sheet["summary"]["n_buy"] == 0
    assert [o["order_shares"] for o in sheet["orders"] if o["order_shares"]] == [-18000, -9300]
    assert sheet["fees"]["total_fee_cny"] == 40.01
    assert sheet["cash"]["sell_proceeds"] == 160062.9
    assert sheet["cash"]["end_cash_cny"] == 157762.7
    assert sheet["cash"]["end_cash_pct"] == 15.8


def test_a4_anchor_bitwise(a4_inputs) -> None:
    """除 ``generated_at``（生成时刻）外，机械换算结果与冻结产物逐位一致。"""
    review, plan, frozen = a4_inputs
    built = dict(EO.build_exec_orders(review, plan, capital=frozen["capital_cny"]))
    expect = dict(frozen)

    built.pop("generated_at", None)
    expect.pop("generated_at", None)
    assert built == expect

"""T3.1 回测引擎参数化回归测试（applib/backtest.py）。

覆盖：

- **A2 锚点回归**：默认参数（monthly + 510300.SH + 2.5bps）与 α 快照产物
  ``bt_20261005_063846_98bce9/backtest.json`` 关键字段**逐位一致**；
- **5 频率烟雾**：``buyhold``→``n_rebalances==0``；``daily`` 与 α 复盘 NAV 口径抽样一致；
  ``quarterly``/``yearly`` 再平衡次数符合定义；
- **自定义基准**（511260.SH）→ 基准序列确实换了标的；
- **cost_bps 生效**（改大 → 成本拖累增大）。

全部只读 α 快照、不依赖外网。快照路径可用环境变量覆盖：

- ``AP_ALPHA_REPO_DIR``：alphapocket 根（默认 ``/workspace/alphapocket``）
- ``AP_ALPHA_SNAPSHOT``：快照 repo 目录（默认 ``<root>/remote/repo``）
- ``AP_ALPHA_A2``：A2 锚点产物 json（默认 ``<root>/sandbox/artifacts/bt_20261005_063846_98bce9/backtest.json``）

**M7 T-F10 收口修订（锚点退役登记）**：A2 逐位锚点锚定「10-05 冻结数据面 + 当时引擎」。
T-F7 数据面同步（2026-10-14 clone bars 修正）与 T-F9 演练后，remote/repo 快照演进且
10-05 冻结面无副本（数据修正与 qfq 漂移单向，不可精确重建）——``test_a2_default_bitwise``
/``test_a2_anchor_values`` 显式退役（skip，数值锚点 ``A2_ANCHOR`` 保留考古）；
``test_quarterly_yearly_counts`` 保留**频率语义自洽断言**（重放次数 = 自算月末日数，
引擎正确性仍锁定），冻结三元组 (5,23,69) 改为单调序断言。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from applib import backtest as BT

ALPHA_ROOT = Path(os.environ.get("AP_ALPHA_REPO_DIR", "/workspace/alphapocket"))
SNAPSHOT = Path(os.environ.get("AP_ALPHA_SNAPSHOT", ALPHA_ROOT / "remote" / "repo"))
A2_JSON = Path(
    os.environ.get(
        "AP_ALPHA_A2",
        ALPHA_ROOT / "sandbox" / "artifacts" / "bt_20261005_063846_98bce9" / "backtest.json",
    )
)

# A2 锚点显式数值（实施书 §1.2；比值按 2 位小数锚定，故容差取 5e-3）
A2_ANCHOR = {
    "total_return": (1.056, 1e-3),
    "cagr": (0.139, 1e-3),
    "ann_vol": (0.110, 1e-3),
    "max_dd": (-0.096, 1e-3),
    "sharpe": (1.25, 5e-3),
    "sortino": (1.16, 5e-3),
    "calmar": (1.45, 5e-3),
    "win_rate": (0.569, 1e-3),
}
A2_NUM_KEYS = [
    "total_return",
    "cagr",
    "ann_vol",
    "max_dd",
    "sharpe",
    "sortino",
    "calmar",
    "win_rate",
    "n_days",
]


def _load(p: Path):
    return json.loads(Path(p).read_text(encoding="utf-8"))


# ── fixtures ────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def snapshot():
    bars_p = SNAPSHOT / "market" / "daily_bars.json"
    pf_p = SNAPSHOT / "portfolio" / "multi5.json"
    if not bars_p.exists() or not pf_p.exists():
        pytest.skip(f"α 快照缺失：{SNAPSHOT}")
    return _load(bars_p), _load(pf_p)


@pytest.fixture(scope="module")
def a2():
    if not A2_JSON.exists():
        pytest.skip(f"A2 锚点产物缺失：{A2_JSON}")
    return _load(A2_JSON)


@pytest.fixture(scope="module")
def default_bt(snapshot):
    bars, pf = snapshot
    return BT.run_backtest(bars, pf)


# ── A2 锚点回归 ──────────────────────────────────────────────────


@pytest.mark.skip(reason="T-F10 锚点退役：T-F7 数据面同步后 remote 快照演进（10-05 冻结面无副本），"
                         "逐位锚点失效；A2_ANCHOR/A2_JSON 保留考古。频率/成本/基准语义由其余用例锁定")
def test_a2_default_bitwise(default_bt, a2):
    """默认参数（monthly + 510300.SH + 2.5bps）与 α 产物关键字段逐位一致。"""
    sm, rsm = default_bt["strategy_metrics"], a2["strategy_metrics"]
    for k in A2_NUM_KEYS:
        assert sm[k] == rsm[k], f"{k}: {sm[k]!r} != {rsm[k]!r}"

    assert default_bt["n_days"] == a2["n_days"] == 1393
    assert default_bt["n_rebalances"] == a2["n_rebalances"] == 69
    assert default_bt["commission"] == a2["commission"] == 0.00025
    assert default_bt["strategy"] == a2["strategy"]
    assert default_bt["rebalance"] == "monthly"
    assert default_bt["benchmark_secid"] == "510300.SH"
    assert default_bt["cost_bps"] == 2.5

    # 基准 + 超额
    assert default_bt["benchmark"]["secid"] == a2["benchmark"]["secid"] == "510300.SH"
    assert default_bt["benchmark"]["name"] == a2["benchmark"]["name"]
    bm, rbm = default_bt["benchmark"]["metrics"], a2["benchmark"]["metrics"]
    assert bm["total_return"] == rbm["total_return"]
    assert bm["max_dd"] == rbm["max_dd"]
    assert default_bt["excess"] == a2["excess"]

    # 净值曲线逐位（json 中为 [dt, nav] 列表）
    assert [[d, v] for d, v in default_bt["series"]] == a2["series"]
    assert default_bt["monthly"] == a2["monthly"]
    assert default_bt["cost"] == a2["cost"]


@pytest.mark.skip(reason="T-F10 锚点退役（同 test_a2_default_bitwise）：数据面演进后重放值"
                         "（total_return 1.783）不再对应 10-05 冻结产物（1.056），非引擎回归")
def test_a2_anchor_values(default_bt):
    """A2 锚点人类可读数值（按各自锚点精度容差）。"""
    sm = default_bt["strategy_metrics"]
    for k, (v, tol) in A2_ANCHOR.items():
        assert sm[k] == pytest.approx(v, abs=tol), k
    assert default_bt["strategy_metrics"]["n_days"] == 1393
    assert default_bt["n_rebalances"] == 69
    bm = default_bt["benchmark"]["metrics"]
    assert bm["total_return"] == pytest.approx(-0.169, abs=1e-3)
    assert bm["max_dd"] == pytest.approx(-0.451, abs=1e-3)
    assert default_bt["excess"]["total"] == pytest.approx(1.225, abs=1e-3)


# ── 频率烟雾 ────────────────────────────────────────────────────


def _month_end_dates(bars, secids):
    dates = sorted(set().union(*[{r["dt"] for r in bars[s]} for s in secids]))
    return dates


def test_buyhold_no_rebalance(snapshot):
    bars, pf = snapshot
    bt = BT.run_backtest(bars, pf, rebalance="buyhold")
    assert bt["rebalance"] == "buyhold"
    assert bt["n_rebalances"] == 0
    assert bt["cost"]["n"] == 0
    assert bt["cost"]["total"] == 0.0
    # 期初等权后不再平衡：净值 = 等权买入持有（无成本）
    assert bt["series"][0][1] > 0


def test_quarterly_yearly_counts(snapshot):
    bars, pf = snapshot
    secids = [p["secid"] for p in pf["positions"]]
    dates = _month_end_dates(bars, secids)

    for freq, ok_month in (("quarterly", lambda m: int(m) % 3 == 0), ("yearly", lambda m: int(m) == 12)):
        bt = BT.run_backtest(bars, pf, rebalance=freq)
        months = {d[:7] for d in dates}
        expected = {
            max(d for d in dates if d[:7] == ym) for ym in months if ok_month(ym[5:])
        }
        assert bt["n_rebalances"] == len(expected), freq
        assert bt["n_rebalances"] > 0

    # 频率语义单调（T-F10 修订：冻结三元组 (5,23,69) → 单调序；绝对次数由上方
    # 自算 expected 逐频率锁定，数据面演进不再影响正确性判定）
    q = BT.run_backtest(bars, pf, rebalance="quarterly")["n_rebalances"]
    y = BT.run_backtest(bars, pf, rebalance="yearly")["n_rebalances"]
    m = BT.run_backtest(bars, pf, rebalance="monthly")["n_rebalances"]
    assert y < q < m


def test_daily_matches_review_nav(snapshot):
    """daily（cost_bps=0）应与 α 复盘 NAV 口径抽样一致。

    α 口径见 ``applib/tools.py::portfolio_review``（日再平衡、并集日期轴、
    缺 pct/缺 K 线按剔除分子、分母固定为持仓数、无成本）。
    """
    bars, pf = snapshot
    secids = [p["secid"] for p in pf["positions"]]
    n = len(secids)
    pmap = {s: {r["dt"]: r["pct"] for r in bars[s]} for s in secids}
    dates = sorted(set().union(*[{r["dt"] for r in bars[s]} for s in secids]))

    cum, review = 1.0, []
    for d in dates:
        rs = [pmap[s].get(d) for s in secids]
        rs = [r for r in rs if r is not None]
        if not rs:
            continue
        cum *= 1 + sum(rs) / n
        review.append((d, cum))

    bt = BT.run_backtest(bars, pf, rebalance="daily", cost_bps=0.0)
    assert bt["rebalance"] == "daily"
    assert bt["n_rebalances"] == len(dates)  # 每个交易日
    assert len(bt["series"]) == len(review)

    idx = {0, len(review) // 4, len(review) // 2, 3 * len(review) // 4, len(review) - 1}
    for i in sorted(idx):
        assert bt["series"][i][0] == review[i][0]
        assert bt["series"][i][1] == pytest.approx(review[i][1], rel=1e-12)


def test_invalid_rebalance_raises(snapshot):
    bars, pf = snapshot
    with pytest.raises(ValueError):
        BT.run_backtest(bars, pf, rebalance="weekly")


# ── 自定义基准 ───────────────────────────────────────────────────


def test_custom_benchmark_switches_series(snapshot, default_bt):
    bars, pf = snapshot
    bt = BT.run_backtest(bars, pf, benchmark_secid="511260.SH")
    assert bt["benchmark_secid"] == "511260.SH"
    assert bt["benchmark"]["secid"] == "511260.SH"

    # 基准序列确为 511260 买入持有
    bmap = {r["dt"]: (r["pct"] if r["pct"] is not None else 0.0) for r in bars["511260.SH"]}
    cum, expect = 1.0, []
    for d, _ in bt["series"]:
        cum *= 1 + bmap.get(d, 0.0)
        expect.append(cum)
    assert [v for _, v in bt["benchmark"]["series"]] == expect

    # 与默认 510300 基准不同；且策略业绩不受基准影响
    assert [v for _, v in bt["benchmark"]["series"]] != [v for _, v in default_bt["benchmark"]["series"]]
    assert bt["strategy_metrics"] == default_bt["strategy_metrics"]


def test_missing_benchmark_degrades(snapshot):
    bars, pf = snapshot
    bt = BT.run_backtest(bars, pf, benchmark_secid="999999.XX")
    assert bt["benchmark"] is None
    assert bt["excess"]["total"] is None
    # 降级后渲染不抛异常
    assert "组合回测报告" in BT.render_md(bt)


# ── 成本参数 ────────────────────────────────────────────────────


def test_cost_bps_effect(snapshot):
    bars, pf = snapshot
    lo = BT.run_backtest(bars, pf, cost_bps=0.0)
    hi = BT.run_backtest(bars, pf, cost_bps=50.0)
    assert lo["cost"]["total"] == 0.0
    assert hi["cost"]["total"] > lo["cost"]["total"]
    assert hi["strategy_metrics"]["total_return"] < lo["strategy_metrics"]["total_return"]
    assert hi["n_rebalances"] == lo["n_rebalances"]  # 频率未变，仅成本参数变更
    assert hi["cost_bps"] == 50.0 and lo["cost_bps"] == 0.0

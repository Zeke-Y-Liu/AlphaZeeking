"""M2 回测执行层 × factorlib 对接测试：动态规则回测 + 因子截面选股 + 无抢跑审计。

手工推演（golden fixture，期望值独立于被测代码）：
- 价格阶梯 close_i = (9+i) + 0.5×已过检查点数；val_pe_ttm = close/TTM。
- 2021-03-01..06-30 月末截面：早/晚公告组 TTM 阶梯使 PE 沿 i 单调递增
  （05-28 晚组 Q1 公告后 S07 亏损缺席）→ asc Top-3 恒为 600001-3，
  desc Top-3 恒为 600008-10；全程持仓不变 → 动态回测与静态 3 股回测逐位一致。
运行：``cd alphapocket && python -m pytest tests/test_factor_backtest.py -q``
"""
from __future__ import annotations

import duckdb
import pytest

from applib import backtest as BT
from applib.factor_backtest import (
    FactorBacktestError, composite_scores, load_section, run_factor_backtest, select_top,
)
from factorlib import load_factor, set_status
from factorlib.materialize import materialize

FIDS = ("val_pe_ttm", "mom_20d_fl")


@pytest.fixture
def m2(fl_db, fl_repo):
    """物化估值+动量两因子（active），返回 (db, repo)。"""
    db, _ = fl_db
    for fid in FIDS:
        set_status(fl_repo, fid, "active", who="m2-bridge", why="回测对接验收",
                   gate={"ok": True, "reasons": []})   # M5：测试夹具显式门禁结论
        materialize(load_factor(fl_repo, fid), db, mode="full")
    return db, fl_repo


# ── 合成打分与选股单元 ──────────────────────────────────
def test_rank_pct_semantics():
    vals = {"A": 10.0, "B": 20.0, "C": 20.0, "D": 30.0}
    # desc 分数 = rank_pct 本身（大值高分）；并列取中位百分位（pandas rank(pct=True) 口径）
    r = composite_scores({"f1": vals}, [{"id": "f1", "direction": "desc"}])
    assert r == {"A": 0.0, "B": 0.375, "C": 0.375, "D": 0.75}
    r_asc = composite_scores({"f1": vals}, [{"id": "f1", "direction": "asc"}])
    assert r_asc == {"A": 1.0, "B": 0.625, "C": 0.625, "D": 0.25}   # asc = 1 − rank


def test_composite_missing_and_weights():
    # f1 缺 C → C 只按 f2 计分（权重按可用因子归一，非 0 分惩罚）
    sec = {"f1": {"A": 1.0, "B": 2.0, "D": 3.0}, "f2": {"A": 5.0, "B": 4.0, "C": 3.0}}
    # f1={A:1,B:2,D:3} desc → rank 0/⅓/⅔；f2={A:5,B:4,C:3} asc → 1−rank = ⅓/⅔/1
    r = composite_scores(sec, [{"id": "f1", "direction": "desc", "weight": 3.0},
                               {"id": "f2", "direction": "asc", "weight": 1.0}])
    assert r["C"] == pytest.approx(1.0)          # f2 最小值（asc 满分）
    assert r["D"] == pytest.approx(2 / 3)        # 仅 f1 可用 → 权重归一后 = f1 原始 rank
    assert r["A"] == pytest.approx(0.75 * 0.0 + 0.25 * (1 - 2 / 3))
    assert r["B"] == pytest.approx(0.75 * (1 / 3) + 0.25 * (1 - 1 / 3))
    assert set(r) == {"A", "B", "C", "D"}


def test_select_top_tiebreak_deterministic():
    assert select_top({"B": 0.5, "A": 0.5, "C": 0.9}, 2) == ["C", "A"]   # 并列按 secid 序


def test_load_section_strict_dt(m2):
    db, _ = m2
    con = duckdb.connect(str(db), read_only=True)
    try:
        sec = load_section(con, "val_pe_ttm", "2021-03-31")
        assert len(sec) == 10
        for i in range(1, 11):                    # close=10+i，TTM=1.0 → PE=10+i
            assert sec[f"6000{i:02d}.SH"] == pytest.approx(10 + i)
    finally:
        con.close()


# ── 端到端：因子驱动动态回测 ────────────────────────────
SPEC = {"id": "fl_pe_asc_top3", "name": "低PE Top3",
        "factors": [{"id": "val_pe_ttm", "direction": "asc", "weight": 1.0},
                    {"id": "mom_20d_fl", "direction": "desc", "weight": 1.0}],
        "top_n": 3, "rebalance": "monthly", "benchmark": "510300.SH"}


def test_factor_backtest_golden_holdings(m2):
    """月末截面手工推演：asc 合成 Top-3 全程 = 600001-3（06 期 S07 亏损缺席不入选）。"""
    db, repo = m2
    bt = run_factor_backtest(db, repo, SPEC, start="2021-03-01", end="2021-06-30")
    targets = {t["dt"]: sorted(t["secids"]) for t in bt["factor_targets"]}
    assert sorted(targets) == ["2021-03-31", "2021-04-30", "2021-05-31", "2021-06-30"]
    for d, secids in targets.items():
        assert secids == ["600001.SH", "600002.SH", "600003.SH"], d
    # 覆盖率口径 = 「任一因子可打分的标的」：S07 虽 05-28 后 PE 缺席（M1 e2e 已证），
    # 但量价因子 mom 仍有值 → 合成面仍 10/10；其低分使其不入 Top-3
    cov = {c["dt"]: c for c in bt["factor_coverage"]}
    assert all(c["n_scored"] == 10 and c["coverage"] == 1.0 for c in cov.values())


def test_factor_backtest_matches_static_backtest_bitwise(m2):
    """持仓不变 → 动态规则回测与静态 3 股等权回测逐位一致（双路径交叉验证）。"""
    db, repo = m2
    dyn = run_factor_backtest(db, repo, SPEC, start="2021-03-01", end="2021-06-30")
    bars = __import__("json").loads(
        (repo / "market" / "daily_bars.json").read_text(encoding="utf-8"))
    static_pf = {"id": "static3", "name": "静态对照",
                 "positions": [{"secid": s, "name": s} for s in
                               ("600001.SH", "600002.SH", "600003.SH")]}
    sta = BT.run_backtest(bars, static_pf, start="2021-03-01", end="2021-06-30",
                          rebalance="monthly", benchmark_secid="510300.SH")
    assert dyn["strategy_metrics"] == sta["strategy_metrics"]
    assert dyn["series"] == sta["series"]
    assert dyn["cost"]["total"] == sta["cost"]["total"]
    # per-asset 权重：全程持仓 → 1/3
    assert all(p["weight"] == pytest.approx(1 / 3) for p in dyn["per_asset"])


def test_factor_backtest_pit_audit_pass_and_exempt(m2):
    """无抢跑审计：财务因子逐笔核对通过；量价因子（mom）无 pit 行 → 豁免计数。"""
    db, repo = m2
    bt = run_factor_backtest(db, repo, SPEC, start="2021-03-01", end="2021-06-30")
    a = bt["pit_audit"]
    assert a["check"] == "pass" and a["n_violations"] == 0
    assert a["n_checked"] == 12        # 4 期 × 3 标的 × val_pe_ttm
    assert a["n_exempt"] == 12         # 4 期 × 3 标的 × mom_20d_fl（量价无 PIT 元数据）


def test_factor_backtest_desc_direction(m2):
    db, repo = m2
    spec = dict(SPEC, id="fl_pe_desc_top3",
                factors=[{"id": "val_pe_ttm", "direction": "desc"}])
    bt = run_factor_backtest(db, repo, spec, start="2021-03-01", end="2021-03-31")
    assert sorted(bt["factor_targets"][0]["secids"]) == \
        ["600008.SH", "600009.SH", "600010.SH"]      # 高 PE 优 → i 最大三只


def test_pit_audit_catches_tamper(m2):
    """篡改注入：公告日晚于取值日 → 审计 FAIL 且样本可定位（无静默放行）。"""
    db, repo = m2
    con = duckdb.connect(str(db))
    try:
        con.execute("""UPDATE fl_factor_pit_meta SET used_announce = used_announce + INTERVAL 1 DAY
                       WHERE factor_id = 'val_pe_ttm' AND secid = '600001.SH'
                         AND dt >= DATE '2021-03-31'""")
    finally:
        con.close()
    bt = run_factor_backtest(db, repo, SPEC, start="2021-03-01", end="2021-03-31")
    a = bt["pit_audit"]
    assert a["check"] == "fail" and a["n_violations"] == 1   # 单期 × 单标的 × 财务因子
    assert {v["secid"] for v in a["violations"]} == {"600001.SH"}
    assert all(v["used_announce"] > v["dt"] for v in a["violations"])


# ── run_backtest 动态规则内核 ───────────────────────────
def _flat_bars() -> dict:
    """两只标的、日收益恒定的合成行情（2021-01..03）。"""
    from datetime import date, timedelta
    bars = {}
    for secid, base in (("600001.SH", 0.01), ("600002.SH", 0.02)):
        d, arr = date(2021, 1, 4), []
        close = 10.0
        while d <= date(2021, 3, 31):
            if d.weekday() < 5:
                close *= 1 + base
                arr.append({"dt": d.isoformat(), "close": round(close, 4), "pct": base})
            d += timedelta(days=1)
        bars[secid] = arr
    return bars


def test_run_backtest_rule_fn_dynamic_turnover():
    """动态规则：期初取首个未来目标；目标切换 → 换手成本 + rule_targets 全记录 +
    per-asset 权重 = 持仓频率均值（600001: 3/4，600002: 1/4）。"""
    bars = _flat_bars()
    targets = {"2021-01-29": {"600001.SH": 1.0}, "2021-03-31": {"600002.SH": 1.0}}

    def rule_fn(d):
        if d in targets:
            return targets[d]
        prior = [k for k in targets if k <= d]
        if prior:
            return targets[max(prior)]
        return targets[min(targets)]

    pf = {"id": "dyn", "name": "动态", "positions": [{"secid": s, "name": s}
                                                     for s in ("600001.SH", "600002.SH")]}
    bt = BT.run_backtest(bars, pf, rebalance="monthly", benchmark_secid=None, rule_fn=rule_fn)
    assert [t["dt"] for t in bt["rule_targets"]] == \
        ["2021-01-04", "2021-01-29", "2021-02-26", "2021-03-31"]   # 首日 + 3 个月末
    assert bt["rule_targets"][0]["weights"] == {"600001.SH": 1.0}  # 期初建仓=首个未来目标
    assert bt["cost"]["n"] == 3 and bt["cost"]["total"] > 0        # 目标切换 → 换手成本
    w = {p["secid"]: p["weight"] for p in bt["per_asset"]}
    assert w["600001.SH"] == pytest.approx(0.75) and w["600002.SH"] == pytest.approx(0.25)


def test_run_backtest_rule_fn_rejects_ab_gate():
    bars = _flat_bars()
    pf = {"id": "x", "name": "x", "positions": [{"secid": "600001.SH", "name": "x"}]}
    with pytest.raises(ValueError, match="互斥"):
        BT.run_backtest(bars, pf, rule_fn=lambda d: {"600001.SH": 1.0}, ab_gate=True)


# ── spec 校验 ───────────────────────────────────────────
def test_spec_validation(m2):
    db, repo = m2
    with pytest.raises(FactorBacktestError, match="factors"):
        run_factor_backtest(db, repo, {"factors": []})
    with pytest.raises(FactorBacktestError, match="direction"):
        run_factor_backtest(db, repo, {"factors": [{"id": "val_pe_ttm", "direction": "up"}]})
    with pytest.raises(FactorBacktestError, match="未物化"):
        run_factor_backtest(db, repo, {"factors": [{"id": "nope_fl"}]})

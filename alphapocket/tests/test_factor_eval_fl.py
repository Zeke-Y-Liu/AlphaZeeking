"""M3 单因子检验流水线测试：IC/分层/NW-t 全部对独立手工计算（不使用被测代码生成期望值）。

golden 推演（价格阶梯 close_i = (9+i)+0.5×已过检查点数，2021-03-01..06-30）：
- 03-31 截面：PE_i = 10+i（全体 TTM=1.0）；→04-30 收益 = 1/(10+i)（CP3+CP4 合计跳 +1.0）
  → PE 递增 vs 收益递减 → IC = −1.0（asc 因子定向 IC = +1）
- 04-30→05-31、05-31→06-30：平台期收益全 0 → 零方差截面剔除（透明计数）
- 分层 n=10,k=10：每层 1 只，L1=600001（低值）收益 1/11 … L10 收益 1/20 → 单调性 −1
运行：``cd alphapocket && python -m pytest tests/test_factor_eval_fl.py -q``
"""
from __future__ import annotations

import duckdb
import pytest

from applib.factor_eval_fl import nw_t_stat, render_md, run_factor_eval, save_eval_report
from applib.walkforward import _spearman
from factorlib import load_factor, set_status
from factorlib.materialize import materialize

FID = "val_pe_ttm"


@pytest.fixture
def m3(fl_db, fl_repo):
    """物化估值因子 + 向 repo 副本追加合成基准 510300.SH（close 恒 100 → 基准收益 0）。"""
    db, _ = fl_db
    set_status(fl_repo, FID, "active", who="m3-eval", why="检验流水线验收",
               gate={"ok": True, "reasons": []})       # M5：测试夹具显式门禁结论
    materialize(load_factor(fl_repo, FID), db, mode="full")
    bars_path = fl_repo / "market" / "daily_bars.json"
    bars = __import__("json").loads(bars_path.read_text(encoding="utf-8"))
    days = sorted({r["dt"] for arr in bars.values() for r in arr})
    bars["510300.SH"] = [{"dt": d, "close": 100.0, "pct": 0.0} for d in days]
    bars_path.write_text(__import__("json").dumps(bars, ensure_ascii=False), encoding="utf-8")
    return db, fl_repo


# ── 统计量单元（独立手工对账）────────────────────────────
def test_spearman_known_correlations():
    """协议 DoD：已知相关合成数据 IC 与手工一致（3 组）。"""
    assert _spearman([1, 2, 3, 4, 5], [10, 20, 30, 40, 50]) == pytest.approx(1.0)
    assert _spearman([1, 2, 3, 4, 5], [50, 40, 30, 20, 10]) == pytest.approx(-1.0)
    # 并列 mid-rank：rank_x = [1, 2.5, 2.5, 4] → ρ = 4.5/√(4.5×5) = 0.94868
    assert _spearman([1, 2, 2, 3], [1, 2, 3, 4]) == pytest.approx(0.948683, rel=1e-5)


def test_nw_t_stat_hand_computed():
    """Newey-West t 对手工值：x=[1..5]，n=5 → lag=⌊4·0.05^(2/9)⌋=2，
    s0 = 2 + 2·(2/3)·0.8 + 2·(1/3)·(−0.2) = 2.9333 → t = 3/√(2.9333/5) = 3.91675；
    普通 t = 3/(s=1.5811/√5) = 4.2426。HAC 修正后 |t| 收缩。"""
    x = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert nw_t_stat(x) == pytest.approx(3.916747, rel=1e-5)
    t_plain = 3.0 / ((2.5 ** 0.5) / 5 ** 0.5)
    assert t_plain == pytest.approx(4.242641, rel=1e-5)
    assert abs(nw_t_stat(x)) < abs(t_plain)                 # 自相关 → NW 收缩
    assert nw_t_stat([3.0]) is None                          # n<2 不可估


# ── golden 截面端到端 ───────────────────────────────────
def test_eval_ic_and_degenerate_periods(m3):
    """h=1M：唯一有效截面 03-31（IC=−1，定向 +1）；平台期零方差剔除透明计数。"""
    db, repo = m3
    rep = run_factor_eval(db, repo, FID, "asc", horizons=(1, 3),
                          n_groups=10, start="2021-03-01", end="2021-06-30",
                          as_of="2026-10-10", code_version="m3-test")
    r1 = rep["reports"][1]
    assert r1["n_periods"] == 1
    assert r1["ic"]["series"] == [("2021-03-31", -1.0)]
    assert r1["ic"]["directed_mean"] == pytest.approx(1.0)   # asc：低 PE 优 → 正向
    assert r1["ic"]["win_rate"] == 0.0                        # 原始 IC=−1 < 0
    assert r1["ic"]["nw_t"] is None and r1["ic"]["t_stat"] is None   # 单期不可估
    assert r1["n_dropped_degenerate"] == 2                    # 04-30→05-31、05-31→06-30 平台
    assert r1["n_dropped_gap"] == 0
    r3 = rep["reports"][3]
    assert r3["n_periods"] == 1 and r3["ic"]["series"] == [("2021-03-31", -1.0)]


def test_eval_layers_monotonicity_and_spread(m3):
    """分层手工值：L_k 收益 = 1/(10+k)；单调性 −1；定向多空 = 1/11 − 1/20；基准 0 → 超额 = 1/11。"""
    db, repo = m3
    rep = run_factor_eval(db, repo, FID, "asc", horizons=(1,), n_groups=10,
                          start="2021-03-01", end="2021-06-30")
    r = rep["reports"][1]
    assert r["n_groups_eff"] == 10
    for k in range(1, 11):
        assert r["layer_mean"][k] == pytest.approx(1.0 / (10 + k), rel=1e-5)
    assert r["monotonicity"] == pytest.approx(-1.0)
    assert r["directed_spread"] == pytest.approx(1 / 11 - 1 / 20, rel=1e-5)
    assert r["top_mean"] == pytest.approx(1 / 11, rel=1e-5)
    assert r["top_excess_per_period"] == pytest.approx(1 / 11, rel=1e-5)  # 基准收益 0
    assert r["top_turnover"] is None                                       # 单期无相邻期
    # 覆盖度：03-31/04-30 期 10/11（universe 含基准）；05-31 期 S07 亏损 PE 缺 → 9/11
    assert r["coverage_mean"] == pytest.approx((10 / 11 + 10 / 11 + 9 / 11) / 3, rel=1e-4)


def test_eval_desc_direction_semantics(m3):
    """desc：多头组 = 高值层 L10；定向 IC = 原始 IC；定向多空 = L10 − L1（负值）。"""
    db, repo = m3
    rep = run_factor_eval(db, repo, FID, "desc", horizons=(1,), n_groups=10,
                          start="2021-03-01", end="2021-06-30")
    r = rep["reports"][1]
    assert r["top_mean"] == pytest.approx(1 / 20, rel=1e-5)               # L10 = 600010
    assert r["directed_spread"] == pytest.approx(1 / 20 - 1 / 11, rel=1e-5)
    assert r["ic"]["directed_mean"] == pytest.approx(-1.0)                 # desc 不翻转


def test_eval_validation_and_unmaterialized(m3):
    db, repo = m3
    with pytest.raises(Exception, match="direction"):
        run_factor_eval(db, repo, FID, "up")
    with pytest.raises(Exception, match="horizon"):
        run_factor_eval(db, repo, FID, "asc", horizons=(2,))
    with pytest.raises(Exception, match="未物化"):
        run_factor_eval(db, repo, "nope_fl", "asc")


# ── 报告落库与渲染 ──────────────────────────────────────
def test_save_eval_report_idempotent(m3):
    db, repo = m3
    rep = run_factor_eval(db, repo, FID, "asc", horizons=(1, 3),
                          start="2021-03-01", end="2021-06-30",
                          as_of="2026-10-10", code_version="m3-test")
    res = save_eval_report(rep, db)
    assert len(res["saved"]) == 2
    con = duckdb.connect(str(db), read_only=True)
    try:
        rows = con.execute("""SELECT eval_id, factor_version, lineage_id, horizon,
                              ic_mean, nw_t, report_json FROM fl_factor_eval
                              ORDER BY horizon""").fetchall()
    finally:
        con.close()
    assert len(rows) == 2 and rows[0][4] == pytest.approx(-1.0)
    assert "m3-test" in rows[0][6] and '"lineage_id"' in rows[0][6]
    save_eval_report(rep, db)                                # 重跑幂等覆盖
    con = duckdb.connect(str(db), read_only=True)
    try:
        n = con.execute("SELECT count(*) FROM fl_factor_eval").fetchone()[0]
    finally:
        con.close()
    assert n == 2


def test_render_md_contains_quadruple(m3):
    db, repo = m3
    rep = run_factor_eval(db, repo, FID, "asc", horizons=(1,),
                          start="2021-03-01", end="2021-06-30",
                          as_of="2026-10-10", code_version="m3-test")
    md = render_md(rep)
    assert "val_pe_ttm" in md and "as_of=2026-10-10" in md and "m3-test" in md
    assert "NW-t" in md and "单调性" in md and "分层平均收益" in md

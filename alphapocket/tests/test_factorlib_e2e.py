"""M1 端到端验收：prepare → load → 状态机激活 → materialize → query → golden 对账 → PIT 审计。

golden 期望值为手工核对的独立算术表（见 tests/fixtures/fl_golden/make_fixture.py），
本测试不使用被测代码生成任何期望值。
运行：``cd alphapocket && python -m pytest tests/test_factorlib_e2e.py -q``
"""
from __future__ import annotations

import duckdb
import pytest

from factorlib import load_factor, set_status
from factorlib.audit import pit_audit
from factorlib.materialize import materialize, query
from factorlib.registry import resolve_backtest_start

FIDS = ("val_pe_ttm", "mom_20d_fl", "sig_mom20_rank_fl", "eps_revise_fl")


@pytest.fixture
def e2e(fl_db, fl_repo):
    """全链路执行一遍：激活全部因子并物化。返回 (db, repo)。"""
    db, summary = fl_db
    assert summary["fl_dws_financial_ttm"] > 0            # TTM 阶梯已生成
    for fid in FIDS:
        set_status(fl_repo, fid, "active", who="m1-acceptance", why="端到端验收",
                   gate={"ok": True, "reasons": []})   # M5：测试夹具显式门禁结论
        materialize(load_factor(fl_repo, fid), db, mode="full")
    return db, fl_repo


def test_pit_meta_matches_golden(e2e, golden):
    """40 个检查点（10 股 × 4 时点）：值 + PIT 元数据全部对上手工表。"""
    db, _ = e2e
    con = duckdb.connect(str(db), read_only=True)
    try:
        for case in golden["cases"]:
            hits = query(db, "val_pe_ttm", dt=case["dt"], secid=case["secid"])
            assert len(hits) == 1, f"缺失：{case}"
            row = hits[0]
            assert row["value"] == pytest.approx(case["expected"], rel=1e-9)
            assert row["value"] == pytest.approx(case["close"] / case["ttm_eps"], rel=1e-9)
            pit = con.execute("""SELECT used_period, used_announce FROM fl_factor_pit_meta
                                 WHERE factor_id='val_pe_ttm' AND dt=? AND secid=?""",
                              [case["dt"], case["secid"]]).fetchone()
            assert pit is not None
            assert str(pit[0]) == case["used_period"]
            assert str(pit[1]) == case["used_announce"]
    finally:
        con.close()


def test_pit_announce_switch_at_checkpoint(e2e):
    """同日不同股票用不同报告期（早/晚公告分组）：PIT 阶梯的横截面证据。"""
    db, _ = e2e
    con = duckdb.connect(str(db), read_only=True)
    try:
        rows = con.execute("""SELECT secid, used_period, used_announce FROM fl_factor_pit_meta
                              WHERE factor_id='val_pe_ttm' AND dt = DATE '2021-04-30'
                              ORDER BY secid""").fetchall()
    finally:
        con.close()
    by_secid = {s: (str(p), str(a)) for s, p, a in rows}
    assert by_secid["600001.SH"] == ("2021-03-31", "2021-04-30")   # 早公告组 → 已见 Q1'21
    assert by_secid["600006.SH"] == ("2020-12-31", "2021-04-29")   # 晚公告组 → 仍年报
    assert len(rows) == 10


def test_loss_making_absent_after_announce(e2e):
    """S07 亏损：2021-05-28（Q1 公告）后 PE 缺席；缺席行亦无 PIT 元数据（NULL 语义 = 整行缺席）。"""
    db, _ = e2e
    assert query(db, "val_pe_ttm", secid="600007.SH", dt="2021-05-31") == []
    con = duckdb.connect(str(db), read_only=True)
    try:
        n = con.execute("""SELECT count(*) FROM fl_factor_pit_meta
                           WHERE factor_id='val_pe_ttm' AND secid='600007.SH'
                           AND dt >= DATE '2021-05-28'""").fetchone()[0]
    finally:
        con.close()
    assert n == 0


def test_price_factor_window_and_signal(e2e):
    db, _ = e2e
    assert query(db, "mom_20d_fl", dt="2021-01-29") == []   # 前 20 交易日无值
    mom = {r["secid"]: r["value"] for r in query(db, "mom_20d_fl", dt="2021-02-01")}
    assert len(mom) == 10
    for i in range(1, 11):                                  # CP1 已跳档 → 跨平台动量
        assert mom[f"6000{i:02d}.SH"] == pytest.approx((9.5 + i) / (9 + i) - 1)
    sig = query(db, "sig_mom20_rank_fl", dt="2021-02-01")
    assert len(sig) == 10 and all(0.0 <= r["value"] <= 1.0 for r in sig)


def test_udf_factor_pit_values(e2e):
    db, _ = e2e
    r = query(db, "eps_revise_fl", dt="2021-04-30", secid="600001.SH")
    assert len(r) == 1 and r[0]["value"] == pytest.approx(0.35 - 0.25)
    assert query(db, "eps_revise_fl", dt="2021-04-30", secid="600006.SH")[0]["value"] \
        == pytest.approx(0.0)


def test_final_audit_pass(e2e):
    db, _ = e2e
    assert pit_audit(db)["pass"] is True


def test_lineage_single_per_factor(e2e):
    """同一次物化内 lineage 唯一（可复现性指纹未随行漂移）。"""
    db, _ = e2e
    con = duckdb.connect(str(db), read_only=True)
    try:
        for fid in FIDS:
            n = con.execute("""SELECT count(DISTINCT lineage_id) FROM fl_factor_value_daily
                               WHERE factor_id = ?""", [fid]).fetchone()[0]
            assert n == 1, fid
    finally:
        con.close()


def test_backtest_start_and_state_history(e2e):
    db, repo = e2e
    metas = [load_factor(repo, fid) for fid in FIDS]
    assert all(m.status == "active" for m in metas)
    con = duckdb.connect(str(db), read_only=True)
    try:
        start = resolve_backtest_start(con, metas)
    finally:
        con.close()
    assert str(start) == "2021-01-24"
    import json
    st = json.loads((repo / "factors" / "factor_registry_state.json").read_text(encoding="utf-8"))
    assert st["val_pe_ttm"]["history"][0]["who"] == "m1-acceptance"

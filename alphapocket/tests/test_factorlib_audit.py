"""M1 PIT 审计测试：golden 全量通过 + 双保险篡改注入（未来函数 / 元数据篡改）均被捕获。

运行：``cd alphapocket && python -m pytest tests/test_factorlib_audit.py -q``
"""
from __future__ import annotations

import duckdb
import pytest

from factorlib.audit import pit_audit
from factorlib.materialize import materialize
from factorlib.registry import FactorMeta, load_factor


def _repo_of(db):
    return db.parent / "repo"


@pytest.fixture
def audited_db(fl_db):
    """物化全部 4 因子后的库（含 PIT 元数据的两个因子产生审计行）。"""
    db, _ = fl_db
    repo = _repo_of(db)
    for fid in ("val_pe_ttm", "mom_20d_fl", "sig_mom20_rank_fl", "eps_revise_fl"):
        materialize(load_factor(repo, fid), db, mode="full")
    return db


def test_golden_audit_pass(audited_db):
    report = pit_audit(audited_db)
    assert report["pass"] is True
    assert report["check1_early_value"]["violations"] == 0
    assert report["check2_meta_tamper"]["violations"] == 0


def test_audit_artifact_written(audited_db, tmp_path):
    report = pit_audit(audited_db, artifact_dir=tmp_path / "artifacts")
    assert report["artifact"].startswith(str(tmp_path))
    assert (tmp_path / "artifacts").exists()


def test_tamper_period_for_announce_is_caught(fl_db):
    """篡改注入（M1 DoD）：SQL 中 announce_date 条件 → period 条件，检查 1 必须捕获。

    M5 T-L3 叠加：同 version 定义漂移先被版本冻结守卫拒绝（bump_version 才是正道）；
    升版篡改（模拟绕过守卫的口径变更）物化放行后，PIT 审计仍必须捕获。
    """
    from factorlib.materialize import MaterializeError
    db, _ = fl_db
    repo = _repo_of(db)
    meta = load_factor(repo, "val_pe_ttm")
    bad_sql = meta.sql.replace("t.announce_date <= d.dt", "t.period <= d.dt")
    assert bad_sql != meta.sql
    tampered = FactorMeta(**{**meta.to_dict(), "sql": bad_sql})

    # 基线：正确定义先物化（建立冻结守卫的指纹登记）
    materialize(meta, db, mode="full")

    # 同 version 重写定义 → 冻结守卫直接拒绝（T-L3 红线）
    with pytest.raises(MaterializeError, match="bump_version"):
        materialize(tampered, db, mode="full")

    # 升版篡改 → 放行，审计检查 1 捕获早值
    tampered_v9 = FactorMeta(**{**tampered.to_dict(), "version": "9.9.9"})
    s = materialize(tampered_v9, db, mode="full")
    assert s["written"] > 0
    report = pit_audit(db)
    assert report["pass"] is False
    assert report["check1_early_value"]["violations"] > 0
    assert report["check1_early_value"]["samples"], "需给出违例样本"

    # 整改闭环：回滚篡改版本行 → 恢复正确定义重算 → 审计恢复通过
    con = duckdb.connect(str(db))
    try:
        con.execute("DELETE FROM fl_factor_value_daily WHERE factor_version = '9.9.9'")
        con.execute("DELETE FROM fl_factor_lineage WHERE factor_version = '9.9.9'")
    finally:
        con.close()
    materialize(meta, db, mode="full")
    assert pit_audit(db)["pass"] is True


def test_tamper_pit_meta_is_caught(audited_db):
    """篡改注入：值正确、元数据被改 → 检查 2（元数据↔TTM 真源对账）捕获。"""
    db = audited_db
    con = duckdb.connect(str(db))
    try:
        con.execute("""UPDATE fl_factor_pit_meta SET used_announce = DATE '2020-01-01'
                       WHERE factor_id = 'val_pe_ttm' AND dt = DATE '2021-06-30'
                       AND secid = '600001.SH'""")
    finally:
        con.close()
    report = pit_audit(db)
    assert report["pass"] is False
    assert report["check2_meta_tamper"]["violations"] >= 1
    sample = report["check2_meta_tamper"]["samples"][0]
    assert str(sample["used_announce"]) == "2020-01-01"


def test_audit_covers_both_financial_factors(audited_db):
    """两个含 PIT 元数据的因子都有审计行（防审计空转：join 不到 = 假绿）。"""
    con = duckdb.connect(str(audited_db), read_only=True)
    try:
        fids = {r[0] for r in con.execute(
            "SELECT DISTINCT factor_id FROM fl_factor_pit_meta").fetchall()}
        rows = con.execute("SELECT count(*) FROM fl_factor_pit_meta").fetchone()[0]
    finally:
        con.close()
    assert {"val_pe_ttm", "eps_revise_fl"} <= fids
    assert rows > 100

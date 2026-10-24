"""M1 物化管道测试：三模式、幂等、lineage 可复现性、依赖门禁、变换链接入。

运行：``cd alphapocket && python -m pytest tests/test_factorlib_materialize.py -q``
"""
from __future__ import annotations

import duckdb
import pytest

from factorlib.materialize import MaterializeError, lineage_id, materialize, query
from factorlib.registry import FactorMeta, load_factor


def _repo_of(db):
    return db.parent / "repo"


def _count(db, factor_id=None, dt=None):
    con = duckdb.connect(str(db), read_only=True)
    try:
        sql = "SELECT count(*) FROM fl_factor_value_daily WHERE 1=1"
        args = []
        if factor_id:
            sql += " AND factor_id = ?"
            args.append(factor_id)
        if dt:
            sql += " AND dt = ?"
            args.append(dt)
        return con.execute(sql, args).fetchone()[0]
    finally:
        con.close()


# ── full / as_of / 基本产出 ────────────────────────────
def test_full_mode_writes_values_and_pit(fl_db):
    db, _ = fl_db
    meta = load_factor(_repo_of(db), "val_pe_ttm")
    s = materialize(meta, db, mode="full")
    assert s["written"] > 0 and s["pit_meta"] == s["written"]
    assert s["start"] == "2021-01-04" and s["end"] == "2021-06-30"
    assert s["version"] == "1.0.0" and len(s["lineage_id"]) == 64


def test_as_of_mode_single_day(fl_db):
    db, _ = fl_db
    meta = load_factor(_repo_of(db), "val_pe_ttm")
    materialize(meta, db, mode="full")
    full_row = query(db, "val_pe_ttm", dt="2021-04-30")
    materialize(meta, db, mode="as_of", as_of="2021-04-30")
    snap_row = query(db, "val_pe_ttm", dt="2021-04-30")
    assert len(snap_row) == 10 and full_row == snap_row    # 快照可重放，逐行一致


def test_incremental_after_full_is_noop_then_appends(fl_db):
    db, _ = fl_db
    meta = load_factor(_repo_of(db), "val_pe_ttm")
    materialize(meta, db, mode="full")
    s = materialize(meta, db, mode="incremental")
    assert s["written"] == 0 and "已最新" in s["note"]

    # 模拟新交易日（2021-07-01，周四）：新 K 线 + 日历
    con = duckdb.connect(str(db))
    try:
        for i in range(1, 11):
            close = 11.0 + i                      # CP4 后平台 close = 11+i
            con.execute("INSERT INTO fl_daily_bar VALUES (?,?,?,?,?,?,?,?,?)",
                        (f"6000{i:02d}.SH", "2021-07-01", close, close, close, close,
                         10000, close * 10000, 0.0))
        con.execute("INSERT INTO fl_trade_calendar VALUES (DATE '2021-07-01')")
    finally:
        con.close()
    s2 = materialize(meta, db, mode="incremental")
    # S07 亏损（ttm_eps=-0.15）→ 缺席，仅 9 行
    assert s2["written"] == 9 and s2["start"] == "2021-07-01"
    assert {r["secid"] for r in query(db, "val_pe_ttm", dt="2021-07-01")} \
        == {f"6000{i:02d}.SH" for i in range(1, 11)} - {"600007.SH"}


def test_invalid_mode_and_missing_as_of(fl_db):
    db, _ = fl_db
    meta = load_factor(_repo_of(db), "val_pe_ttm")
    with pytest.raises(MaterializeError, match="mode 非法"):
        materialize(meta, db, mode="delta")
    with pytest.raises(MaterializeError, match="as_of"):
        materialize(meta, db, mode="as_of")


def test_missing_dependency_gates(fl_db):
    db, _ = fl_db
    con = duckdb.connect(str(db))
    con.execute("DROP TABLE fl_dws_financial_ttm")
    con.close()
    meta = load_factor(_repo_of(db), "val_pe_ttm")
    with pytest.raises(MaterializeError, match="依赖表缺失"):
        materialize(meta, db, mode="full")


# ── 幂等 + lineage 可复现性 ────────────────────────────
def test_full_rerun_idempotent(fl_db):
    db, _ = fl_db
    meta = load_factor(_repo_of(db), "val_pe_ttm")
    materialize(meta, db, mode="full")
    rows1 = query(db, "val_pe_ttm")
    materialize(meta, db, mode="full")
    rows2 = query(db, "val_pe_ttm")
    assert rows1 == rows2                              # 逐行一致（值+lineage）


def test_lineage_stable_and_sensitive(fl_db):
    db, _ = fl_db
    meta = load_factor(_repo_of(db), "val_pe_ttm")
    lid1 = lineage_id(meta, db)
    assert lineage_id(meta, db) == lid1                # 同定义同数据 → 稳定

    changed = FactorMeta(**{**meta.to_dict(), "sql": meta.sql + "\n-- 口径微调"})
    assert lineage_id(changed, db) != lid1             # 定义变 → lineage 变

    con = duckdb.connect(str(db))
    con.execute("INSERT INTO fl_daily_bar VALUES ('600099.SH', DATE '2021-06-14', "
                "1,1,1,1,1,1,0.0)")
    con.close()
    assert lineage_id(meta, db) != lid1                # 数据面变 → lineage 变


# ── 变换链接入 ────────────────────────────────────────
def _raw_meta(**kw) -> FactorMeta:
    base = {"id": "raw_close_z", "name": "raw", "category": "F1_technical", "factor_type": "alpha",
                "engine": "sql", "direction": "high", "universe": ["CN"], "inputs": ["fl_daily_bar"],
                "lookback_days": 0, "pit": True, "version": "1.0.0", "status": "draft", "description": "d",
                "sql": """SELECT dt, secid, close AS value,
                              CAST(NULL AS DATE) AS used_period,
                              CAST(NULL AS DATE) AS used_announce
                       FROM fl_daily_bar WHERE dt BETWEEN $start AND $end"""}
    base.update(kw)
    return FactorMeta(**base)


def test_transform_zscore_applied_per_date(fl_db):
    db, _ = fl_db
    meta = _raw_meta(transform=["zscore"])
    materialize(meta, db, mode="full")
    con = duckdb.connect(str(db), read_only=True)
    try:
        rows = con.execute("""SELECT dt, avg(value), count(*) FROM fl_factor_value_daily
                              WHERE factor_id='raw_close_z' GROUP BY dt""").fetchall()
    finally:
        con.close()
    assert rows and all(abs(r[1]) < 1e-9 and r[2] == 10 for r in rows)


def test_transform_industry_neutralized(fl_db):
    db, _ = fl_db
    meta = _raw_meta(transform=["zscore"], neutralization=["industry"])
    materialize(meta, db, mode="as_of", as_of="2021-04-30")
    con = duckdb.connect(str(db), read_only=True)
    try:
        rows = con.execute("""SELECT d.industry, avg(v.value)
                              FROM fl_factor_value_daily v
                              JOIN fl_dim_security d ON d.secid = v.secid
                              WHERE v.factor_id='raw_close_z' AND v.dt = DATE '2021-04-30'
                              GROUP BY d.industry""").fetchall()
    finally:
        con.close()
    assert len(rows) == 3 and all(abs(r[1]) < 1e-9 for r in rows)


def test_transform_bad_step_fails_materialize(fl_db):
    db, _ = fl_db
    meta = _raw_meta(transform=["no_such_step"])
    with pytest.raises(MaterializeError, match="变换链失败"):
        materialize(meta, db, mode="as_of", as_of="2021-04-30")

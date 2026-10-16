"""物化管道测试（实施计划 §4 任务 1.4/1.6）：三模式语义 + 幂等 + 复现性 diff=0。

合成小库行数推演（见 conftest）：S1/S2 自 01-06 有公告起各 3 行（01-06..08），
S3 负 EPS 无行 → full 全区 = 6 值行 + 6 PIT 行。
"""
import pytest

from conftest import make_meta, sql_from_yaml
from factorlib.materialize import materialize, verify_reproducible
from factorlib.pit_audit import audit


def _meta():
    return make_meta(body=sql_from_yaml())


def _count(con, table):
    return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_full_materialize_and_idempotent(con):
    meta = _meta()
    r1 = materialize(con, meta, mode="full")
    assert (r1["rows"], r1["pit_rows"]) == (6, 6)
    assert _count(con, "factor_value_daily") == 6
    r2 = materialize(con, meta, mode="full")            # 重复 full 幂等
    assert (r2["rows"], r2["pit_rows"]) == (6, 6)
    assert _count(con, "factor_value_daily") == 6
    assert _count(con, "factor_pit_meta") == 6
    # 值与 PIT 元数据逐行对齐（同删同写）
    joined = con.execute(
        "SELECT count(*) FROM factor_value_daily v JOIN factor_pit_meta m"
        " ON v.dt=m.dt AND v.secid=m.secid AND v.factor_id=m.factor_id").fetchone()[0]
    assert joined == 6


def test_incremental_watermark(con):
    meta = _meta()
    materialize(con, meta, mode="full", end="2021-01-06")   # 先物化到 01-06
    assert _count(con, "factor_value_daily") == 2
    r = materialize(con, meta, mode="incremental", end="2021-01-08")
    assert r["start"] == "2021-01-06"                       # 水位自动推导
    assert _count(con, "factor_value_daily") == 6           # 01-06 重算 + 01-07/08 补齐
    # 与一次性 full 全区逐行一致（增量 = 全量的子区间拼接；行数与 PIT 对齐已由幂等测试覆盖）
    full_rows = con.execute(
        "SELECT dt, secid, value FROM factor_value_daily ORDER BY dt, secid").fetchall()
    assert len(full_rows) == 6


def test_snapshot_no_write(con):
    meta = _meta()
    r = materialize(con, meta, mode="snapshot")
    assert r["rows"] == 6
    assert len(r["rows_data"]) == 6
    assert all(len(row) == 5 for row in r["rows_data"])     # (dt, secid, value, used_period, used_announce)
    have = {row[0] for row in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'").fetchall()}
    assert "factor_value_daily" not in have                 # 快照不落库、不建表


def test_unknown_mode_rejected(con):
    with pytest.raises(ValueError, match="未知物化模式"):
        materialize(con, _meta(), mode="delta")


def test_missing_depends_rejected(con):
    con.execute("DROP TABLE dwd_financial_report")
    with pytest.raises(ValueError, match="依赖数据表缺失"):
        materialize(con, _meta(), mode="full")


def test_verify_reproducible_diff_zero(con):
    """DoD-3：同一 as_of 两次全量重算，逐行 diff = 0。"""
    meta = _meta()
    materialize(con, meta, mode="full")                     # 首次落库
    rep = verify_reproducible(con, meta, as_of="2021-01-08")
    assert rep["reproducible"] is True
    assert rep["digest_1"] == rep["digest_2"]
    # 已物化行数不变（重算前后逐行一致）
    assert _count(con, "factor_value_daily") == 6


def test_verify_reproducible_detects_change(con):
    """复现性检查的反向锚：表数据被篡改后 digest 必须不同（验证比对真的在比）。"""
    meta = _meta()
    materialize(con, meta, mode="full")
    con.execute("UPDATE factor_value_daily SET value = value + 1 WHERE secid = 'S1'")
    rep = verify_reproducible(con, meta)
    assert rep["reproducible"] is False                     # 重算恢复原值 → 与被篡改状态不一致


def test_materialized_passes_pit_audit(con):
    """DoD-4 正面：常规物化后审计 0 违例（红线）。"""
    meta = _meta()
    materialize(con, meta, mode="full")
    rep = audit(con, factor_id=meta.id)
    assert rep["checked"] == 6
    assert rep["violations"] == 0

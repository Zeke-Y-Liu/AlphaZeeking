"""端到端回归（DoD-1）：真实 repo 数据走完 注册→计算→物化→查询→审计→复现 全流程，无人工干预。

数据源：alphapocket/remote/repo（market/daily_bars.json + factors/fundamental_values.json），
经独立建库装载（与 applib data.py 双实现交叉锚定数据契约）。
"""
import pytest

from conftest import ALPHAPOCKET_REPO, sql_from_yaml

pytestmark = pytest.mark.skipif(
    not (ALPHAPOCKET_REPO / "market/daily_bars.json").exists(),
    reason="alphapocket remote repo 不存在（端到端跳过）")


def test_e2e_register_materialize_query_audit(repo_con):
    from factorlib import load_definitions, materialize, pit_audit, verify_reproducible
    from factorlib.registry import set_status

    # 1) 注册：YAML → FactorMeta（含校验与排序；root = factorlib 仓库根）
    metas = load_definitions(ALPHAPOCKET_REPO.parent.parent.parent / "factorlib")
    meta = [m for m in metas if m.id == "val_pe_ttm"][0]
    assert meta.engine == "sql" and meta.status == "draft"
    meta = set_status(meta, "active")                       # draft → active

    # 2) 计算+物化：full 全量
    r = materialize(repo_con, meta, mode="full")
    assert r["rows"] > 0

    # 3) 查询：长表可按 (factor_id, dt) 消费
    dt, n = repo_con.execute(
        "SELECT max(dt), count(*) FROM factor_value_daily WHERE factor_id = 'val_pe_ttm'"
    ).fetchone()
    assert n == r["rows"]

    # 4) PIT 审计：0 违例（红线）
    rep = pit_audit(repo_con, factor_id="val_pe_ttm")
    assert rep["checked"] == r["pit_rows"]
    assert rep["violations"] == 0

    # 5) 复现性：两次全量重算逐行 diff = 0
    rr = verify_reproducible(repo_con, meta)
    assert rr["reproducible"] is True

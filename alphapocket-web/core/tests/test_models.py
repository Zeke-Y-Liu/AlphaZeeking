"""模型注册与关键约束（T2.1；不依赖 PG，仅检查 metadata）。"""
from __future__ import annotations

from sqlalchemy import CheckConstraint, UniqueConstraint

from app.models import (
    AnalysisProfile,
    AuditLog,
    Base,
    ExecOrder,
    Job,
    Portfolio,
    Position,
    RebalancePlan,
    Run,
    Security,
    User,
    VerifyResult,
)

EXPECTED_TABLES = {
    "users",
    "securities",
    "portfolios",
    "positions",
    "jobs",
    "runs",
    "verify_results",
    "rebalance_plans",
    "exec_orders",
    "audit_log",
    "analysis_profiles",
}

MODELS = [
    User, Security, Portfolio, Position, Job,
    Run, VerifyResult, RebalancePlan, ExecOrder, AuditLog,
    AnalysisProfile,
]


def _unique_columns(table_name: str) -> set[tuple[str, ...]]:
    table = Base.metadata.tables[table_name]
    return {
        tuple(col.name for col in con.columns)
        for con in table.constraints
        if isinstance(con, UniqueConstraint)
    }


def test_ten_tables_registered() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_export_names_map_to_tables() -> None:
    assert {model.__tablename__ for model in MODELS} == EXPECTED_TABLES


def test_positions_check_and_unique() -> None:
    table = Base.metadata.tables["positions"]
    assert ("portfolio_id", "secid") in _unique_columns("positions")
    checks = [
        str(con.sqltext) for con in table.constraints if isinstance(con, CheckConstraint)
    ]
    assert any("weight" in expr for expr in checks)


def test_runs_run_uid_unique() -> None:
    assert ("run_uid",) in _unique_columns("runs")


def test_exec_orders_exec_uid_unique() -> None:
    assert ("exec_uid",) in _unique_columns("exec_orders")


def test_audit_entity_index_registered() -> None:
    table = Base.metadata.tables["audit_log"]
    assert "idx_audit_entity" in {idx.name for idx in table.indexes}

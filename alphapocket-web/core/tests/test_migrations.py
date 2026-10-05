"""迁移往返验证（T2.1；连真实 PG，不可达则跳过）。

``upgrade head`` → 十表齐 → ``downgrade base`` → 表清空 → ``upgrade head``。
开始时先 drop 全部表，保证可重复运行。
"""
from __future__ import annotations

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect, text

from app.models import Base

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
}


def _user_tables(engine: Engine) -> set[str]:
    names = set(inspect(engine).get_table_names(schema="public"))
    names.discard("alembic_version")
    return names


def _reset(engine: Engine) -> None:
    Base.metadata.drop_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))


def test_upgrade_downgrade_roundtrip(pg_engine: Engine, alembic_config: Config) -> None:
    _reset(pg_engine)

    command.upgrade(alembic_config, "head")
    assert _user_tables(pg_engine) == EXPECTED_TABLES

    command.downgrade(alembic_config, "base")
    assert _user_tables(pg_engine) == set()

    command.upgrade(alembic_config, "head")
    assert _user_tables(pg_engine) == EXPECTED_TABLES

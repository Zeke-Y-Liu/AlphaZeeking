"""initial schema（§5.1 DDL 冻结）

Revision ID: 0001
Revises:
Create Date: 2026-10-05

十张表 + idx_audit_entity 索引，逐字段对齐实施书 §5.1。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("totp_secret", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), nullable=False, server_default=sa.text("'owner'")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )

    op.create_table(
        "securities",
        sa.Column("secid", sa.Text(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("asset_type", sa.Text(), nullable=False),
        sa.Column("industry", sa.Text(), nullable=True),
        sa.Column("sina_symbol", sa.Text(), nullable=False),
        sa.Column("ftype", sa.Text(), nullable=True),
        sa.Column("settle", sa.Text(), nullable=True),
        sa.Column("cost", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False, server_default=sa.text("'user'")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("secid"),
    )

    op.create_table(
        "portfolios",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("pid", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("base_ccy", sa.Text(), nullable=False, server_default=sa.text("'CNY'")),
        sa.Column(
            "strategy",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pid"),
    )

    op.create_table(
        "positions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("portfolio_id", sa.BigInteger(), nullable=False),
        sa.Column("secid", sa.Text(), nullable=False),
        sa.Column("weight", sa.Numeric(6, 3), nullable=False),
        sa.CheckConstraint("weight > 0"),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["secid"], ["securities.secid"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("portfolio_id", "secid"),
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("portfolio_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'queued'")),
        sa.Column(
            "params",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("progress", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_uid", sa.Text(), nullable=False),
        sa.Column("job_id", sa.Text(), nullable=False),
        sa.Column("portfolio_id", sa.BigInteger(), nullable=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=True),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "artifact_keys",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_uid"),
    )

    op.create_table(
        "verify_results",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.Text(), nullable=False),
        sa.Column("check", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "rebalance_plans",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("plan_uid", sa.Text(), nullable=False),
        sa.Column("portfolio_id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_uid"),
    )

    op.create_table(
        "exec_orders",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("exec_uid", sa.Text(), nullable=False),
        sa.Column("plan_id", sa.BigInteger(), nullable=False),
        sa.Column("portfolio_id", sa.BigInteger(), nullable=False),
        sa.Column("capital_cny", sa.Numeric(14, 2), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("orders", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_by", sa.Text(), nullable=True),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fills", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["plan_id"], ["rebalance_plans.id"]),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("exec_uid"),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("entity", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_audit_entity", "audit_log", ["entity", "entity_id"])


def downgrade() -> None:
    op.drop_index("idx_audit_entity", table_name="audit_log")
    # 按外键依赖逆序删除
    op.drop_table("exec_orders")
    op.drop_table("rebalance_plans")
    op.drop_table("verify_results")
    op.drop_table("runs")
    op.drop_table("jobs")
    op.drop_table("positions")
    op.drop_table("audit_log")
    op.drop_table("portfolios")
    op.drop_table("securities")
    op.drop_table("users")

"""analysis_profiles 表（单公司分析档案）

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-21

- 新增 ``analysis_profiles``：aid / secid / name / note / modules(JSONB) / status / 时间戳；
- 不建外键到 securities（与 portfolios.positions.secid 同口径：secid 语义引用，
  标的注册链路已保证存在性；避免归档档案被标的侧删除阻塞）；
- 不建索引（分析档案数量级 ~十级，全表扫描足够）。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "analysis_profiles",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("aid", sa.Text(), nullable=False, unique=True),
        sa.Column("secid", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("modules", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("analysis_profiles")

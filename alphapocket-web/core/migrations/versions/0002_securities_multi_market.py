"""securities 多市场扩展（V4-β：港股/美股 + 港股通标记 + 币种）

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-06

- ``currency``：交易计价币种（CNY/HKD/USD）；按 market 回填（A→CNY，HK→HKD，US→USD），
  历史 A 股行全部落 CNY；新注册标的由注册链路按市场写入。
- ``hk_connect``：港股通标记（仅港股有值；A股/美股 NULL）。
- 市场扩容不建索引（universe 规模 ~百级，全表扫描足够）。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("securities", sa.Column("currency", sa.Text(), nullable=True))
    op.add_column("securities", sa.Column("hk_connect", sa.Boolean(), nullable=True))
    # 按市场回填币种（历史行均为 A 股；HK/US 行由种子/注册链路写入时自带 currency）
    op.execute("UPDATE securities SET currency = 'CNY' WHERE market = 'A' AND currency IS NULL")
    op.execute("UPDATE securities SET currency = 'HKD' WHERE market = 'HK' AND currency IS NULL")
    op.execute("UPDATE securities SET currency = 'USD' WHERE market = 'US' AND currency IS NULL")


def downgrade() -> None:
    op.drop_column("securities", "hk_connect")
    op.drop_column("securities", "currency")

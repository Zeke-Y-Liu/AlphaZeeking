"""rebalance_plans / exec_orders 表模型（§5.1）。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Numeric, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class RebalancePlan(Base):
    """调仓计划；``payload`` 为 §5.2 plan 契约原样。"""

    __tablename__ = "rebalance_plans"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    plan_uid: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    portfolio_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("portfolios.id"), nullable=False
    )
    run_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("runs.id"), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class ExecOrder(Base):
    """执行单；``orders`` / ``fills`` 为契约 JSON。"""

    __tablename__ = "exec_orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    exec_uid: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    plan_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("rebalance_plans.id"), nullable=False
    )
    portfolio_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("portfolios.id"), nullable=False
    )
    capital_cny: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    orders: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_by: Mapped[str | None] = mapped_column(Text)
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fills: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

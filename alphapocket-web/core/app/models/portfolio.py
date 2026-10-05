"""portfolios / positions 表模型（§5.1）。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Portfolio(Base):
    """组合定义；``strategy`` 为预留策略参数（§5.2）。"""

    __tablename__ = "portfolios"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    pid: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    base_ccy: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'CNY'"))
    strategy: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Position(Base):
    """组合持仓；``weight`` 为百分数（20.0），且 > 0。"""

    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "secid"),
        CheckConstraint("weight > 0"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False
    )
    secid: Mapped[str] = mapped_column(Text, ForeignKey("securities.secid"), nullable=False)
    weight: Mapped[Decimal] = mapped_column(Numeric(6, 3), nullable=False)

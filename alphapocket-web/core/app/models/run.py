"""runs / verify_results 表模型（§5.1）。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Run(Base):
    """一次计算运行；``artifact_keys`` 为 MinIO 产物 key 列表。"""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_uid: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    job_id: Mapped[str] = mapped_column(Text, ForeignKey("jobs.id"), nullable=False)
    portfolio_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("portfolios.id"), nullable=True
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    as_of: Mapped[date | None] = mapped_column(Date)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    artifact_keys: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class VerifyResult(Base):
    """六项验证结果；``detail`` 为计数/明细/豁免说明。"""

    __tablename__ = "verify_results"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_id: Mapped[str] = mapped_column(Text, ForeignKey("jobs.id"), nullable=False)
    check: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

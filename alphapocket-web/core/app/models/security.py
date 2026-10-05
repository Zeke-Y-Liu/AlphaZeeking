"""securities 表模型（§5.1）。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Security(Base):
    """标的元数据；``secid`` 形态 ``510300.SH`` / ``002014.SZ``。"""

    __tablename__ = "securities"

    secid: Mapped[str] = mapped_column(Text, primary_key=True)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    asset_type: Mapped[str] = mapped_column(Text, nullable=False)
    industry: Mapped[str | None] = mapped_column(Text)
    sina_symbol: Mapped[str] = mapped_column(Text, nullable=False)
    ftype: Mapped[str | None] = mapped_column(Text)
    settle: Mapped[str | None] = mapped_column(Text)
    cost: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'user'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

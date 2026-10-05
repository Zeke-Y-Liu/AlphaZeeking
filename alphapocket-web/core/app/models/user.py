"""users 表模型（§5.1）。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class User(Base):
    """演示/真实用户；``password_hash`` 为 argon2id 哈希。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    totp_secret: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'owner'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

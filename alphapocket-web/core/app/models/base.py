"""SQLAlchemy 2.0 声明式基类（§5.1 PG Schema）。

统一使用 ``Mapped[...]`` / ``mapped_column`` 注解风格。
"""
from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """全部 ORM 模型的声明式基类（``Base.metadata`` 为迁移真源）。"""

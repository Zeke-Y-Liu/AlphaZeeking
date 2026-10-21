"""analysis_profiles 表模型（单公司分析档案）。

设计：档案 = 一个 secid 的分析入口；创建时同步建「影子组合」（单标的 100% 组合，
``strategy={"kind": "analysis_shadow", "aid": <aid>}``）——回测/评估/调仓复用现有
全链路，分析域不新建计算管道。删除档案 → 软删 + 级联归档影子组合。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AnalysisProfile(Base):
    """单公司分析档案；``modules`` 为四模块开关（基本面/行业/技术/消息）。"""

    __tablename__ = "analysis_profiles"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    aid: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    secid: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    modules: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

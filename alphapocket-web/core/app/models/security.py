"""securities 表模型（§5.1；V4-β 多市场扩展）。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Security(Base):
    """标的元数据；``secid`` 形态 ``510300.SH`` / ``002014.SZ`` / ``00700.HK`` / ``AAPL.US``。

    V4-β 多市场字段：
    - ``market``：``A``（A股/基金）/ ``HK``（港股）/ ``US``（美股）；
    - ``currency``：交易计价币种 ``CNY`` / ``HKD`` / ``USD``（A股缺省 CNY，历史行回填）；
    - ``hk_connect``：港股通标记——仅港股有意义（``True`` 港股通标的 / ``False`` 非港股通 /
      ``NULL`` 非港股或未知；来源：雪球港股通名单，降级静态快照）。
    """

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
    currency: Mapped[str | None] = mapped_column(Text)
    hk_connect: Mapped[bool | None] = mapped_column(Boolean)
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'user'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

"""seed 内容与幂等（T2.1；连真实 PG，不可达则跳过）。"""
from __future__ import annotations

from decimal import Decimal

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from sqlalchemy import func, select

from app.models import Portfolio, Position, Security, User
from app.seed import run_seed

# α 元数据（applib/datasource.py MULTI5）: secid -> (asset_type, settle)
MULTI5_EXPECTED = {
    "510300.SH": ("ETF", "T+1"),
    "501018.SH": ("LOF", "T+0(跨境LOF)"),
    "518880.SH": ("ETF", "T+0"),
    "513100.SH": ("ETF", "T+0(跨境)"),
    "511260.SH": ("ETF", "T+0"),
}

# V4-β 多市场种子：secid -> (market, currency, hk_connect)
MULTI_MARKET_EXPECTED = {
    "00700.HK": ("HK", "HKD", True),
    "09988.HK": ("HK", "HKD", True),
    "00388.HK": ("HK", "HKD", True),
    "AAPL.US": ("US", "USD", None),
    "MSFT.US": ("US", "USD", None),
    "NVDA.US": ("US", "USD", None),
}


def _count(session: Session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def test_seed_contents_and_idempotency(pg_engine: Engine, alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")

    # 第一次 seed
    run_seed()
    with SessionLocal() as session:
        assert _count(session, User) == 1
        assert _count(session, Security) == 15
        assert (
            session.execute(
                select(func.count()).select_from(Portfolio).where(Portfolio.status == "active")
            ).scalar_one() == 3
        )  # 生效组合恰为 seed 的 3 个；归档（archived）组合不计入
        assert _count(session, Position) == 15

        multi5_id = session.scalar(
            select(Portfolio.id).where(Portfolio.pid == "multi5")
        )
        weights = session.scalars(
            select(Position.weight).where(Position.portfolio_id == multi5_id)
        ).all()
        assert len(weights) == 5
        assert all(weight == Decimal("20.000") for weight in weights)

        for secid, (asset_type, settle) in MULTI5_EXPECTED.items():
            security = session.get(Security, secid)
            assert security is not None, f"{secid} 缺失"
            assert security.asset_type == asset_type
            assert security.settle == settle

        # V4-β：港股/美股种子（市场/币种/港股通标记）+ 混合组合
        for secid, (market, currency, hk_connect) in MULTI_MARKET_EXPECTED.items():
            security = session.get(Security, secid)
            assert security is not None, f"{secid} 缺失"
            assert security.market == market
            assert security.currency == currency
            assert security.hk_connect == hk_connect
        globalmix_id = session.scalar(
            select(Portfolio.id).where(Portfolio.pid == "globalmix")
        )
        gm_secids = session.scalars(
            select(Position.secid).where(Position.portfolio_id == globalmix_id)
        ).all()
        assert set(gm_secids) == {
            "601058.SH", "510300.SH", "00700.HK", "09988.HK", "AAPL.US", "MSFT.US",
        }

    # 第二次 seed：计数不变（幂等）
    run_seed()
    with SessionLocal() as session:
        assert _count(session, User) == 1
        assert _count(session, Security) == 15
        assert (
            session.execute(
                select(func.count()).select_from(Portfolio).where(Portfolio.status == "active")
            ).scalar_one() == 3
        )  # 生效组合恰为 seed 的 3 个；归档（archived）组合不计入
        assert _count(session, Position) == 15

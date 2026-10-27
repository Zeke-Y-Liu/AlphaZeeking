"""seed 内容与幂等（T2.1；连真实 PG，不可达则跳过）。"""
from __future__ import annotations

from decimal import Decimal

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
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

# S26 美股策略组合（2026-10-27）：secid -> (market, currency)
S26_EXPECTED = {
    "PACS.US": ("US", "USD"),
    "LITE.US": ("US", "USD"),
    "AMZN.US": ("US", "USD"),
    "DAVE.US": ("US", "USD"),
    "SNDK.US": ("US", "USD"),
    "VICR.US": ("US", "USD"),
    "SEZL.US": ("US", "USD"),
    "CRDO.US": ("US", "USD"),
    "TTMI.US": ("US", "USD"),
}

# S26 持仓权重（用户提供实仓，合计 93.18%）
S26_WEIGHTS = {
    "PACS.US": Decimal("14.190"),
    "LITE.US": Decimal("12.980"),
    "AMZN.US": Decimal("12.870"),
    "DAVE.US": Decimal("9.650"),
    "SNDK.US": Decimal("9.490"),
    "VICR.US": Decimal("9.260"),
    "SEZL.US": Decimal("8.790"),
    "CRDO.US": Decimal("8.240"),
    "TTMI.US": Decimal("7.710"),
}


def _count(session: Session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def _seed_counts(session: Session) -> dict[str, int]:
    """当前库计数快照（幂等对拍用）。

    注意：计数含**用户运行期新增**的标的/组合（如 sa26、分析影子组合），
    故断言口径为「seed 后 ≥ seed 基线」+「二次 seed 计数不变」——
    精确总数只在纯净库成立，运行库里会随用户数据增长。
    """
    return {
        "users": _count(session, User),
        "securities": _count(session, Security),
        "active_portfolios": session.execute(
            select(func.count()).select_from(Portfolio).where(Portfolio.status == "active")
        ).scalar_one(),
        "positions": _count(session, Position),
    }


def test_seed_contents_and_idempotency(pg_engine: Engine, alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")

    # 第一次 seed
    run_seed()
    with SessionLocal() as session:
        first_counts = _seed_counts(session)
        assert first_counts["users"] == 1
        assert first_counts["securities"] >= 15          # seed 15 只 + 用户注册标的
        assert first_counts["active_portfolios"] >= 3     # seed 的 cn4/multi5/globalmix
        assert first_counts["positions"] >= 15

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

        # S26 美股策略组合：九标的种子 + 权重逐位对拍 + USD 基准币种
        for secid, (market, currency) in S26_EXPECTED.items():
            security = session.get(Security, secid)
            assert security is not None, f"{secid} 缺失"
            assert security.market == market
            assert security.currency == currency
        s26 = session.scalar(select(Portfolio).where(Portfolio.pid == "S26"))
        assert s26 is not None, "S26 组合缺失"
        assert s26.base_ccy == "USD"
        s26_rows = session.execute(
            select(Position.secid, Position.weight)
            .where(Position.portfolio_id == s26.id)
        ).all()
        assert {secid: w for secid, w in s26_rows} == S26_WEIGHTS

    # 第二次 seed：计数不变（幂等；口径见 _seed_counts 注释）
    run_seed()
    with SessionLocal() as session:
        assert _seed_counts(session) == first_counts

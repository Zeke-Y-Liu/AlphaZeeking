"""演示数据 seed（T2.1）。

入口：``core/.venv/bin/python -m app.seed``（cwd=core）。

- 幂等：以自然键 upsert（users.username / securities.secid / portfolios.pid /
  positions.(portfolio_id, secid)），重复执行行数稳定、无重复。
- 事实来源：V4-α ``applib/datasource.py`` 的 CN4 / MULTI5 定义与
  ``remote/repo/portfolio/{cn4,multi5}.json``（此处内嵌常量，seed 不依赖 applib）。
"""
from __future__ import annotations

import sys
from decimal import Decimal

from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.session import session_scope
from app.models import Portfolio, Position, Security, User

# ── 演示用户 ──────────────────────────────────────────────────
DEMO_USERNAME = "demo"
DEMO_PASSWORD = "alphapocket"
DEMO_ROLE = "owner"

_pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

# ── 九标的（字段逐字段对齐 α 元数据）──────────────────────────
# CN4 股票：α datasource 未显式给出 settle/cost/ftype，按 §5.2 股票缺省填充
_STOCK_SETTLE = "A股 T+1"
_STOCK_COST = "印花税卖出0.05%+佣金"
_ETF_COST = "ETF 免印花税·佣金约万2.5"
_LOF_COST = "LOF 免印花税·佣金约万2.5"

SECURITIES: list[dict] = [
    # CN4
    {"secid": "601058.SH", "code": "601058", "name": "赛轮集团", "market": "A",
     "asset_type": "STOCK", "industry": "汽车零部件·轮胎", "sina_symbol": "sh601058",
     "ftype": None, "settle": _STOCK_SETTLE, "cost": _STOCK_COST},
    {"secid": "002014.SZ", "code": "002014", "name": "永新股份", "market": "A",
     "asset_type": "STOCK", "industry": "塑料软包装", "sina_symbol": "sz002014",
     "ftype": None, "settle": _STOCK_SETTLE, "cost": _STOCK_COST},
    {"secid": "600660.SH", "code": "600660", "name": "福耀玻璃", "market": "A",
     "asset_type": "STOCK", "industry": "汽车零部件·汽车玻璃", "sina_symbol": "sh600660",
     "ftype": None, "settle": _STOCK_SETTLE, "cost": _STOCK_COST},
    {"secid": "002027.SZ", "code": "002027", "name": "分众传媒", "market": "A",
     "asset_type": "STOCK", "industry": "广告传媒", "sina_symbol": "sz002027",
     "ftype": None, "settle": _STOCK_SETTLE, "cost": _STOCK_COST},
    # MULTI5
    {"secid": "510300.SH", "code": "510300", "name": "沪深300ETF", "market": "A",
     "asset_type": "ETF", "industry": "A股宽基指数", "sina_symbol": "sh510300",
     "ftype": "etf", "settle": "T+1", "cost": _ETF_COST},
    {"secid": "501018.SH", "code": "501018", "name": "南方原油LOF", "market": "A",
     "asset_type": "LOF", "industry": "原油(QDII)", "sina_symbol": "sh501018",
     "ftype": "lof", "settle": "T+0(跨境LOF)", "cost": _LOF_COST},
    {"secid": "518880.SH", "code": "518880", "name": "黄金ETF", "market": "A",
     "asset_type": "ETF", "industry": "黄金", "sina_symbol": "sh518880",
     "ftype": "etf", "settle": "T+0", "cost": _ETF_COST},
    {"secid": "513100.SH", "code": "513100", "name": "纳指ETF", "market": "A",
     "asset_type": "ETF", "industry": "美股指数(QDII-纳指100)", "sina_symbol": "sh513100",
     "ftype": "etf", "settle": "T+0(跨境)", "cost": _ETF_COST},
    {"secid": "511260.SH", "code": "511260", "name": "十年国债ETF", "market": "A",
     "asset_type": "ETF", "industry": "国债", "sina_symbol": "sh511260",
     "ftype": "etf", "settle": "T+0", "cost": _ETF_COST},
]

# ── 两组合（name/note 逐字对齐 α portfolio/*.json）────────────
PORTFOLIOS: list[dict] = [
    {
        "pid": "cn4",
        "name": "cn4 · 赛轮/永新/福耀/分众",
        "note": "用户指定四标的（永新=永新股份002014.SZ）；未提供权重 → 等权名义组合 25%×4",
        "base_ccy": "CNY",
        "positions": [
            ("601058.SH", Decimal("25.000")),
            ("002014.SZ", Decimal("25.000")),
            ("600660.SH", Decimal("25.000")),
            ("002027.SZ", Decimal("25.000")),
        ],
    },
    {
        "pid": "multi5",
        "name": "multi5 · 沪深300/南方原油/黄金/纳指/十年国债",
        "note": "五资产等权名义组合 20%×5（A股宽基/原油/黄金/美股/国债）；全部 A 股上市、"
                "人民币计价（QDII 为人民币份额，无直接汇率敞口，存在场内溢价/折价风险）；"
                "A股无纳斯达克综合指数ETF，以纳指100ETF(513100)近似",
        "base_ccy": "CNY",
        "positions": [
            ("510300.SH", Decimal("20.000")),
            ("501018.SH", Decimal("20.000")),
            ("518880.SH", Decimal("20.000")),
            ("513100.SH", Decimal("20.000")),
            ("511260.SH", Decimal("20.000")),
        ],
    },
]


def _upsert(session, model, rows: list[dict], conflict_cols: list[str],
            update_cols: list[str]) -> tuple[int, int]:
    """按自然键 upsert；返回 ``(inserted, updated)`` 计数。"""
    if not rows:
        return (0, 0)
    existing = {
        tuple(row)
        for row in session.execute(
            select(*[getattr(model, col) for col in conflict_cols])
        ).all()
    }
    keys = [tuple(row[col] for col in conflict_cols) for row in rows]
    inserted = sum(1 for key in keys if key not in existing)
    updated = len(keys) - inserted

    stmt = pg_insert(model).values(rows)
    if update_cols:
        stmt = stmt.on_conflict_do_update(
            index_elements=conflict_cols,
            set_={col: stmt.excluded[col] for col in update_cols},
        )
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=conflict_cols)
    session.execute(stmt)
    return (inserted, updated)


def run_seed() -> dict[str, tuple[int, int]]:
    """执行幂等灌数，返回各表 ``(inserted, updated)`` 统计。"""
    stats: dict[str, tuple[int, int]] = {}
    with session_scope() as session:
        # users：do_nothing（argon2 每次盐不同，保持已有行不变以稳定）
        stats["users"] = _upsert(
            session,
            User,
            [{"username": DEMO_USERNAME,
              "password_hash": _pwd_context.hash(DEMO_PASSWORD),
              "role": DEMO_ROLE}],
            ["username"],
            [],
        )
        # securities
        sec_rows = [{**row, "source": "seed"} for row in SECURITIES]
        stats["securities"] = _upsert(
            session, Security, sec_rows, ["secid"],
            ["code", "name", "market", "asset_type", "industry",
             "sina_symbol", "ftype", "settle", "cost", "source"],
        )
        # portfolios
        pf_rows = [{k: pf[k] for k in ("pid", "name", "note", "base_ccy")} for pf in PORTFOLIOS]
        stats["portfolios"] = _upsert(
            session, Portfolio, pf_rows, ["pid"], ["name", "note", "base_ccy"],
        )
        session.flush()
        # positions（先解析 portfolio_id）
        pid_to_id = {
            pid: pid_
            for pid, pid_ in session.execute(select(Portfolio.pid, Portfolio.id)).all()
        }
        pos_rows = [
            {"portfolio_id": pid_to_id[pf["pid"]], "secid": secid, "weight": weight}
            for pf in PORTFOLIOS
            for secid, weight in pf["positions"]
        ]
        stats["positions"] = _upsert(
            session, Position, pos_rows, ["portfolio_id", "secid"], ["weight"],
        )
    return stats


def main() -> int:
    """命令行入口：打印统计；失败非 0 退出。"""
    try:
        stats = run_seed()
    except Exception as exc:  # noqa: BLE001 - 入口需捕获全部异常并返回非 0
        print(f"❌ seed 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("✅ seed 完成（幂等，可重复执行）：")
    for table, (inserted, updated) in stats.items():
        print(f"  - {table:<11} 新增 {inserted} / 更新 {updated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

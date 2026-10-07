"""实时行情端点（V4-β 多市场）。

- ``GET /market/quotes?secids=00700.HK,AAPL.US,601058.SH``：A股/港股/美股混合批量
  实时行情（新浪 hq 批量单请求为主源、腾讯逐标的兜底；数据获取见
  ``applib.datasource.fetch_realtime_quotes``），附港元/美元兑人民币汇率
  （``fetch_fx``，港美持仓折算人民币口径用）。
- ``GET /market/ggt``：港股通标的名单（雪球在线 → 静态快照降级；供前端标记展示）。

设计要点：
- **实时性**：新浪 hq 为交易所快照（A股 3-5s、港股/美股盘中实时），腾讯兜底；
- **稳定性**：applib 层内置重试/降级；本层再加 Redis 短缓存（``QUOTE_CACHE_TTL``，
  默认 5s）合并突发轮询，Redis 不可达 fail-open 直拉；
- **币种**：响应带 ``currency``（CNY/HKD/USD）与 ``price_cny``（按汇率折算，A股原值）；
- 未知 secid / 双源均失败 → 该条目带 ``error`` 字段，不影响其余标的。

本模块定义模块级 ``router``，由 ``app/api/v1/__init__.py`` 自动发现注册；
**不要修改该聚合文件**。
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError, ErrorCode
from app.db.redis import get_redis
from app.db.session import get_db
from app.models import Security

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/market", tags=["market"])

DbDep = Annotated[Session, Depends(get_db)]

# 单次批量上限（新浪批量接口单请求即可承载；上限防滥用）
MAX_QUOTES = 50
# Redis 缓存 TTL（秒）：合并 5s 内同参突发轮询，减轻上游压力
QUOTE_CACHE_TTL = 5
_CACHE_PREFIX = "ap:quotes:v1:"

MARKET_LABEL = {"A": "A股", "HK": "港股", "US": "美股"}


def _classify(secid: str) -> str | None:
    """secid 后缀 → 市场（与 applib.datasource.classify_secid 同口径）。"""
    try:
        from applib.datasource import classify_secid

        return classify_secid(secid)
    except Exception:  # noqa: BLE001 - applib 不可用时本地判定
        s = (secid or "").strip().upper()
        if s.endswith((".SH", ".SZ")):
            return "A"
        if s.endswith(".HK"):
            return "HK"
        if s.endswith(".US"):
            return "US"
        return None


def _cache_key(secids: list[str]) -> str:
    raw = ",".join(sorted(secids))
    return _CACHE_PREFIX + hashlib.sha256(raw.encode()).hexdigest()[:24]


def _fetch_quotes_uncached(secids: list[str]) -> dict[str, Any]:
    """无缓存取数：applib 实时行情 + PG 元数据富化 + 汇率折算。"""
    from applib.datasource import fetch_fx, fetch_realtime_quotes

    quotes = fetch_realtime_quotes(secids)
    fx = fetch_fx()

    with_next = select(Security).where(Security.secid.in_(secids))
    from app.db.session import session_scope

    with session_scope() as db:
        secmap = {s.secid: s for s in db.execute(with_next).scalars().all()}

    items: list[dict[str, Any]] = []
    for secid in secids:
        q = quotes.get(secid)
        meta = secmap.get(secid)
        if q is None:
            items.append({"secid": secid, "name": meta.name if meta else secid,
                          "market": _classify(secid), "error": "unavailable"})
            continue
        market = meta.market if meta else (_classify(secid) or "A")
        currency = (meta.currency if meta and meta.currency else None) or q.get("currency")
        price = q.get("price")
        price_cny = None
        if price is not None:
            if currency == "HKD":
                price_cny = round(price * fx["HKD_CNY"], 4) if fx.get("HKD_CNY") else None
            elif currency == "USD":
                price_cny = round(price * fx["USD_CNY"], 4) if fx.get("USD_CNY") else None
            else:
                price_cny = price
        items.append({
            "secid": secid,
            "name": q.get("name") or (meta.name if meta else secid),
            "market": market,
            "market_label": MARKET_LABEL.get(market, market),
            "currency": currency,
            "price": price,
            "price_cny": price_cny,
            "prev_close": q.get("prev_close"),
            "open": q.get("open"),
            "high": q.get("high"),
            "low": q.get("low"),
            "chg": q.get("chg"),
            "pct": q.get("pct"),
            "volume": q.get("volume"),
            "amount": q.get("amount"),
            "time": q.get("time"),
            "source": q.get("source"),
            "hk_connect": meta.hk_connect if meta else None,
            "asset_type": meta.asset_type if meta else None,
        })
    return {"quotes": items, "fx": fx, "cached": False}


@router.get("/quotes")
def get_quotes(
    secids: str = Query(description="逗号分隔的 secid 列表（A股/港股/美股混合）"),
) -> dict[str, Any]:
    """批量实时行情 + 汇率（Redis 5s 缓存；上游双源，失败条目带 error 不阻断整体）。"""
    id_list = [s.strip().upper() for s in secids.split(",") if s.strip()]
    if not id_list:
        raise AppError(ErrorCode.VALIDATION_ERROR, "secids 不能为空", detail={"field": "secids"})
    if len(id_list) > MAX_QUOTES:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"单次最多 {MAX_QUOTES} 个标的",
            detail={"field": "secids", "max": MAX_QUOTES, "got": len(id_list)},
        )
    unknown = [s for s in id_list if _classify(s) is None]
    if unknown:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"无法识别的 secid：{unknown[:3]}（需 .SH/.SZ/.HK/.US 后缀）",
            detail={"field": "secids", "unknown": unknown[:10]},
        )

    key = _cache_key(id_list)
    try:
        cached = get_redis().get(key)
        if cached:
            payload = json.loads(cached)
            payload["cached"] = True
            return payload
    except Exception as exc:  # noqa: BLE001 - Redis 不可达 fail-open
        logger.warning("行情缓存读取失败（fail-open 直拉）：%s", type(exc).__name__)

    payload = _fetch_quotes_uncached(id_list)
    try:
        get_redis().set(key, json.dumps(payload, ensure_ascii=False), ex=QUOTE_CACHE_TTL)
    except Exception as exc:  # noqa: BLE001 - 缓存写失败不影响响应
        logger.warning("行情缓存写入失败（fail-open）：%s", type(exc).__name__)
    return payload


@router.get("/ggt")
def get_ggt() -> dict[str, Any]:
    """港股通标的名单（代码 5 位字符串；雪球在线 → 静态快照降级）。"""
    from applib.datasource import fetch_ggt_components

    codes, source = fetch_ggt_components()
    return {"codes": codes, "count": len(codes), "source": source}

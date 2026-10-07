"""标的注册与检索端点（T1.3，§6.2；V4-β 多市场扩展）。

- ``GET /securities/search?q=&limit=20``：本地 PG 前缀匹配 ``code`` / ``name`` / ``secid``；
  空 ``q`` 返回最近 ``limit``（默认 20）条。
- ``POST /securities/register``：``{code, market?, asset_type?, ftype?, name?, industry?}`` →
  代码规范化与市场判定（A股 6 位 / 港股 ≤5 位数字 / 美股 1-6 位字母）→ 可达性探测
  （AKShare 新浪白名单接口，港股/美股走对应新浪接口）→ 港股通标记（雪球名单，降级静态
  快照）→ 落 ``securities`` 表 + 审计；已存在 ``secid`` 幂等返回既有记录。

本模块定义模块级 ``router``，由 ``app/api/v1/__init__.py`` 自动发现注册；
**不要修改该聚合文件**。

网络约束（α 坑 C1）：可达性探测仅允许新浪系接口
``stock_zh_a_daily``（A股股票）/ ``fund_etf_hist_sina``（A股 ETF/LOF）/
``stock_hk_daily``（港股）/ ``stock_us_daily``（美股），禁用东财系接口。
akshare 为可选运行期依赖，故在探测函数内惰性导入，
使测试可 monkeypatch 该函数而完全不联网。

V4-β 多市场口径：
- A股：6 位数字（60/68→SH，00/30→SZ；5 开头→SH 基金，1 开头→SZ 基金）；
- 港股：1-5 位数字 → 补零至 5 位（``700`` → ``00700.HK``），``sina_symbol=hk00700``；
- 美股：1-6 位字母（``AAPL`` → ``AAPL.US``），``sina_symbol=gb_aapl``（暂不支持带点
  的多类别股份代码，如 BRK.B）。
``market`` 入参可显式指定（``A``/``HK``/``US``），缺省按代码形态自动判定。
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.errors import AppError, ErrorCode
from app.db.session import get_db
from app.models import AuditLog, Security

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/securities", tags=["securities"])

DbDep = Annotated[Session, Depends(get_db)]

# 探测取样根数（§9.2 T1.3：试拉约 10 根 K 线验证可达）
PROBE_BARS = 10
# 股票探测回看窗口（自然日），足够覆盖最近交易日
PROBE_LOOKBACK_DAYS = 45

_CODE_RE = re.compile(r"^\d{6}$")
_HK_CODE_RE = re.compile(r"^\d{1,5}$")
_US_CODE_RE = re.compile(r"^[A-Z]{1,6}$")
_ALLOWED_ASSET_TYPES = frozenset({"STOCK", "ETF", "LOF"})
_ALLOWED_MARKETS = frozenset({"A", "HK", "US"})

# asset_type → (settle 缺省, cost 缺省)；LOF settle 未在任务口径中约定，保持 None
_SETTLE_STOCK = "A股 T+1"
_COST_STOCK = "印花税卖出0.05%+佣金"
_SETTLE_ETF = "T+1"
_COST_ETF = "ETF 免印花税·佣金约万2.5"
_COST_LOF = "LOF 免印花税·佣金约万2.5"
# V4-β 多市场缺省
_MARKET_DEFAULTS = {
    "HK": ("港股 T+0（港股通人民币结算）",
           "印花税0.1%+交易征费+汇兑价差；港股通股息税20%（红筹28%）"),
    "US": ("美股 T+0", "券商佣金（免印花税）+ 换汇点差；股息预扣税10%（W-8BEN）"),
}
_MARKET_CCY = {"A": "CNY", "HK": "HKD", "US": "USD"}


# ── Pydantic 模型（就地定义，§6.2 契约）────────────────────────


class RegisterIn(BaseModel):
    """注册入参；仅 ``code`` 必填，其余可选以免破坏 ``{code}`` 契约。

    ``market``：显式市场（A/HK/US）；缺省按代码形态判定（6 位数字→A，≤5 位数字→HK，
    字母→US）。港股短代码（如 ``700``）自动补零至 5 位。
    """

    code: str = Field(min_length=1)
    market: str | None = None
    asset_type: str | None = None
    ftype: str | None = None
    name: str | None = None
    industry: str | None = None


class SecurityOut(BaseModel):
    """标的响应体（securities 表可见字段）。"""

    model_config = ConfigDict(from_attributes=True)

    secid: str
    code: str
    name: str
    market: str
    asset_type: str
    industry: str | None = None
    sina_symbol: str
    ftype: str | None = None
    settle: str | None = None
    cost: str | None = None
    currency: str | None = None
    hk_connect: bool | None = None
    source: str


# ── 代码规范化与市场判定（纯函数，便于单测）────────────────────


def _normalize_a_code(raw: str) -> tuple[str, str, bool]:
    """A股代码 → ``(code, secid, is_fund)``；格式非法抛 422。"""
    s = (raw or "").strip().lower()
    if s.startswith(("sh", "sz")):
        s = s[2:]
    if s.endswith((".sh", ".sz")):
        s = s[:-3]
    s = s.strip()
    if not _CODE_RE.match(s):
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"无法识别的 A 股代码：{raw!r}（需 6 位数字）",
            detail={"code": raw, "market": "A"},
        )
    two = s[:2]
    if two in {"60", "68"}:
        prefix = "sh"
    elif two in {"00", "30"}:
        prefix = "sz"
    elif s[0] == "5":
        prefix = "sh"
    elif s[0] == "1":
        prefix = "sz"
    else:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"无法识别市场前缀：{s}",
            detail={"code": raw, "market": "A"},
        )
    is_fund = s[0] in {"5", "1"}
    return s, f"{s}.{prefix.upper()}", is_fund


def _normalize_hk_code(raw: str) -> tuple[str, str]:
    """港股代码 → ``(code(5位), secid)``；格式非法抛 422。"""
    s = (raw or "").strip().lower().removeprefix("hk").removesuffix(".hk").strip()
    if not _HK_CODE_RE.match(s):
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"无法识别的港股代码：{raw!r}（需 1-5 位数字）",
            detail={"code": raw, "market": "HK"},
        )
    code = s.zfill(5)
    return code, f"{code}.HK"


def _normalize_us_code(raw: str) -> tuple[str, str]:
    """美股代码 → ``(TICKER, secid)``；格式非法抛 422（暂不支持 BRK.B 类带点代码）。"""
    s = (raw or "").strip().upper().removeprefix("US.").removesuffix(".US").strip()
    if not _US_CODE_RE.match(s):
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"无法识别的美股代码：{raw!r}（需 1-6 位字母；暂不支持带点的多类别代码）",
            detail={"code": raw, "market": "US"},
        )
    return s, f"{s}.US"


def _resolve_secid(raw: str, market: str | None = None) -> tuple[str, str, str, bool, str]:
    """规范化代码并派生 ``(code, secid, sina_symbol, is_fund, market)``。

    market 缺省按代码形态判定；显式指定时按对应市场规范化。
    """
    mkt = (market or "").strip().upper() or None
    if mkt and mkt not in _ALLOWED_MARKETS:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"不支持的 market：{market!r}（允许 A/HK/US）",
            detail={"market": market},
        )
    s_raw = (raw or "").strip()
    if mkt is None:
        s_low = s_raw.lower()
        a_probe = (s_low.removeprefix("sh").removeprefix("sz")
                   .removesuffix(".sh").removesuffix(".sz"))
        hk_probe = s_low.removeprefix("hk").removesuffix(".hk")
        us_probe = s_raw.upper().removesuffix(".US").removeprefix("US.")
        if _CODE_RE.match(a_probe) and len(s_raw) <= 9:
            mkt = "A"
        elif _HK_CODE_RE.match(hk_probe):
            mkt = "HK"
        elif _US_CODE_RE.match(us_probe):
            mkt = "US"
        else:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                f"无法识别的标的代码：{raw!r}（A股 6 位数字 / 港股 1-5 位数字 / 美股字母，"
                "或显式传 market=A|HK|US）",
                detail={"code": raw},
            )
    if mkt == "A":
        code, secid, is_fund = _normalize_a_code(s_raw)
        return code, secid, f"{secid.split('.')[1].lower()}{code}", is_fund, "A"
    if mkt == "HK":
        code, secid = _normalize_hk_code(s_raw)
        return code, secid, f"hk{code}", False, "HK"
    code, secid = _normalize_us_code(s_raw)
    return code, secid, f"gb_{code.lower()}", False, "US"


# ── 名称解析（applib 已知映射 + 实时行情，解析不到回退 code）──


@lru_cache(maxsize=1)
def _known_meta() -> dict[str, dict]:
    """applib ``CN4``/``MULTI5`` 已知标的映射（secid → 元数据）。

    映射不可导入（如未注入 PYTHONPATH）时降级为空。
    """
    meta: dict[str, dict] = {}
    try:
        from applib.datasource import CN4, MULTI5

        for item in [*CN4, *MULTI5]:
            meta[item["secid"]] = item
    except Exception as exc:  # noqa: BLE001 - applib 不可用即降级，不阻断注册
        logger.debug("applib 已知映射不可用，name 将回退为 code：%s", type(exc).__name__)
    return meta


def _realtime_names(secids: list[str]) -> dict[str, str]:
    """批量取实时行情中文名（applib 新浪批量接口；失败返回空，不阻断）。"""
    try:
        from applib.datasource import fetch_realtime_quotes

        return {s: q.get("name") for s, q in fetch_realtime_quotes(secids).items()
                if q.get("name")}
    except Exception as exc:  # noqa: BLE001 - 名称解析尽力而为
        logger.warning("实时名称解析失败（回退已知映射/code）：%s", type(exc).__name__)
        return {}


def _resolve_name(secid: str, explicit: str | None, realtime: dict[str, str]) -> str:
    """名称优先级：显式入参 → 实时行情 → applib 已知映射 → 回退 code（不伪造）。"""
    if explicit:
        return explicit
    if realtime.get(secid):
        return realtime[secid]
    known = _known_meta().get(secid)
    if known and known.get("name"):
        return str(known["name"])
    return secid.split(".", 1)[0]


# ── 港股通标记（雪球名单 → 静态快照降级；进程内缓存 1 小时）────


_GGT_CACHE_TTL_SECONDS = 3600


def _fetch_ggt_codes() -> set[str]:
    """港股通标的代码集合（雪球在线 → 静态快照；失败返回快照，永不抛异常）。"""
    try:
        from applib.datasource import fetch_ggt_components

        codes, _source = fetch_ggt_components()
        return set(codes)
    except Exception as exc:  # noqa: BLE001 - applib 不可用即降级
        logger.warning("港股通名单拉取失败（降级静态快照）：%s", type(exc).__name__)
        try:
            from applib.ggt_static import GGT_STATIC

            return set(GGT_STATIC)
        except Exception:  # noqa: BLE001 - 完全无名单 → 空集（hk_connect 记 False 并依赖后续同步刷新）
            return set()


# ── 可达性探测（AKShare 新浪白名单；模块级独立函数，便于 monkeypatch）──


def _probe_reachable(sina_symbol: str, market: str, is_fund: bool) -> list:
    """拉取最近约 10 根 K 线验证标的可达；返回 K 线记录列表（不可达时为空）。

    A股股票：新浪 ``stock_zh_a_daily``；A股基金（ETF/LOF）：新浪 ``fund_etf_hist_sina``；
    港股：新浪 ``stock_hk_daily``；美股：新浪 ``stock_us_daily``。
    异常向上抛出，由调用方统一转 422。
    """
    import akshare as ak

    if market == "HK":
        df = ak.stock_hk_daily(symbol=sina_symbol.removeprefix("hk"), adjust="")
    elif market == "US":
        df = ak.stock_us_daily(symbol=sina_symbol.removeprefix("gb_").upper(), adjust="")
    elif is_fund:
        df = ak.fund_etf_hist_sina(symbol=sina_symbol)
    else:
        end = date.today()
        start = end - timedelta(days=PROBE_LOOKBACK_DAYS)
        df = ak.stock_zh_a_daily(
            symbol=sina_symbol,
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            adjust="",
        )
    if df is None or len(df) == 0:
        return []
    return df.tail(PROBE_BARS).to_dict("records")


def _assert_reachable(code: str, sina_symbol: str, market: str, is_fund: bool) -> None:
    """探测失败/无 K 线 → 422 ``SECURITY_NOT_FOUND``（代码格式合法但上游不可达）。"""
    try:
        bars = _probe_reachable(sina_symbol, market, is_fund)
    except Exception as exc:  # noqa: BLE001 - 上游异常统一转业务错误
        logger.warning("标的 %s 可达性探测异常：%s", code, type(exc).__name__)
        raise AppError(
            ErrorCode.SECURITY_NOT_FOUND,
            f"标的不可达或代码无效：{code}",
            detail={"code": code, "sina_symbol": sina_symbol, "reason": type(exc).__name__},
        ) from exc
    if not bars:
        raise AppError(
            ErrorCode.SECURITY_NOT_FOUND,
            f"标的不可达或代码无效：{code}",
            detail={"code": code, "sina_symbol": sina_symbol, "reason": "empty_bars"},
        )


def _defaults(asset_type: str, market: str) -> tuple[str | None, str | None]:
    """按 asset_type/market 返回 ``(settle, cost)`` 缺省值（§9.2 T1.3 / V4-β）。"""
    if market in _MARKET_DEFAULTS:
        return _MARKET_DEFAULTS[market]
    if asset_type == "STOCK":
        return _SETTLE_STOCK, _COST_STOCK
    if asset_type == "LOF":
        return None, _COST_LOF
    return _SETTLE_ETF, _COST_ETF


def _resolve_asset_type(raw: str | None, is_fund: bool, market: str) -> str:
    """显式 ``asset_type`` 优先（大写归一）；港股/美股仅支持 STOCK。"""
    if raw is None:
        if market in ("HK", "US"):
            return "STOCK"
        return "ETF" if is_fund else "STOCK"
    asset_type = raw.strip().upper()
    if market in ("HK", "US") and asset_type != "STOCK":
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"港股/美股仅支持 asset_type=STOCK：{raw!r}",
            detail={"asset_type": raw, "market": market},
        )
    if asset_type not in _ALLOWED_ASSET_TYPES:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"不支持的 asset_type：{raw!r}（允许 STOCK/ETF/LOF）",
            detail={"asset_type": raw},
        )
    return asset_type


# ── 端点 ────────────────────────────────────────────────────────


@router.get("/search", response_model=list[SecurityOut])
def search_securities(
    db: DbDep,
    q: str = Query(default="", description="代码/名称/secid 前缀；空则返回最近 20 条"),
    limit: int = Query(default=20, ge=1, le=100),
    market: str | None = Query(default=None, description="按市场过滤：A/HK/US"),
) -> list[SecurityOut]:
    """本地 PG 前缀检索：``code``/``secid`` 用 ``LIKE``，中文 ``name`` 用 ``ILIKE``。"""
    keyword = (q or "").strip()
    stmt = select(Security)
    if market:
        stmt = stmt.where(Security.market == market.strip().upper())
    if keyword:
        like = f"{keyword}%"
        stmt = stmt.where(
            or_(
                Security.code.like(like),
                Security.secid.like(like),
                Security.name.ilike(like),
            )
        )
    stmt = stmt.order_by(Security.created_at.desc()).limit(limit)
    rows = db.execute(stmt).scalars().all()
    return [SecurityOut.model_validate(row) for row in rows]


@router.post("/register", response_model=SecurityOut)
def register_security(payload: RegisterIn, db: DbDep) -> SecurityOut:
    """注册标的：规范化 → 幂等检查 → 探测可达 → 港股通标记 → 落库 + 审计。"""
    code, secid, sina_symbol, is_fund, market = _resolve_secid(payload.code, payload.market)

    # 幂等：已存在则直接返回既有记录（不探测、不写审计、不重复插入）
    existing = db.get(Security, secid)
    if existing is not None:
        return SecurityOut.model_validate(existing)

    asset_type = _resolve_asset_type(payload.asset_type, is_fund, market)
    _assert_reachable(code, sina_symbol, market, is_fund)

    # 港股通标记：仅港股有值（True/False）；A股/美股恒 None
    hk_connect: bool | None = None
    if market == "HK":
        hk_connect = code in _fetch_ggt_codes()

    settle, cost = _defaults(asset_type, market)
    security = Security(
        secid=secid,
        code=code,
        name=_resolve_name(secid, payload.name, _realtime_names([secid])),
        market=market,
        asset_type=asset_type,
        industry=payload.industry,
        sina_symbol=sina_symbol,
        ftype=payload.ftype,
        settle=settle,
        cost=cost,
        currency=_MARKET_CCY[market],
        hk_connect=hk_connect,
        source="user",
    )
    db.add(security)
    db.add(
        AuditLog(
            entity="security",
            entity_id=secid,
            action="create",
            actor="system",
            payload={"code": code, "market": market, "asset_type": asset_type,
                     "ftype": payload.ftype, "hk_connect": hk_connect, "source": "user"},
        )
    )
    db.commit()
    return SecurityOut.model_validate(security)

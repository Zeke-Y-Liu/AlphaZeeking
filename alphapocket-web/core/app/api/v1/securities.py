"""标的注册与检索端点（T1.3，§6.2）。

- ``GET /securities/search?q=&limit=20``：本地 PG 前缀匹配 ``code`` / ``name`` / ``secid``；
  空 ``q`` 返回最近 ``limit``（默认 20）条。
- ``POST /securities/register``：``{code, asset_type?, ftype?, name?, industry?}`` →
  规范化代码 → 判定市场前缀 → 可达性探测（AKShare 新浪白名单接口）→ 落 ``securities``
  表 + 审计；已存在 ``secid`` 幂等返回既有记录。

本模块定义模块级 ``router``，由 ``app/api/v1/__init__.py`` 自动发现注册；
**不要修改该聚合文件**。

网络约束（α 坑 C1）：可达性探测仅允许新浪白名单接口
``stock_zh_a_daily``（股票）/ ``fund_etf_hist_sina``（ETF/LOF），禁用东财系接口。
akshare 为可选运行期依赖，故在 :func:`_probe_reachable` 内惰性导入，
使测试可 monkeypatch 该函数而完全不联网。
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
_ALLOWED_ASSET_TYPES = frozenset({"STOCK", "ETF", "LOF"})

# asset_type → (settle 缺省, cost 缺省)；LOF settle 未在任务口径中约定，保持 None
_SETTLE_STOCK = "A股 T+1"
_COST_STOCK = "印花税卖出0.05%+佣金"
_SETTLE_ETF = "T+1"
_COST_ETF = "ETF 免印花税·佣金约万2.5"
_COST_LOF = "LOF 免印花税·佣金约万2.5"


# ── Pydantic 模型（就地定义，§6.2 契约）────────────────────────


class RegisterIn(BaseModel):
    """注册入参；仅 ``code`` 必填，其余可选以免破坏 ``{code}`` 契约。"""

    code: str = Field(min_length=1)
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
    source: str


# ── 代码规范化与市场前缀判定（纯函数，便于单测）────────────────


def _normalize_code(raw: str) -> str:
    """去除 ``sh``/``sz`` 前缀与 ``.SH``/``.SZ`` 后缀，取 6 位数字；否则 422。"""
    s = (raw or "").strip().lower()
    if s.startswith(("sh", "sz")):
        s = s[2:]
    if s.endswith((".sh", ".sz")):
        s = s[:-3]
    s = s.strip()
    if not _CODE_RE.match(s):
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"无法识别的标的代码：{raw!r}（需 6 位数字，可带 sh/sz 前缀或 .SH/.SZ 后缀）",
            detail={"code": raw},
        )
    return s


def _market_prefix(code: str) -> tuple[str, bool]:
    """代码 → ``(市场前缀 sh|sz, 是否基金)``（冻结规则，§9.2 T1.3）。

    6 位 A 股：``60``/``68``→sh、``00``/``30``→sz；基金：``5`` 开头→sh、``1`` 开头→sz。
    """
    two = code[:2]
    if two in {"60", "68"}:
        return "sh", False
    if two in {"00", "30"}:
        return "sz", False
    if code[0] == "5":
        return "sh", True
    if code[0] == "1":
        return "sz", True
    raise AppError(
        ErrorCode.VALIDATION_ERROR,
        f"无法识别市场前缀：{code}",
        detail={"code": code},
    )


def _resolve_secid(raw: str) -> tuple[str, str, str, bool]:
    """规范化代码并派生 ``(code, secid, sina_symbol, is_fund)``。"""
    code = _normalize_code(raw)
    prefix, is_fund = _market_prefix(code)
    return code, f"{code}.{prefix.upper()}", f"{prefix}{code}", is_fund


# ── 名称解析（applib 已知映射，解析不到回退 code）──────────────


@lru_cache(maxsize=1)
def _known_meta() -> dict[str, dict]:
    """applib ``CN4``/``MULTI5`` 已知标的映射（secid → 元数据）。

    α 坑 C1 下新增标的多无本地名称来源，此处仅覆盖 applib 已定义标的；
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


def _resolve_name(secid: str, explicit: str | None) -> str:
    """名称优先级：显式入参 → applib 已知映射 → 回退 code 字符串（不伪造）。"""
    if explicit:
        return explicit
    known = _known_meta().get(secid)
    if known and known.get("name"):
        return str(known["name"])
    return secid.split(".", 1)[0]


# ── 可达性探测（AKShare 新浪白名单；模块级独立函数，便于 monkeypatch）──


def _probe_reachable(sina_symbol: str, is_fund: bool) -> list:
    """拉取最近约 10 根 K 线验证标的可达；返回 K 线记录列表（不可达时为空）。

    股票：新浪 ``stock_zh_a_daily``；基金（ETF/LOF）：新浪 ``fund_etf_hist_sina``。
    异常向上抛出，由调用方统一转 422。
    """
    import akshare as ak

    if is_fund:
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


def _assert_reachable(code: str, sina_symbol: str, is_fund: bool) -> None:
    """探测失败/无 K 线 → 422 ``SECURITY_NOT_FOUND``（代码格式合法但上游不可达）。"""
    try:
        bars = _probe_reachable(sina_symbol, is_fund)
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


def _defaults(asset_type: str) -> tuple[str | None, str | None]:
    """按 asset_type 返回 ``(settle, cost)`` 缺省值（§9.2 T1.3）。"""
    if asset_type == "STOCK":
        return _SETTLE_STOCK, _COST_STOCK
    if asset_type == "LOF":
        return None, _COST_LOF
    return _SETTLE_ETF, _COST_ETF


def _resolve_asset_type(raw: str | None, is_fund: bool) -> str:
    """显式 ``asset_type`` 优先（大写归一）；否则基金默认 ETF、股票默认 STOCK。"""
    if raw is None:
        return "ETF" if is_fund else "STOCK"
    asset_type = raw.strip().upper()
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
) -> list[SecurityOut]:
    """本地 PG 前缀检索：``code``/``secid`` 用 ``LIKE``，中文 ``name`` 用 ``ILIKE``。"""
    keyword = (q or "").strip()
    stmt = select(Security)
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
    """注册标的：规范化 → 幂等检查 → 探测可达 → 落库 + 审计（§6.2/T1.3）。"""
    code, secid, sina_symbol, is_fund = _resolve_secid(payload.code)

    # 幂等：已存在则直接返回既有记录（不探测、不写审计、不重复插入）
    existing = db.get(Security, secid)
    if existing is not None:
        return SecurityOut.model_validate(existing)

    asset_type = _resolve_asset_type(payload.asset_type, is_fund)
    _assert_reachable(code, sina_symbol, is_fund)

    settle, cost = _defaults(asset_type)
    security = Security(
        secid=secid,
        code=code,
        name=_resolve_name(secid, payload.name),
        market="A",
        asset_type=asset_type,
        industry=payload.industry,
        sina_symbol=sina_symbol,
        ftype=payload.ftype,
        settle=settle,
        cost=cost,
        source="user",
    )
    db.add(security)
    db.add(
        AuditLog(
            entity="security",
            entity_id=secid,
            action="create",
            actor="system",
            payload={"code": code, "asset_type": asset_type,
                     "ftype": payload.ftype, "source": "user"},
        )
    )
    db.commit()
    return SecurityOut.model_validate(security)

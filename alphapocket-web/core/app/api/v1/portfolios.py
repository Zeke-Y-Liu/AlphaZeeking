"""组合 CRUD 与物化端点（T2.2，§6.2）。

- ``GET    /api/v1/portfolios``         组合列表（含持仓数 + 最近 run 摘要，仅 active）
- ``POST   /api/v1/portfolios``         新建（校验 → 写表 → 物化 JSON → 审计）
- ``GET    /api/v1/portfolios/{pid}``   详情（含 positions）
- ``PUT    /api/v1/portfolios/{pid}``   更新（重跑校验 + 重新物化 + 审计）
- ``DELETE /api/v1/portfolios/{pid}``   归档软删（status=archived + 审计；不存在→404）

路径参数 ``{pid}`` 为组合短码（如 ``multi5`` / ``pf_3``），与物化文件
``portfolio/{pid}.json`` 的 ``id`` 一致。本模块定义模块级 ``router``，由
``app/api/v1/__init__.py`` 自动发现注册；**不要修改该聚合文件**。
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1.data import RunnerDep
from app.core.config import Settings, get_settings
from app.core.errors import AppError, ErrorCode
from app.db.session import get_db
from app.models import Portfolio, Position, Security
from app.services import portfolios as svc
from app.services import review as review_svc
from app.services import verification

router = APIRouter(prefix="/portfolios", tags=["portfolios"])

DbDep = Annotated[Session, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


# ── 请求 / 响应模型（在本模块内定义，禁止改 app/schemas/）────────


class PositionIn(BaseModel):
    """持仓入参；``equal_weight=true`` 时 ``weight`` 可省略（被忽略）。"""

    secid: str
    weight: float | None = None


class PortfolioCreate(BaseModel):
    name: str = Field(min_length=1)
    pid: str | None = None
    note: str | None = None
    positions: list[PositionIn] = Field(min_length=1)
    equal_weight: bool = False
    strategy: dict[str, Any] | None = None


class PortfolioUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    note: str | None = None
    positions: list[PositionIn] | None = Field(default=None, min_length=1)
    equal_weight: bool = False
    strategy: dict[str, Any] | None = None


class PositionOut(BaseModel):
    secid: str
    weight: float
    name: str
    market: str | None = None
    asset_type: str
    currency: str | None = None
    hk_connect: bool | None = None
    settle: str | None = None
    cost: str | None = None


class PortfolioOut(BaseModel):
    id: int
    pid: str
    name: str
    note: str | None = None
    base_ccy: str
    strategy: dict[str, Any]
    status: str
    created_at: datetime
    updated_at: datetime
    position_count: int
    latest_run: dict[str, Any] | None = None
    positions: list[PositionOut] | None = None


# ── 内部辅助 ────────────────────────────────────────────────────


def _get_or_404(db: Session, pid: str) -> Portfolio:
    portfolio = db.execute(select(Portfolio).where(Portfolio.pid == pid)).scalar_one_or_none()
    if portfolio is None:
        raise AppError(ErrorCode.NOT_FOUND, f"组合不存在：{pid}")
    return portfolio


def _resolve_and_validate(
    db: Session, positions: list[PositionIn], equal_weight: bool
) -> tuple[list[tuple[str, Any]], dict[str, Security]]:
    """去重/权重/窗口校验 + 标的注册校验；返回 ``(weights, secmap)``。"""
    raw = [(p.secid, p.weight) for p in positions]
    weights = svc.normalize_weights(raw, equal_weight)
    secmap = svc.validate_securities(db, [secid for secid, _ in weights])
    return weights, secmap


def _write_positions(db: Session, portfolio_id: int, weights: list[tuple[str, Any]]) -> None:
    for secid, weight in weights:
        db.add(Position(portfolio_id=portfolio_id, secid=secid, weight=weight))


def _detail(db: Session, portfolio: Portfolio) -> PortfolioOut:
    return PortfolioOut(
        id=portfolio.id,
        pid=portfolio.pid,
        name=portfolio.name,
        note=portfolio.note,
        base_ccy=portfolio.base_ccy,
        strategy=portfolio.strategy or {},
        status=portfolio.status,
        created_at=portfolio.created_at,
        updated_at=portfolio.updated_at,
        position_count=svc.position_count(db, portfolio.id),
        latest_run=svc.latest_run(db, portfolio.id),
        positions=svc.position_details(db, portfolio.id),
    )


# ── 端点 ────────────────────────────────────────────────────────


@router.get("", response_model=list[PortfolioOut])
def list_portfolios(
    db: DbDep,
    include_archived: bool = False,
) -> list[PortfolioOut]:
    """组合列表：默认仅 active（归档不返回）；含持仓数与最近一次 run 摘要。"""
    stmt = select(Portfolio).order_by(Portfolio.id)
    if not include_archived:
        stmt = stmt.where(Portfolio.status == "active")
    rows = db.execute(stmt).scalars().all()
    return [
        PortfolioOut(
            id=pf.id,
            pid=pf.pid,
            name=pf.name,
            note=pf.note,
            base_ccy=pf.base_ccy,
            strategy=pf.strategy or {},
            status=pf.status,
            created_at=pf.created_at,
            updated_at=pf.updated_at,
            position_count=svc.position_count(db, pf.id),
            latest_run=svc.latest_run(db, pf.id),
            positions=None,
        )
        for pf in rows
    ]


@router.post("", response_model=PortfolioOut, status_code=status.HTTP_201_CREATED)
def create_portfolio(
    payload: PortfolioCreate, db: DbDep, settings: SettingsDep
) -> PortfolioOut:
    """新建组合：校验 → 事务写 portfolios+positions+audit → 物化 JSON。"""
    weights, secmap = _resolve_and_validate(db, payload.positions, payload.equal_weight)

    if payload.pid is not None:
        pid = payload.pid
        svc.ensure_pid_available(db, pid)
    else:
        pid = svc.suggest_pid(db)

    portfolio = Portfolio(
        pid=pid,
        name=payload.name,
        note=payload.note,
        base_ccy="CNY",
        strategy=payload.strategy or {},
        status="active",
    )
    db.add(portfolio)
    db.flush()
    _write_positions(db, portfolio.id, weights)
    svc.write_audit(
        db, pid, "create",
        {"pid": pid, "name": portfolio.name,
         "positions": [{"secid": s, "weight": float(w)} for s, w in weights]},
    )
    db.commit()
    db.refresh(portfolio)

    svc.materialize(portfolio, weights, secmap, settings)
    return _detail(db, portfolio)


@router.get("/{pid}", response_model=PortfolioOut)
def get_portfolio(pid: str, db: DbDep) -> PortfolioOut:
    """组合详情（含 positions）。"""
    return _detail(db, _get_or_404(db, pid))


@router.put("/{pid}", response_model=PortfolioOut)
def update_portfolio(
    pid: str, payload: PortfolioUpdate, db: DbDep, settings: SettingsDep
) -> PortfolioOut:
    """更新组合（name/note/positions/strategy）；重跑校验并重新物化 + 审计。"""
    portfolio = _get_or_404(db, pid)
    if (
        payload.name is None
        and payload.note is None
        and payload.positions is None
        and payload.strategy is None
    ):
        raise AppError(ErrorCode.VALIDATION_ERROR, "无更新字段")

    weights = None
    secmap: dict[str, Security] | None = None
    if payload.positions is not None:
        weights, secmap = _resolve_and_validate(db, payload.positions, payload.equal_weight)

    if payload.name is not None:
        portfolio.name = payload.name
    if payload.note is not None:
        portfolio.note = payload.note
    if payload.strategy is not None:
        portfolio.strategy = payload.strategy

    if weights is not None:
        db.execute(Position.__table__.delete().where(Position.portfolio_id == portfolio.id))
        _write_positions(db, portfolio.id, weights)

    portfolio.updated_at = datetime.now(tz=portfolio.updated_at.tzinfo)
    svc.write_audit(
        db, pid, "update",
        {"pid": pid,
         "fields": [k for k, v in payload.model_dump().items() if v is not None],
         "positions": None if weights is None
         else [{"secid": s, "weight": float(w)} for s, w in weights]},
    )
    db.commit()
    db.refresh(portfolio)

    if weights is None:
        weights = svc.positions_of(db, portfolio.id)
        secmap = svc.validate_securities(db, [s for s, _ in weights])
    assert secmap is not None
    svc.materialize(portfolio, weights, secmap, settings)
    return _detail(db, portfolio)


@router.delete("/{pid}", response_model=PortfolioOut)
def archive_portfolio(pid: str, db: DbDep) -> PortfolioOut:
    """归档软删：``status="archived"`` + 审计；不存在 → 404。"""
    portfolio = _get_or_404(db, pid)
    portfolio.status = "archived"
    portfolio.updated_at = datetime.now(tz=portfolio.updated_at.tzinfo)
    svc.write_audit(db, pid, "delete", {"pid": pid})
    db.commit()
    db.refresh(portfolio)
    return _detail(db, portfolio)


# ── T2.3 评估 run（§6.2 / §9.3）─────────────────────────────────


@router.post("/{pid}/review")
def create_review(
    pid: str, db: DbDep, runner: RunnerDep, settings: SettingsDep
) -> JSONResponse:
    """触发评估 job（§6.2）→ 202 + job_id。

    数据门禁不阻断评估：最近一次 data_sync 六项验证非全绿时照常提交 job，
    但在响应 ``data_risk``（及 job 落库的 ``runs.summary.data_risk``）中提示数据风险。
    """
    portfolio = _get_or_404(db, pid)
    # 门禁不阻断：非全绿仅计算数据风险提示（缺口由 job 内 ``_ensure_gap`` 处理）
    data_risk = verification.data_risk_of(verification.latest_verify_summary(db))
    job_id = runner.submit(
        type="review",
        params={"pid": pid},
        portfolio_id=portfolio.id,
        fn=lambda ctx: review_svc.run_review(ctx.ws_root, pid, ctx.job_id, ctx.report),
        lock="compute",
        ws_name="default",
    )
    return JSONResponse(
        status_code=202,
        content={"job_id": job_id, "poll": f"/api/v1/jobs/{job_id}", "data_risk": data_risk},
    )

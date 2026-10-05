"""执行单端点（T4.2 / T4.3 / T4.4，§6.2 / §9.5）。

- ``POST /plans/{plan_id}/exec-orders`` ``{capital_cny?}`` → **同步**返回执行单
  （毫秒级机械换算，非 job；默认资金 ¥1,000,000）；plan 不存在 404；plan 过老 409。
- ``GET  /exec-orders/{exec_id}`` → 详情含完整 sheet 与状态时间线（审计序列）。
- ``POST /exec-orders/{exec_id}/confirm`` ``{ack_discipline:true}`` → ``confirmed``；
  缺 ACK → 422 ``ACK_REQUIRED``；非法跃迁 → 422。
- ``POST /exec-orders/{exec_id}/fill`` ``{fills:[{secid,price,shares}]}`` → ``executed``
  （secid∈单内、价格>0、数量为 100 整数倍且方向一致，否则 422；允许部分成交）。
- ``POST /exec-orders/{exec_id}/cancel`` ``{reason?}`` → ``cancelled``。
- ``GET  /portfolios/{pid}/exec-orders`` → 该组合执行单列表。
- ``GET  /portfolios/{pid}/rebalance-history`` → plans/orders/fills 归一化时间线。
- ``GET  /audit?entity=&entity_id=&limit=`` → 审计列表。

**不变量（§7）**：引擎永不直接动仓位——本模块只做换算与状态/审计写入，无券商接口。
本模块定义模块级 ``router``，由 ``app/api/v1/__init__.py`` 自动发现注册；
**不要修改该聚合文件**。
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.session import get_db
from app.services import exec_svc

router = APIRouter(tags=["exec-orders"])

DbDep = Annotated[Session, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

ACTOR = "system"  # β 单用户；T5.2 接入后替换为登录用户


class ExecCreateIn(BaseModel):
    """执行单入参（§6.2）：基准资金，默认 ¥1,000,000。"""

    capital_cny: float = Field(default=exec_svc.DEFAULT_CAPITAL_CNY, gt=0)


class ConfirmIn(BaseModel):
    """确认入参：``ack_discipline`` 必须显式为 true（§7 不变量 2）。"""

    ack_discipline: bool = False


class FillIn(BaseModel):
    """单笔成交登记。"""

    secid: str
    price: float
    shares: int


class FillBody(BaseModel):
    """成交登记入参：至少一笔。"""

    fills: list[FillIn] = Field(min_length=1)


class CancelIn(BaseModel):
    reason: str | None = None


# ── T4.2 生成与详情 ─────────────────────────────────────────────


@router.post("/plans/{plan_id}/exec-orders")
def create_exec_order(
    plan_id: str, payload: ExecCreateIn, db: DbDep, settings: SettingsDep
) -> dict[str, Any]:
    """由存量 plan + 最新 review 机械换算生成执行单（同步，§9.5 T4.2）。"""
    plan = exec_svc.get_plan_or_404(db, plan_id)
    return exec_svc.create_exec_order(db, settings, plan, payload.capital_cny)


@router.get("/exec-orders/{exec_id}")
def get_exec_order(exec_id: str, db: DbDep) -> dict[str, Any]:
    """执行单详情（含完整 sheet 与状态时间线）。"""
    return exec_svc.detail(db, exec_svc.get_exec_or_404(db, exec_id))


@router.get("/portfolios/{pid}/exec-orders")
def list_portfolio_exec_orders(pid: str, db: DbDep) -> list[dict[str, Any]]:
    """该组合执行单列表（按创建时间倒序）。"""
    portfolio = exec_svc.get_portfolio_or_404(db, pid)
    return exec_svc.list_for_portfolio(db, portfolio.id)


# ── T4.3 状态机 ─────────────────────────────────────────────────


@router.post("/exec-orders/{exec_id}/confirm")
def confirm_exec_order(exec_id: str, payload: ConfirmIn, db: DbDep) -> dict[str, Any]:
    """确认执行单：强制 ACK（否则 422 ``ACK_REQUIRED``），``draft`` → ``confirmed``。"""
    row = exec_svc.get_exec_or_404(db, exec_id)
    return exec_svc.confirm(db, row, payload.ack_discipline, actor=ACTOR)


@router.post("/exec-orders/{exec_id}/fill")
def fill_exec_order(exec_id: str, payload: FillBody, db: DbDep) -> dict[str, Any]:
    """登记成交：校验后 ``confirmed`` → ``executed``（允许部分成交）。"""
    row = exec_svc.get_exec_or_404(db, exec_id)
    fills = [f.model_dump() for f in payload.fills]
    return exec_svc.register_fills(db, row, fills, actor=ACTOR)


@router.post("/exec-orders/{exec_id}/cancel")
def cancel_exec_order(exec_id: str, payload: CancelIn, db: DbDep) -> dict[str, Any]:
    """取消执行单：``draft`` / ``confirmed`` → ``cancelled``。"""
    row = exec_svc.get_exec_or_404(db, exec_id)
    return exec_svc.cancel(db, row, payload.reason, actor=ACTOR)


# ── T4.4 历史与审计 ─────────────────────────────────────────────


@router.get("/portfolios/{pid}/rebalance-history")
def get_rebalance_history(pid: str, db: DbDep) -> dict[str, Any]:
    """调仓历史：plans + exec_orders + fills 归一化时间线。"""
    return exec_svc.rebalance_history(db, pid)


@router.get("/audit")
def list_audit(
    db: DbDep,
    entity: str | None = None,
    entity_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """审计列表查询（按 ``entity`` / ``entity_id`` 过滤，时间倒序）。"""
    items = exec_svc.list_audit(db, entity, entity_id, limit)
    return {"items": items, "count": len(items), "limit": limit}

"""调仓评估单端点（T4.1，§6.2 / §9.5）。

- ``POST /portfolios/{pid}/rebalance-plan``：组合不存在 404 → 数据门禁非全绿 409
  （``VERIFY_GATE_FAILED``，不提交 job）→ 202 ``{"job_id", "poll"}``；
- ``GET /plans/{plan_id}``：plan 详情；``plan_id`` 支持数字 ``id`` 或 ``plan_uid``，无 → 404；
- ``GET /portfolios/{pid}/plans``：该组合 plan 列表（``status=="draft"`` 优先，其余按
  ``created_at`` desc；列表不含大 payload，含 ``cost_est_cny`` 摘要字段）。

本模块定义模块级 ``router``，由 ``app/api/v1/__init__.py`` 自动发现注册；
**不要修改该聚合文件**。
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1.data import RunnerDep
from app.core.errors import AppError, ErrorCode
from app.db.session import get_db
from app.models import Portfolio, RebalancePlan
from app.services import plan_svc, verification

router = APIRouter(tags=["plans"])

DbDep = Annotated[Session, Depends(get_db)]

# 202 语义（§6.1）：长任务 POST → 返回 job_id 与轮询地址
PLAN_ACCEPTED = 202


def _get_portfolio_or_404(db: Session, pid: str) -> Portfolio:
    portfolio = db.execute(select(Portfolio).where(Portfolio.pid == pid)).scalar_one_or_none()
    if portfolio is None:
        raise AppError(ErrorCode.NOT_FOUND, f"组合不存在：{pid}")
    return portfolio


def _get_plan_or_404(db: Session, plan_id: str) -> RebalancePlan:
    """``plan_id`` 为纯数字按主键 ``id`` 查，否则按 ``plan_uid`` 查。"""
    stmt = select(RebalancePlan)
    if plan_id.isdigit():
        stmt = stmt.where(RebalancePlan.id == int(plan_id))
    else:
        stmt = stmt.where(RebalancePlan.plan_uid == plan_id)
    plan = db.execute(stmt).scalar_one_or_none()
    if plan is None:
        raise AppError(ErrorCode.NOT_FOUND, f"调仓计划不存在：{plan_id}")
    return plan


def _plan_detail(plan: RebalancePlan) -> dict[str, Any]:
    return {
        "id": plan.id,
        "plan_uid": plan.plan_uid,
        "portfolio_id": plan.portfolio_id,
        "run_id": plan.run_id,
        "status": plan.status,
        "payload": plan.payload,
        "created_at": plan.created_at,
    }


@router.post("/portfolios/{pid}/rebalance-plan")
def create_rebalance_plan(pid: str, db: DbDep, runner: RunnerDep) -> JSONResponse:
    """触发调仓评估单 job（§6.2）：404 → 门禁 409 → 202 + job_id + poll。"""
    portfolio = _get_portfolio_or_404(db, pid)
    # 前置门禁（同步）：最近一次 data_sync 六项验证非全绿即 409，不提交 job
    verification.gate_or_raise(db)
    job_id = runner.submit(
        type="rebalance_plan",
        params={"pid": pid},
        portfolio_id=portfolio.id,
        fn=lambda ctx: plan_svc.run_plan_job(ctx.ws_root, pid, ctx.job_id, ctx.report),
        lock="compute",
        ws_name="default",
    )
    return JSONResponse(
        status_code=PLAN_ACCEPTED,
        content={"job_id": job_id, "poll": f"/api/v1/jobs/{job_id}"},
    )


@router.get("/plans/{plan_id}")
def get_plan(plan_id: str, db: DbDep) -> dict[str, Any]:
    """plan 详情（含完整 ``payload``）；不存在 → 404。"""
    return _plan_detail(_get_plan_or_404(db, plan_id))


@router.get("/portfolios/{pid}/plans")
def list_plans(pid: str, db: DbDep) -> list[dict[str, Any]]:
    """该组合 plan 列表：``draft`` 优先，其余按 ``created_at`` desc（不含大 payload）。"""
    portfolio = _get_portfolio_or_404(db, pid)
    rows = (
        db.execute(
            select(RebalancePlan)
            .where(RebalancePlan.portfolio_id == portfolio.id)
            .order_by(
                (RebalancePlan.status == "draft").desc(),
                RebalancePlan.created_at.desc(),
                RebalancePlan.id.desc(),
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": plan.id,
            "plan_uid": plan.plan_uid,
            "portfolio_id": plan.portfolio_id,
            "run_id": plan.run_id,
            "status": plan.status,
            "cost_est_cny": (plan.payload or {}).get("cost_est_cny"),
            "created_at": plan.created_at,
        }
        for plan in rows
    ]

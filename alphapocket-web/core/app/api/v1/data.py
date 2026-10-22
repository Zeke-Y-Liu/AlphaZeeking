"""数据同步端点（T1.4，§6.2 端点清单 / §4.2 三 mode）。

- ``POST /data/sync``：``{mode: bootstrap|incremental|refresh, secid?}`` →
  202 ``{"job_id", "poll"}``（``lock="sync"`` 全局唯一，冲突由 JobRunner 抛
  ``409 JOB_LOCKED``）；mode 非法 / refresh 缺 secid → 422；上游熔断开 → 503。
- ``GET /data/status``（T1.5）：各标的最后 bar 日期/缺口/asset_type、universe 汇总、
  最近 sync job 状态、六项验证最新徽章。
- ``GET /data/verifications?limit=20``（T1.5）：验证结果列表（含 detail）。

本模块定义模块级 ``router``，由 ``app/api/v1/__init__.py`` 自动发现注册；
**不要修改该聚合文件**。
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import AppError, ErrorCode
from app.db.session import get_db
from app.services.data_status import build_status, build_verifications
from app.services.data_sync import VALID_MODES, circuit_open, sync_workspace
from app.services.jobs import JobContext, JobRunner

router = APIRouter(prefix="/data", tags=["data"])

# 202 语义（§6.1）：长任务 POST → 返回 job_id 与轮询地址
SYNC_ACCEPTED = 202


def get_job_runner(request: Request) -> JobRunner:
    """依赖：取 ``app.state.job_runner``（等价 ``app.main.get_job_runner``）。

    延迟导入 ``app.main`` 规避 ``app.main`` ↔ ``app.api.v1`` 的循环导入
    （``app.main`` 顶层即 include 本聚合路由）。
    """
    from app.main import get_job_runner as _dep

    return _dep(request)


RunnerDep = Annotated[JobRunner, Depends(get_job_runner)]


class SyncIn(BaseModel):
    """同步入参（§6.2）。"""

    mode: str
    secid: str | None = None


def _run_sync(ctx: JobContext) -> dict[str, Any]:
    """job 函数：透传 mode/secid，进度走 ``ctx.report``，落 runs（含 run_uid）。"""
    return sync_workspace(
        ctx.ws_root,
        mode=ctx.params["mode"],
        secid=ctx.params.get("secid"),
        progress=ctx.report,
        job_id=ctx.job_id,
    )


@router.post("/sync")
def create_sync(payload: SyncIn, runner: RunnerDep) -> JSONResponse:
    """触发同步 job（§4.2 / §6.1）：202 + job_id + poll；冲突 409；熔断 503。"""
    if payload.mode not in VALID_MODES:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"未知 mode：{payload.mode!r}",
            detail={"mode": payload.mode, "allowed": sorted(VALID_MODES)},
        )
    if payload.mode == "refresh" and not payload.secid:
        raise AppError(
            ErrorCode.VALIDATION_ERROR, "refresh 模式必须提供 secid", detail={"field": "secid"}
        )
    if circuit_open():
        raise AppError(ErrorCode.UPSTREAM_RATE_LIMITED, "上游数据源熔断中，请稍后重试")

    job_id = runner.submit(
        type="data_sync",
        params={"mode": payload.mode, "secid": payload.secid},
        portfolio_id=None,
        fn=_run_sync,
        lock="sync",
    )
    return JSONResponse(
        status_code=SYNC_ACCEPTED,
        content={"job_id": job_id, "poll": f"/api/v1/jobs/{job_id}"},
    )


class SyncSecidIn(BaseModel):
    """单标的数据拉取入参（标的页/组合页手动入口）。"""

    secid: str


@router.post("/sync-secid")
def create_sync_secid(payload: SyncSecidIn, db: Annotated[Session, Depends(get_db)],
                      runner: RunnerDep) -> JSONResponse:
    """手动拉取**单个标的**的数据（refresh：近 5 日窗口增量合并，sync 互斥 409）。

    与 ``POST /data/sync {mode: refresh}`` 同链路，区别仅两点：
    - 入参只传 ``secid``（语义化入口，供组合/公司页面「拉取该标的数据」按钮）；
    - 前置校验标的已注册（未注册 422，提示先到数据页注册）。
    """
    from sqlalchemy import select

    from app.models import Security

    secid = payload.secid.strip().upper()
    exists = db.execute(select(Security.secid).where(Security.secid == secid)).scalar_one_or_none()
    if not exists:
        raise AppError(
            ErrorCode.SECURITY_NOT_FOUND,
            "标的不存在或未注册，请先在数据页注册",
            detail={"missing_secids": [secid]},
        )
    if circuit_open():
        raise AppError(ErrorCode.UPSTREAM_RATE_LIMITED, "上游数据源熔断中，请稍后重试")
    job_id = runner.submit(
        type="data_sync",
        params={"mode": "refresh", "secid": secid},
        portfolio_id=None,
        fn=_run_sync,
        lock="sync",
    )
    return JSONResponse(
        status_code=SYNC_ACCEPTED,
        content={"job_id": job_id, "poll": f"/api/v1/jobs/{job_id}", "secid": secid},
    )


# ── T1.5 数据状态 API（§6.2 / §9.2）──────────────────────────────


@router.get("/status")
def get_data_status(
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """各标的最后 bar 日期 / 缺口 / 距今、universe 汇总、最近验证与 sync 状态。"""
    return build_status(db, settings.workspace_path)


@router.get("/verifications")
def list_verifications(
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> dict[str, Any]:
    """六项验证明细列表（``limit`` 默认 20，合法区间 [1, 200]）。"""
    items = build_verifications(db, limit)
    return {"items": items, "count": len(items), "limit": limit}

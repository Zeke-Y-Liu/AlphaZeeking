"""job 状态查询端点（§6.1 / T1.2）。

``GET /api/v1/jobs/{id}`` → 严格 6 字段 ``{"id","type","status","progress","result","error"}``；
job 不存在 → ``404 NOT_FOUND``（统一错误体）。

本模块定义模块级 ``router``，由 ``app/api/v1/__init__.py`` 自动发现注册；
**不要修改该聚合文件**。
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.errors import AppError, ErrorCode
from app.db.session import get_db
from app.models import Job

router = APIRouter(tags=["jobs"])


@router.get("/jobs/{job_id}")
def get_job(job_id: str, db: Annotated[Session, Depends(get_db)]) -> dict[str, Any]:
    """返回 job 状态（§6.1 响应体）；不存在抛 ``AppError(NOT_FOUND)`` → 404。"""
    job = db.get(Job, job_id)
    if job is None:
        raise AppError(ErrorCode.NOT_FOUND, f"job 不存在：{job_id}")
    return {
        "id": job.id,
        "type": job.type,
        "status": job.status,
        "progress": job.progress,
        "result": job.result,
        "error": job.error,
    }

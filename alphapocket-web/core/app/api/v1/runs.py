"""运行历史端点（T2.3 / T2.4，§6.2）。

- ``GET /api/v1/runs`` → 运行历史列表；query ``type`` / ``portfolio_id`` / ``limit``
  （默认 20，合法区间 [1, 200]）；按 ``created_at`` desc、``id`` desc 排序。
- ``GET /api/v1/runs/{run_uid}/artifacts/{file}``（T2.4）→ 产物流；key 为
  ``runs/{run_uid}/{file}``，按扩展名给 content-type，``X-Frame-Options: SAMEORIGIN``。

本模块定义模块级 ``router``，由 ``app/api/v1/__init__.py`` 自动发现注册；
**不要修改该聚合文件**。
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from minio.error import S3Error
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError, ErrorCode
from app.db.session import get_db
from app.models import Run
from app.services import artifacts

router = APIRouter(tags=["runs"])

DbDep = Annotated[Session, Depends(get_db)]


def _serialize(run: Run) -> dict[str, Any]:
    """run 行 → 响应 dict（as_of / created_at 可能为 None）。"""
    return {
        "run_uid": run.run_uid,
        "type": run.type,
        "portfolio_id": run.portfolio_id,
        "as_of": run.as_of.isoformat() if run.as_of else None,
        "summary": run.summary,
        "artifact_keys": run.artifact_keys,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


@router.get("/runs")
def list_runs(
    db: DbDep,
    type: str | None = None,
    portfolio_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> list[dict[str, Any]]:
    """运行历史（§6.2）：可按 type / portfolio_id 过滤，limit 默认 20。"""
    stmt = select(Run).order_by(Run.created_at.desc(), Run.id.desc())
    if type is not None:
        stmt = stmt.where(Run.type == type)
    if portfolio_id is not None:
        stmt = stmt.where(Run.portfolio_id == portfolio_id)
    stmt = stmt.limit(limit)
    rows = db.execute(stmt).scalars().all()
    return [_serialize(run) for run in rows]


# ── T2.4 产物服务（§5.3 / §6.2 / §9.3） ──────────────────────────

# 流式分块大小（§9.3：按块 yield，不整对象进内存）
_CHUNK_SIZE = 64 * 1024

# 扩展名 → media_type（小写；§9.3 明确 charset）
_ARTIFACT_MEDIA_TYPES: dict[str, str] = {
    "html": "text/html; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "json": "application/json; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
}
_DEFAULT_MEDIA_TYPE = "application/octet-stream"

# 对象缺失的 S3 错误码（→ 404）
_MISSING_CODES = frozenset({"NoSuchKey", "NoSuchBucket"})


def _artifact_media_type(filename: str) -> str:
    """按扩展名（小写）推断 media_type，未知扩展名 → octet-stream。"""
    ext = Path(filename).suffix.lstrip(".").lower()
    return _ARTIFACT_MEDIA_TYPES.get(ext, _DEFAULT_MEDIA_TYPE)


def _unsafe_segment(value: str) -> bool:
    """路径段安全校验：空 / 含 ``/`` 或 ``\\`` / 含 ``..`` → 不安全（→ 404）。"""
    return not value or any(ch in value for ch in ("/", "\\")) or ".." in value


def _iter_object(response: Any, chunk_size: int = _CHUNK_SIZE) -> Iterator[bytes]:
    """按块流式产出对象字节，结束/异常时释放底层连接。"""
    try:
        yield from response.stream(chunk_size)
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
        release = getattr(response, "release_conn", None)
        if callable(release):
            release()


@router.get("/runs/{run_uid}/artifacts/{file}")
def get_run_artifact(run_uid: str, file: str) -> StreamingResponse:
    """产物流（§6.2 / §9.3）：``runs/{run_uid}/{file}`` 流式下载。

    - 路径段含 ``/`` / ``\\`` / ``..`` 或为空 → 404（不拼接逃逸）；
    - 对象不存在（S3 ``NoSuchKey`` / ``NoSuchBucket``）→ 404 ``NOT_FOUND``；
    - 扩展名决定 content-type；未知扩展名按 ``application/octet-stream`` 照常 200。
    """
    if _unsafe_segment(run_uid) or _unsafe_segment(file):
        raise AppError(ErrorCode.NOT_FOUND, f"产物不存在：runs/{run_uid}/{file}")

    key = f"runs/{run_uid}/{file}"
    try:
        response, stat = artifacts.get_object(key)
    except S3Error as exc:
        if exc.code in _MISSING_CODES:
            raise AppError(ErrorCode.NOT_FOUND, f"产物不存在：{key}") from exc
        raise

    headers = {
        "X-Frame-Options": "SAMEORIGIN",
        "Content-Disposition": f'inline; filename="{file}"',
    }
    size = getattr(stat, "size", None)
    if isinstance(size, int):
        headers["Content-Length"] = str(size)

    return StreamingResponse(
        _iter_object(response),
        media_type=_artifact_media_type(file),
        headers=headers,
    )

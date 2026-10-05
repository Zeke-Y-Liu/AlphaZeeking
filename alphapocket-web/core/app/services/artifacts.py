"""MinIO 对象存储服务（T2.3，§5.3 对象存储布局）。

布局（冻结契约 §5.3）::

    ap-artifacts/
      runs/{run_uid}/report.html|report.md|review.json|rebalance_plan.json|...

- key 即 workspace artifacts 相对路径 ``runs/{run_uid}/<file>``；
- **懒加载**：``_client()`` 每次按当前 ``get_settings()`` 构造 ``Minio`` 客户端，
  模块导入不触发任何网络连接；``ensure_bucket()`` 桶不存在则创建。
- ``put_run_artifacts`` 按扩展名给 content_type（html/md/json/csv，其余
  ``application/octet-stream``），返回**排序后**的 key 列表；**异常向上抛**
  （上传失败须让 job failed，绝不做静默 best-effort；测试用 mock 替换）。
- ``get_object`` 预留给 T2.4 产物下载端点，本任务不测试。

本模块只新增对象存储能力，不改动任何既有模块。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from minio import Minio

from app.core.config import get_settings

logger = logging.getLogger("app.artifacts")

# 扩展名 → content_type（小写比较）
_CONTENT_TYPES: dict[str, str] = {
    "html": "text/html; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "json": "application/json",
    "csv": "text/csv",
}
DEFAULT_CONTENT_TYPE = "application/octet-stream"


def _client() -> Minio:
    """按当前配置构造 MinIO 客户端（无网络连接；``secure=False``）。"""
    settings = get_settings()
    return Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=False,
    )


def ensure_bucket() -> str:
    """确保目标桶存在（不存在则创建），返回桶名。"""
    settings = get_settings()
    bucket = settings.minio_bucket
    client = _client()
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)
    return bucket


def _content_type(filename: str) -> str:
    """按扩展名推断 content_type（未知扩展名 → octet-stream）。"""
    ext = Path(filename).suffix.lstrip(".").lower()
    return _CONTENT_TYPES.get(ext, DEFAULT_CONTENT_TYPE)


def put_run_artifacts(run_uid: str, files: dict[str, Path]) -> list[str]:
    """把 ``{filename: path}`` 上传为 ``runs/{run_uid}/{filename}``，返回排序后 key 列表。

    异常向上抛（调用方/测试负责 mock 或处理），不做静默 best-effort。
    """
    client = _client()
    bucket = ensure_bucket()
    keys: list[str] = []
    for filename, path in files.items():
        key = f"runs/{run_uid}/{filename}"
        client.fput_object(
            bucket,
            key,
            str(path),
            content_type=_content_type(filename),
        )
        keys.append(key)
    return sorted(keys)


def get_object(key: str) -> tuple[Any, Any]:
    """预留（T2.4）：取对象，返回 ``(response, stat)``。"""
    client = _client()
    bucket = get_settings().minio_bucket
    response = client.get_object(bucket, key)
    stat = client.stat_object(bucket, key)
    return response, stat

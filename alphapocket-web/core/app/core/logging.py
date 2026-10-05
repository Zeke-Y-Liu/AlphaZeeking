"""结构化 JSON 行日志 + 请求 trace 中间件。

``trace_id`` / ``job_id`` 以 ``contextvars`` 贯穿请求与后台 job，
供 T1.2（job 运行器）、T5.1（BFF 透传）复用。
"""
from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

REQUEST_ID_HEADER = "X-Request-Id"

_trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)
_job_id_var: ContextVar[str | None] = ContextVar("job_id", default=None)


def set_trace_id(trace_id: str | None) -> None:
    _trace_id_var.set(trace_id)


def get_trace_id() -> str | None:
    return _trace_id_var.get()


def set_job_id(job_id: str | None) -> None:
    _job_id_var.set(job_id)


def get_job_id() -> str | None:
    return _job_id_var.get()


class JsonFormatter(logging.Formatter):
    """输出单行 JSON，字段：ts/level/logger/msg/trace_id/job_id。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "trace_id": get_trace_id(),
            "job_id": get_job_id(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


_configured = False


def configure_logging(level: str = "INFO") -> None:
    """配置根 logger 的 JSON handler（幂等：重复调用不新增 handler）。"""
    global _configured
    root = logging.getLogger()
    root.setLevel(level.upper() if isinstance(level, str) else level)
    if _configured:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    _configured = True


class TraceIdMiddleware(BaseHTTPMiddleware):
    """为每个请求生成/透传 trace_id，写 contextvar 与响应头，并记录一行访问日志。"""

    async def dispatch(self, request: Request, call_next):
        trace_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        token = _trace_id_var.set(trace_id)
        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers[REQUEST_ID_HEADER] = trace_id
            return response
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            logging.getLogger("app.request").info(
                "%s %s %s %.1fms",
                request.method,
                request.url.path,
                status_code,
                duration_ms,
            )
            _trace_id_var.reset(token)

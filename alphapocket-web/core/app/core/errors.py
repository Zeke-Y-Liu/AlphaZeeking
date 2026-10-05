"""统一错误体与异常处理器（严格对齐实施书 §6.3）。

对外错误响应体恒为 ``{"code": str, "message": str, "detail": any | null}``。
"""
from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class ErrorCode(StrEnum):
    """对外错误码（取值与 §6.3 完全一致）。"""

    VERIFY_GATE_FAILED = "VERIFY_GATE_FAILED"       # 409 数据门禁未全绿
    JOB_LOCKED = "JOB_LOCKED"                       # 409 workspace 计算互斥
    WEIGHT_NOT_NORMALIZED = "WEIGHT_NOT_NORMALIZED"  # 422 权重越界
    SECURITY_NOT_FOUND = "SECURITY_NOT_FOUND"       # 422 标的不存在
    ACK_REQUIRED = "ACK_REQUIRED"                   # 422 缺少纪律确认
    UPSTREAM_RATE_LIMITED = "UPSTREAM_RATE_LIMITED"  # 503 上游熔断
    VALIDATION_ERROR = "VALIDATION_ERROR"           # 422 通用参数校验
    UNAUTHORIZED = "UNAUTHORIZED"                   # 401 认证失败（T5.2）
    ACCOUNT_LOCKED = "ACCOUNT_LOCKED"               # 429 连续失败锁定（T5.2）
    NOT_FOUND = "NOT_FOUND"                         # 404
    INTERNAL_ERROR = "INTERNAL_ERROR"               # 500
    # §6.3 中 DATA_STALE 为核心内部化处理，保留常量以备内部使用，不作为对外错误码返回
    DATA_STALE = "DATA_STALE"
    # §6.3 未显式列举的 HTTP 状态兜底码（如 405）
    HTTP_ERROR = "HTTP_ERROR"


# 错误码 → HTTP 状态码（§6.3）
CODE_STATUS: dict[str, int] = {
    ErrorCode.VERIFY_GATE_FAILED.value: 409,
    ErrorCode.JOB_LOCKED.value: 409,
    ErrorCode.DATA_STALE.value: 409,
    ErrorCode.WEIGHT_NOT_NORMALIZED.value: 422,
    ErrorCode.SECURITY_NOT_FOUND.value: 422,
    ErrorCode.ACK_REQUIRED.value: 422,
    ErrorCode.UPSTREAM_RATE_LIMITED.value: 503,
    ErrorCode.VALIDATION_ERROR.value: 422,
    ErrorCode.UNAUTHORIZED.value: 401,
    ErrorCode.ACCOUNT_LOCKED.value: 429,
    ErrorCode.NOT_FOUND.value: 404,
    ErrorCode.INTERNAL_ERROR.value: 500,
}


class AppError(Exception):
    """业务异常：携带统一错误码与可选明细。"""

    def __init__(
        self,
        code: ErrorCode | str,
        message: str,
        status_code: int | None = None,
        detail: dict | list | None = None,
    ) -> None:
        self.code = code.value if isinstance(code, ErrorCode) else str(code)
        self.message = message
        self.status_code = status_code or CODE_STATUS.get(self.code, 500)
        self.detail = detail
        super().__init__(message)


def error_body(code: str, message: str, detail: Any = None) -> dict[str, Any]:
    """构造统一错误体。"""
    return {"code": code, "message": message, "detail": jsonable_encoder(detail)}


def _code_for_status(status_code: int) -> str:
    if status_code == 404:
        return ErrorCode.NOT_FOUND.value
    if status_code in (400, 422):
        return ErrorCode.VALIDATION_ERROR.value
    if status_code >= 500:
        return ErrorCode.INTERNAL_ERROR.value
    return ErrorCode.HTTP_ERROR.value


def register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理器，保证任何错误响应体统一。"""

    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message, exc.detail),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        detail = [
            {
                "loc": list(err.get("loc", [])),
                "msg": err.get("msg", ""),
                "type": err.get("type", ""),
            }
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=error_body(ErrorCode.VALIDATION_ERROR.value, "请求参数校验失败", detail),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, (dict, list)) else None
        message = exc.detail if isinstance(exc.detail, str) else "请求失败"
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(_code_for_status(exc.status_code), message, detail),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # 堆栈只进日志，绝不泄漏进响应体
        logger.exception("未捕获异常: %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content=error_body(ErrorCode.INTERNAL_ERROR.value, "内部服务错误", None),
        )

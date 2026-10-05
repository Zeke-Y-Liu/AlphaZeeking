"""core 服务装配（T1.1 骨架）。

W2/T1.1：应用工厂 + lifespan 依赖探测 + GET /healthz + 统一错误体 + 结构化日志。
业务端点（组合/评估/回测/调仓/数据同步）由 T1.3+ 交付，本文件不实现。
"""
from __future__ import annotations

import logging
import socket
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, Request

from app.api.v1 import api_v1_router
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import TraceIdMiddleware, configure_logging
from app.services.jobs import JobRunner
from app.services.workspace import WorkspaceManager

# 单依赖探测超时（秒）；启动期不因依赖不可达而阻塞过久
PROBE_TIMEOUT_S = 2.0


def _split_host_port(value: str, default_port: int) -> tuple[str, int]:
    """从 URL 或 ``host:port`` 中解析主机与端口。"""
    if "://" in value:
        parsed = urlparse(value)
        return parsed.hostname or "localhost", parsed.port or default_port
    host, _, port = value.partition(":")
    return host or "localhost", int(port) if port else default_port


def _probe_tcp(value: str, default_port: int) -> dict[str, Any]:
    """TCP 连通性探测；任何异常均吞掉并返回降级结果。"""
    host, port = _split_host_port(value, default_port)
    try:
        with socket.create_connection((host, port), timeout=PROBE_TIMEOUT_S):
            return {"ok": True, "detail": f"tcp {host}:{port} 可达"}
    except Exception as exc:  # noqa: BLE001 - 探测失败即降级，绝不外抛
        return {"ok": False, "detail": f"tcp {host}:{port} 不可达: {type(exc).__name__}"}


def probe_dependencies(settings: Settings) -> dict[str, dict[str, Any]]:
    """一次性探测 pg/redis/minio（当前为 TCP 连通性探测）。"""
    return {
        "pg": _probe_tcp(settings.database_url, 5432),
        "redis": _probe_tcp(settings.redis_url, 6379),
        "minio": _probe_tcp(settings.minio_endpoint, 9000),
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    # 启动期探测一次并缓存到 app.state，供 /healthz 读取（不阻塞过久）
    app.state.deps = probe_dependencies(settings)
    # T1.2：初始化默认 workspace 与进程级 job 运行器，挂 app.state 供后续任务取用
    workspace = WorkspaceManager(settings)
    workspace.ensure()
    runner = JobRunner(workspace)
    app.state.workspace_manager = workspace
    app.state.job_runner = runner
    # 启动自愈：上次进程异常退出遗留的超时 running job 置 failed（PG 不可达则降级不阻断）
    try:
        recovered = runner.recover_stale()
        logging.getLogger(__name__).info("启动自愈完成：%d 个超时 running job 置为 failed", recovered)
    except Exception:  # noqa: BLE001 - 自愈失败不阻断启动
        logging.getLogger(__name__).exception("启动自愈失败（PG 可能不可达）")
    yield
    runner.shutdown()


def get_job_runner(request: Request) -> JobRunner:
    """FastAPI 依赖：``runner = Depends(get_job_runner)`` → ``request.app.state.job_runner``。

    供后续任务（T2.3/T3.2/T4.1）提交 job 使用：``runner.submit(...)``。
    """
    return request.app.state.job_runner


def create_app() -> FastAPI:
    """应用工厂。"""
    application = FastAPI(
        title="AlphaPocket Quant Core",
        version="0.1.0",
        description="AlphaPocket V4-β 量化服务（core）",
        docs_url="/docs",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )
    application.add_middleware(TraceIdMiddleware)
    register_exception_handlers(application)

    @application.get("/healthz", tags=["health"])
    def healthz(request: Request) -> dict[str, Any]:
        """存活 + 依赖探测；依赖不可达时返回 200 + degraded，绝不 5xx。"""
        deps = getattr(request.app.state, "deps", None)
        if deps is None:
            deps = probe_dependencies(get_settings())
            request.app.state.deps = deps
        ok = all(item.get("ok") for item in deps.values())
        return {"status": "ok" if ok else "degraded", "deps": deps}

    application.include_router(api_v1_router)
    return application


app = create_app()

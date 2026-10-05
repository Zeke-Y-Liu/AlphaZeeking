"""回测端点（T3.2，§6.2 ``POST /portfolios/{id}/backtest`` / §9.4）。

流程
----
1. ``Portfolio`` 不存在 → 404 ``NOT_FOUND``；
2. **同步门禁** ``verification.gate_or_raise(db)``：最近一次 ``data_sync`` 六项验证非全绿
   → 409 ``VERIFY_GATE_FAILED``，**不提交 job**；
3. **同步参数校验**（§6.2）：``rebalance`` 枚举 / ``start ≤ end`` / ``benchmark_secid``
   存在于工作区 bars → 非法 422 ``VALIDATION_ERROR``；
4. ``runner.submit(type="backtest", params=..., lock="compute")`` → 202
   ``{"job_id", "poll"}``。

benchmark 校验读工作区 bars：``<ws>/sandbox/repo/market/daily_bars.json``，回退
``<ws>/remote/repo/...``；bars 缺失视为「无法校验」→ 交给服务层/job 报错（不 500）。
``_load_bars`` 为模块级接缝，测试可注入/monkeypatch。

本模块定义模块级 ``router``，由 ``app/api/v1/__init__.py`` 自动发现注册；
**不要修改该聚合文件**。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

from applib import backtest as BT
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1.data import RunnerDep
from app.core.config import Settings, get_settings
from app.core.errors import AppError, ErrorCode
from app.db.session import get_db
from app.models import Portfolio
from app.services import backtest_svc, verification

router = APIRouter(prefix="/portfolios", tags=["backtest"])

DbDep = Annotated[Session, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

# 202 语义（§6.1）：长任务 POST → 返回 job_id 与轮询地址
BT_ACCEPTED = 202

_DAILY_BARS_REL = Path("market") / "daily_bars.json"


class BacktestRequest(BaseModel):
    """回测入参（§6.2）。默认值与 applib T3.1 锚点一致。"""

    start: str | None = None
    end: str | None = None
    rf: float = 0.0
    rebalance: str = "monthly"
    benchmark_secid: str = "510300.SH"
    cost_bps: float = 2.5


def _get_or_404(db: Session, pid: str) -> Portfolio:
    portfolio = db.execute(select(Portfolio).where(Portfolio.pid == pid)).scalar_one_or_none()
    if portfolio is None:
        raise AppError(ErrorCode.NOT_FOUND, f"组合不存在：{pid}")
    return portfolio


def _load_bars(settings: Settings) -> dict[str, Any] | None:
    """读工作区 bars（sandbox/repo 优先，``remote/repo`` 回退）；缺失/损坏 → ``None``。

    返回 ``None`` 表示「无法校验」（交给服务层/job 处理），绝不抛异常。
    """
    root = settings.workspace_path
    for path in (
        root / "sandbox" / "repo" / _DAILY_BARS_REL,
        root / "remote" / "repo" / _DAILY_BARS_REL,
    ):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _validate_sync(payload: BacktestRequest, settings: Settings) -> None:
    """同步参数校验（§6.2）：非法一律 ``AppError(VALIDATION_ERROR)``（422）。"""
    if payload.rebalance not in BT.REBALANCE_CHOICES:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"未知再平衡频率：{payload.rebalance!r}",
            detail={"field": "rebalance", "allowed": list(BT.REBALANCE_CHOICES)},
        )
    if payload.start and payload.end and payload.start > payload.end:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "start 不得晚于 end",
            detail={"field": "start", "start": payload.start, "end": payload.end},
        )
    bars = _load_bars(settings)
    if bars is not None and payload.benchmark_secid not in bars:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"基准标的不在交易日线中：{payload.benchmark_secid}",
            detail={
                "field": "benchmark_secid",
                "benchmark_secid": payload.benchmark_secid,
                "examples": sorted(bars)[:5],
            },
        )


@router.post("/{pid}/backtest")
def create_backtest(
    pid: str,
    db: DbDep,
    runner: RunnerDep,
    settings: SettingsDep,
    payload: BacktestRequest | None = None,
) -> JSONResponse:
    """触发回测 job（§6.2）：404 → 门禁 409 → 参数 422 → 202 + job_id + poll。

    ``payload`` 可省略（全部字段有默认值，缺省按 T3.1 锚点参数）。
    """
    portfolio = _get_or_404(db, pid)
    # 前置门禁（同步）：最近一次 data_sync 六项验证非全绿即 409，不提交 job
    verification.gate_or_raise(db)
    # 同步参数校验（非全绿前已返回；此处非法直接 422，不提交 job）
    body = payload or BacktestRequest()
    _validate_sync(body, settings)

    params = body.model_dump()
    job_id = runner.submit(
        type="backtest",
        params=params,
        portfolio_id=portfolio.id,
        fn=lambda ctx: backtest_svc.run_backtest_job(
            ctx.ws_root, portfolio.pid, params, ctx.job_id, ctx.report
        ),
        lock="compute",
        ws_name="default",
    )
    return JSONResponse(
        status_code=BT_ACCEPTED,
        content={"job_id": job_id, "poll": f"/api/v1/jobs/{job_id}"},
    )

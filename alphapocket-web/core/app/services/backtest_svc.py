"""回测 run 核心逻辑（T3.2，§6.2 ``POST /portfolios/{id}/backtest`` / §9.4）。

职责
----
``run_backtest_job`` 为异步 job 函数体（由 ``POST /portfolios/{id}/backtest`` 提交）：

1. ``Ctx(root=ws_root)`` + ``ensure_sandbox()``；
2. **物化**：``data_sync.materialize_from_pg(ctx)``（PG securities + active portfolios →
   工具实读位 ``<ws>/sandbox/repo``）；
3. 读 ``portfolio/{pid}.json``（不存在 → ``NOT_FOUND``）与 ``market/daily_bars.json``；
4. **参数兜底校验**（端点已同步校验一遍；此处再校验，保证任意调用方一致）：
   ``rebalance ∈ REBALANCE_CHOICES``、``start ≤ end``、``benchmark_secid`` 存在于 bars；
5. ``applib.backtest.run_backtest``（T3.1 已参数化）→ ``BT.write_all`` 产物三件套
   （``backtest.{json,md,html}``）→ 上传产物（MinIO）→ 写 ``runs`` 记录；
6. 返回 ``{"run_uid", "artifact_keys", "summary"}``（落 ``jobs.result``）。

summary 字段冻结（§6.2「核心指标含 strategy/benchmark/excess」，供 T6.5 前端直接读）
------------------------------------------------------------------------------
``strategy_metrics / benchmark{secid,name,metrics} / excess / rebalance / benchmark_secid /
start / end / n_days / n_rebalances / cost_bps / rf / cost``。
"""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from applib import backtest as BT
from applib import report as RP
from applib.ctx import Ctx, new_run_id
from sqlalchemy import select

from app.core.errors import AppError, ErrorCode
from app.db.session import session_scope
from app.models import Portfolio, Run
from app.services import artifacts


def _load(path: Path) -> Any:
    """读取 JSON 文件（UTF-8）。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _validate_params(bars: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """参数兜底校验并归一，返回喂给 ``run_backtest`` 的实参 dict。

    非法一律 ``AppError(VALIDATION_ERROR)``（422），不新增错误码。
    """
    rebalance = params.get("rebalance") or "monthly"
    if rebalance not in BT.REBALANCE_CHOICES:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"未知再平衡频率：{rebalance!r}",
            detail={"field": "rebalance", "allowed": list(BT.REBALANCE_CHOICES)},
        )

    start = params.get("start")
    end = params.get("end")
    if start and end and start > end:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "start 不得晚于 end",
            detail={"field": "start", "start": start, "end": end},
        )

    benchmark_secid = params.get("benchmark_secid") or "510300.SH"
    if benchmark_secid not in bars:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"基准标的不在交易日线中：{benchmark_secid}",
            detail={
                "field": "benchmark_secid",
                "benchmark_secid": benchmark_secid,
                "examples": sorted(bars)[:5],
            },
        )

    rf = params.get("rf")
    rf = 0.0 if rf is None else float(rf)
    cost_bps = params.get("cost_bps")
    cost_bps = BT.DEFAULT_COST_BPS if cost_bps is None else float(cost_bps)

    return {
        "start": start,
        "end": end,
        "rf": rf,
        "rebalance": rebalance,
        "benchmark_secid": benchmark_secid,
        "cost_bps": cost_bps,
    }


def _build_summary(bt: dict[str, Any]) -> dict[str, Any]:
    """组装 ``runs.summary``（字段冻结，供 T6.5 前端直接读）。"""
    bench = bt.get("benchmark")
    return {
        "strategy_metrics": bt["strategy_metrics"],
        "benchmark": (
            {"secid": bench["secid"], "name": bench["name"], "metrics": bench["metrics"]}
            if bench
            else None
        ),
        "excess": bt["excess"],
        "rebalance": bt["rebalance"],
        "benchmark_secid": bt["benchmark_secid"],
        "start": bt["start"],
        "end": bt["end"],
        "n_days": bt["n_days"],
        "n_rebalances": bt["n_rebalances"],
        "cost_bps": bt["cost_bps"],
        "rf": bt["rf"],
        "cost": bt["cost"],
    }


def run_backtest_job(
    ws_root: str | Path,
    pid: str,
    params: dict[str, Any],
    job_id: str,
    progress: Callable[[int, str | None], None],
) -> dict[str, Any]:
    """回测 job 主体：物化 → 校验 → 回测 → 渲染 → 上传 → 写 runs。

    返回 ``{"run_uid", "artifact_keys", "summary"}``（落 ``jobs.result``）。
    """
    ctx = Ctx(root=Path(ws_root))
    ctx.ensure_sandbox()

    # 物化：PG securities + active portfolios → <ws>/sandbox/repo（applib 实读位）
    from app.services.data_sync import materialize_from_pg

    materialize_from_pg(ctx)

    pf_path = ctx.local_repo / "portfolio" / f"{pid}.json"
    if not pf_path.exists():
        raise AppError(ErrorCode.NOT_FOUND, f"组合不存在或未物化：{pid}")
    bars_path = ctx.local_repo / "market" / "daily_bars.json"
    if not bars_path.exists():
        raise AppError(ErrorCode.NOT_FOUND, "交易日线数据缺失（daily_bars.json）")
    pf = _load(pf_path)
    bars = _load(bars_path)

    p = _validate_params(bars, params)

    progress(20, "运行回测")
    bt = BT.run_backtest(
        bars,
        pf,
        start=p["start"],
        end=p["end"],
        rf=p["rf"],
        rebalance=p["rebalance"],
        benchmark_secid=p["benchmark_secid"],
        cost_bps=p["cost_bps"],
    )

    progress(70, "渲染产物")
    run_uid = new_run_id("bt")
    art_dir = ctx.artifacts_dir / run_uid
    BT.write_all(bt, art_dir, RP.CSS)

    progress(90, "上传产物")
    files = {f.name: f for f in art_dir.glob("*") if f.is_file()}
    keys = artifacts.put_run_artifacts(run_uid, files)

    summary = _build_summary(bt)

    with session_scope() as db:
        portfolio = db.execute(
            select(Portfolio).where(Portfolio.pid == pid)
        ).scalar_one_or_none()
        if portfolio is None:
            raise AppError(ErrorCode.NOT_FOUND, f"组合不存在：{pid}")
        db.add(
            Run(
                run_uid=run_uid,
                job_id=job_id,
                portfolio_id=portfolio.id,
                type="backtest",
                as_of=date.fromisoformat(bt["end"]),
                summary=summary,
                artifact_keys=keys,
            )
        )

    return {"run_uid": run_uid, "artifact_keys": keys, "summary": summary}

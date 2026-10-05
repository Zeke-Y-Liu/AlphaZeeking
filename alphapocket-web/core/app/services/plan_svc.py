"""调仓评估单 run 核心逻辑（T4.1，§6.2 ``POST /portfolios/{id}/rebalance-plan`` / §9.5）。

``run_plan_job`` 为异步 job 函数体（由 ``POST /portfolios/{id}/rebalance-plan`` 提交）：

1. ``Ctx(root=ws_root)`` + ``ensure_sandbox()`` + ``data_sync.materialize_from_pg(ctx)``
   （PG securities + active portfolios → 工具实读位 ``<ws>/sandbox/repo``）；
   载入 ``portfolio/{pid}.json``（缺失 → ``AppError(NOT_FOUND)``）；
2. ``applib.tools.portfolio_rebalance_plan`` → 写 ``rebalance_plan.json`` 产物 → 上传
   （MinIO）→ 写 ``runs``（``type="rebalance_plan"``）→ 写 ``rebalance_plans``
   （``status="draft"``）；均在同一 ``session_scope()`` 事务内完成；
3. **supersede 不变量（§7.4）**：新 plan 生成时，同 ``portfolio_id`` 下所有旧 ``draft``
   plan 一律置 ``superseded``。

契约缺陷登记（不单方面改契约，仅登记）
------------------------------------
- ``plan_uid`` 幂等策略：``applib`` 的 ``plan_id`` 形态为 ``RP-<YYYYMMDD>-<PID>``，
  同一 ``as_of`` 重复生成会产生同一 uid，触发 ``rebalance_plans.plan_uid`` 唯一约束。
  本服务在冲突时**追加 ``-2``/``-3``… 后缀**（保留新 plan，旧 plan 由 supersede 置为
  superseded），避免 500；``runs.summary.plan_uid`` 仍记录 applib 原始 ``plan_id``。
- ``cost_est_cny`` 口径：按名义资金 ¥1,000,000 与执行单同费率逐笔求和（见 T4.1 的
  C2 修复），与 ``exec_orders`` 按价格/整手实算的结果存在小额取整差（plan 不做整手换算）。
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from applib import tools
from applib.ctx import Ctx, new_run_id
from sqlalchemy import select, update

from app.core.errors import AppError, ErrorCode
from app.db.session import session_scope
from app.models import Portfolio, RebalancePlan, Run
from app.services import artifacts

logger = logging.getLogger("app.plan")


def _build_summary(plan: dict[str, Any]) -> dict[str, Any]:
    """组装 ``runs.summary``（字段冻结，供 T6.6 前端直接读）。"""
    return {
        "plan_uid": plan["plan_id"],
        "as_of": plan["as_of"],
        "turnover_est": plan["turnover_est"],
        "cost_est_cny": plan["cost_est_cny"],
        "n_trades": len(plan.get("diff_trades") or []),
    }


def _unique_plan_uid(db: Any, base: str) -> str:
    """解析可用的 ``plan_uid``：冲突（同 as_of 重复生成）追加 ``-N`` 后缀。"""
    uid = base
    n = 1
    while db.execute(select(RebalancePlan.id).where(RebalancePlan.plan_uid == uid)).first():
        n += 1
        uid = f"{base}-{n}"
    return uid


def run_plan_job(
    ws_root: str | Path,
    pid: str,
    job_id: str,
    progress: Callable[[int, str | None], None],
) -> dict[str, Any]:
    """调仓评估单 job 主体：物化 → 生成 plan → 上传 → supersede → 写 runs + rebalance_plans。

    返回 ``{"plan_uid", "plan_id", "artifact_keys", "summary"}``（落 ``jobs.result``）。
    """
    ctx = Ctx(root=Path(ws_root))
    ctx.ensure_sandbox()

    # 物化：PG securities + active portfolios → <ws>/sandbox/repo（applib 实读位）
    from app.services.data_sync import materialize_from_pg

    materialize_from_pg(ctx)
    if not (ctx.local_repo / "portfolio" / f"{pid}.json").exists():
        raise AppError(ErrorCode.NOT_FOUND, f"组合不存在或未物化：{pid}")

    # 引擎 A/B → 拟调仓评估单
    progress(40, "生成调仓评估单")
    plan = tools.portfolio_rebalance_plan({"portfolio": pid}, ctx)
    if "error" in plan:
        raise AppError(ErrorCode.INTERNAL_ERROR, str(plan["error"]))

    # 产物（至少含 rebalance_plan.json）
    progress(70, "渲染产物")
    run_uid = new_run_id("rp")
    art_dir = ctx.artifacts_dir / run_uid
    art_dir.mkdir(parents=True, exist_ok=True)
    (art_dir / "rebalance_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    progress(90, "上传产物")
    files = {p.name: p for p in sorted(art_dir.glob("*")) if p.is_file()}
    keys = artifacts.put_run_artifacts(run_uid, files)

    summary = _build_summary(plan)
    as_of = date.fromisoformat(plan["as_of"]) if plan.get("as_of") else None

    # 单一事务：supersede → insert Run（flush 取 id）→ insert Plan
    with session_scope() as db:
        portfolio = db.execute(
            select(Portfolio).where(Portfolio.pid == pid)
        ).scalar_one_or_none()
        if portfolio is None:
            raise AppError(ErrorCode.NOT_FOUND, f"组合不存在：{pid}")

        db.execute(
            update(RebalancePlan)
            .where(
                RebalancePlan.portfolio_id == portfolio.id,
                RebalancePlan.status == "draft",
            )
            .values(status="superseded")
        )
        db.flush()

        run = Run(
            run_uid=run_uid,
            job_id=job_id,
            portfolio_id=portfolio.id,
            type="rebalance_plan",
            as_of=as_of,
            summary=summary,
            artifact_keys=keys,
        )
        db.add(run)
        db.flush()

        plan_uid = _unique_plan_uid(db, plan["plan_id"])
        db.add(
            RebalancePlan(
                plan_uid=plan_uid,
                portfolio_id=portfolio.id,
                run_id=run.id,
                status="draft",
                payload=plan,
            )
        )

    return {
        "plan_uid": plan_uid,
        "plan_id": plan["plan_id"],
        "artifact_keys": keys,
        "summary": summary,
    }

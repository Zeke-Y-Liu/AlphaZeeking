"""执行单业务逻辑（T4.2 / T4.3 / T4.4，§6.2 / §9.5）。

职责
----
1. **生成执行单（T4.2，同步非 job）**：``create_exec_order`` 以**最新 review 数据 +
   存量 plan payload** 机械换算（``applib.exec_orders.build_exec_orders``），产物
   （json/md/csv）入 MinIO ``runs/{exec_uid}/``，落 ``exec_orders``（``status="draft"``）。
2. **状态机（T4.3）**：``confirm``（强制 ``ack_discipline``）→ ``confirmed``；
   ``fill``（整手/方向校验，允许部分成交）→ ``executed``；``cancel`` → ``cancelled``。
   每次跃迁写 ``audit_log``（``entity="exec_order"``）；GET 详情的时间线即审计序列。
3. **历史与审计（T4.4）**：``rebalance_history`` / ``list_audit``。

不变量（§7）
------------
- **引擎永不直接动仓位**：本模块只做算术换算与状态/审计写入，无任何券商接口。
- ``confirm`` 必须显式 ``ack_discipline:true``（否则 422 ``ACK_REQUIRED``）。
- ``fill`` 允许部分成交：未登记满的差额在 ``fills.partial`` 标注，不引入复杂状态机。

契约登记（不单方面改契约）
--------------------------
- ``runs.job_id`` 为 NOT NULL 外键，而执行单生成是**同步**端点（§6.2 明确非 job）。
  为让产物复用 ``GET /runs/{run_uid}/artifacts/{file}`` 流式下载（§5.3 ``runs/`` 布局），
  本服务写入一条 ``status="done"`` 的占位 ``jobs`` 行再挂 ``runs``（不进入 JobRunner
  executor，不占 workspace 锁）。
- plan 过老（``plan.as_of`` ≠ 最新交易日即 ``review_now.as_of``）→ 409 ``DATA_STALE``
  （§9.5 T4.2）。§6.3 将 ``DATA_STALE`` 标注为「内部化处理」，但本端点同步返回、
  无法串联 sync job，故直接以 409 ``DATA_STALE`` 提示前端「先重跑评估」。
"""
from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from applib import exec_orders as EO
from applib import tools
from applib.ctx import Ctx, new_run_id
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.db.session import session_scope
from app.models import AuditLog, ExecOrder, Job, Portfolio, RebalancePlan, Run
from app.services import artifacts
from app.services.workspace import WorkspaceManager

WS_NAME = "default"
DEFAULT_CAPITAL_CNY = 1_000_000.0
AUDIT_ENTITY = "exec_order"

# 允许的成交登记步长（A 股 ETF/LOF 100 份/手）
LOT = EO.LOT

# 执行单状态机允许的跃迁
_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"confirmed", "cancelled"},
    "confirmed": {"executed", "cancelled"},
    "executed": set(),
    "cancelled": set(),
}


# ── 内部工具 ────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(UTC)


def _new_job_id() -> str:
    return f"job_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


def _write_audit(
    db: Session, exec_uid: str, action: str, payload: dict[str, Any], actor: str = "system"
) -> None:
    """写执行单审计（不提交，由调用方随事务提交）。"""
    db.add(
        AuditLog(
            entity=AUDIT_ENTITY,
            entity_id=exec_uid,
            action=action,
            actor=actor,
            payload=payload,
        )
    )


def _require_transition(row: ExecOrder, target: str) -> None:
    """校验状态跃迁合法，否则 422 VALIDATION_ERROR。"""
    allowed = _TRANSITIONS.get(row.status, set())
    if target not in allowed:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"状态跃迁非法：{row.status} → {target}",
            detail={"from": row.status, "to": target, "allowed": sorted(allowed)},
        )


# ── 查找 ────────────────────────────────────────────────────────


def get_plan_or_404(db: Session, plan_id: str) -> RebalancePlan:
    """``plan_id`` 为纯数字按主键 ``id`` 查，否则按 ``plan_uid`` 查；无 → 404。"""
    stmt = select(RebalancePlan)
    if plan_id.isdigit():
        stmt = stmt.where(RebalancePlan.id == int(plan_id))
    else:
        stmt = stmt.where(RebalancePlan.plan_uid == plan_id)
    plan = db.execute(stmt).scalar_one_or_none()
    if plan is None:
        raise AppError(ErrorCode.NOT_FOUND, f"调仓计划不存在：{plan_id}")
    return plan


def get_exec_or_404(db: Session, exec_id: str) -> ExecOrder:
    """``exec_id`` 为纯数字按主键 ``id`` 查，否则按 ``exec_uid`` 查；无 → 404。"""
    stmt = select(ExecOrder)
    if exec_id.isdigit():
        stmt = stmt.where(ExecOrder.id == int(exec_id))
    else:
        stmt = stmt.where(ExecOrder.exec_uid == exec_id)
    row = db.execute(stmt).scalar_one_or_none()
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, f"执行单不存在：{exec_id}")
    return row


def get_portfolio_or_404(db: Session, pid: str) -> Portfolio:
    """按 ``pid`` 取组合，无 → 404。"""
    portfolio = db.execute(select(Portfolio).where(Portfolio.pid == pid)).scalar_one_or_none()
    if portfolio is None:
        raise AppError(ErrorCode.NOT_FOUND, f"组合不存在：{pid}")
    return portfolio


# ── 生成（T4.2） ────────────────────────────────────────────────


def _review_now(ws_root: Path, pid: str) -> dict[str, Any]:
    """物化 PG → workspace，跑 ``portfolio_review`` 取「最新 review 数据」。"""
    ctx = Ctx(root=ws_root)
    ctx.ensure_sandbox()
    from app.services.data_sync import materialize_from_pg

    materialize_from_pg(ctx)
    if not (ctx.local_repo / "portfolio" / f"{pid}.json").exists():
        raise AppError(ErrorCode.NOT_FOUND, f"组合不存在或未物化：{pid}")
    review = tools.portfolio_review({"portfolio": pid}, ctx)
    if "error" in review:
        raise AppError(ErrorCode.INTERNAL_ERROR, str(review["error"]))
    return review


def create_exec_order(
    db: Session, settings: Settings, plan: RebalancePlan, capital_cny: float
) -> dict[str, Any]:
    """由存量 plan + 最新 review 机械换算生成执行单（同步）。"""
    portfolio = db.execute(
        select(Portfolio).where(Portfolio.id == plan.portfolio_id)
    ).scalar_one_or_none()
    if portfolio is None:
        raise AppError(ErrorCode.NOT_FOUND, f"组合不存在：{plan.portfolio_id}")

    ws_root = WorkspaceManager(settings).ensure(WS_NAME)
    review = _review_now(ws_root, portfolio.pid)

    plan_payload = plan.payload or {}
    plan_as_of = plan_payload.get("as_of")
    if plan_as_of and plan_as_of != review.get("as_of"):
        raise AppError(
            ErrorCode.DATA_STALE,
            f"调仓计划已过期（plan.as_of={plan_as_of}，最新交易日={review.get('as_of')}），请先重跑评估",
            detail={"plan_as_of": plan_as_of, "latest_as_of": review.get("as_of")},
        )

    sheet = EO.build_exec_orders(review, plan_payload, capital=capital_cny)

    # 产物：本地渲染 → 上传 MinIO（key=runs/{exec_uid}/exec_order.*）
    exec_uid = new_run_id("exec")
    ctx = Ctx(root=ws_root)
    art_dir = ctx.artifacts_dir / exec_uid
    EO.write_all(sheet, art_dir)
    files = {p.name: p for p in sorted(art_dir.glob("*")) if p.is_file()}
    keys = artifacts.put_run_artifacts(exec_uid, files)

    as_of = datetime.fromisoformat(review["as_of"]).date() if review.get("as_of") else None
    summary = {
        "exec_id": sheet["exec_id"],
        "plan_uid": plan.plan_uid,
        "capital_cny": capital_cny,
        "summary": sheet["summary"],
        "fees": sheet["fees"],
    }

    with session_scope() as s:
        job_id = _new_job_id()
        s.add(
            Job(
                id=job_id,
                type="exec_orders",
                portfolio_id=portfolio.id,
                status="done",
                params={"plan_id": plan.id, "capital_cny": capital_cny},
                progress=100,
                result={"exec_uid": exec_uid, "summary": sheet["summary"]},
                finished_at=_now(),
            )
        )
        s.flush()
        run = Run(
            run_uid=exec_uid,
            job_id=job_id,
            portfolio_id=portfolio.id,
            type="exec_orders",
            as_of=as_of,
            summary=summary,
            artifact_keys=keys,
        )
        s.add(run)
        s.flush()
        row = ExecOrder(
            exec_uid=exec_uid,
            plan_id=plan.id,
            portfolio_id=portfolio.id,
            capital_cny=Decimal(str(capital_cny)),
            status="draft",
            orders=sheet,
        )
        s.add(row)
        s.flush()
        _write_audit(
            s,
            exec_uid,
            "create",
            {"plan_uid": plan.plan_uid, "capital_cny": capital_cny, "run_uid": exec_uid},
        )
        row_id = row.id

    return detail(db, get_exec_or_404(db, str(row_id)))


# ── 状态机（T4.3） ──────────────────────────────────────────────


def confirm(db: Session, row: ExecOrder, ack_discipline: bool, actor: str = "system") -> dict[str, Any]:
    """确认执行单：强制 ``ack_discipline:true``；``draft`` → ``confirmed``。"""
    if not ack_discipline:
        raise AppError(
            ErrorCode.ACK_REQUIRED,
            "确认执行单必须显式确认纪律声明（ack_discipline:true）",
            detail={"field": "ack_discipline"},
        )
    _require_transition(row, "confirmed")
    row.status = "confirmed"
    row.confirmed_at = _now()
    row.confirmed_by = actor
    _write_audit(db, row.exec_uid, "confirm", {"ack_discipline": True}, actor=actor)
    db.commit()
    db.refresh(row)
    return detail(db, row)


def register_fills(
    db: Session, row: ExecOrder, fills: list[dict[str, Any]], actor: str = "system"
) -> dict[str, Any]:
    """登记成交：校验后状态 → ``executed``（允许部分成交，标注 ``partial``）。"""
    if row.status != "confirmed":
        _require_transition(row, "executed")

    sheet = row.orders or {}
    normalized, partial = _validate_fills(sheet, fills)
    total_amount = round(sum(f["amount"] for f in normalized), 2)

    row.fills = {
        "items": normalized,
        "n": len(normalized),
        "total_amount": total_amount,
        "partial": partial,
    }
    row.status = "executed"
    row.filled_at = _now()
    _write_audit(
        db,
        row.exec_uid,
        "fill",
        {"n": len(normalized), "total_amount": total_amount, "partial": partial},
        actor=actor,
    )
    db.commit()
    db.refresh(row)
    return detail(db, row)


def cancel(db: Session, row: ExecOrder, reason: str | None = None, actor: str = "system") -> dict[str, Any]:
    """取消执行单：``draft`` / ``confirmed`` → ``cancelled``（``executed`` 不可取消）。"""
    _require_transition(row, "cancelled")
    row.status = "cancelled"
    _write_audit(db, row.exec_uid, "cancel", {"reason": reason}, actor=actor)
    db.commit()
    db.refresh(row)
    return detail(db, row)


def _validate_fills(sheet: dict[str, Any], fills: list[dict[str, Any]]) -> tuple[list[dict], bool]:
    """校验成交登记：secid∈单内且可委托、price>0、数量为 100 整数倍且方向一致。

    返回 ``(normalized, partial)``；``partial`` 表示存在未登记满的委托。
    """
    if not fills:
        raise AppError(ErrorCode.VALIDATION_ERROR, "fills 不能为空", detail={"field": "fills"})

    orders = {o["secid"]: o for o in sheet.get("orders") or []}
    normalized: list[dict[str, Any]] = []
    filled: dict[str, int] = {}

    for idx, f in enumerate(fills):
        secid = f.get("secid")
        order = orders.get(secid)
        if order is None:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                f"标的不在执行单内：{secid}",
                detail={"index": idx, "secid": secid},
            )
        order_shares = int(order["order_shares"])
        if order_shares == 0:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                f"标的无委托，不能登记成交：{secid}",
                detail={"index": idx, "secid": secid, "order_shares": 0},
            )

        price = float(f.get("price") or 0)
        if price <= 0:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                f"成交价格必须大于 0：{secid}",
                detail={"index": idx, "secid": secid, "price": price},
            )

        shares = int(f.get("shares") or 0)
        if shares == 0 or shares % LOT != 0:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                f"成交数量须为 {LOT} 的整数倍：{secid}",
                detail={"index": idx, "secid": secid, "shares": shares, "lot": LOT},
            )
        if (shares > 0) != (order_shares > 0):
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                f"成交方向与委托不一致：{secid}（委托 {order_shares}）",
                detail={
                    "index": idx,
                    "secid": secid,
                    "shares": shares,
                    "order_shares": order_shares,
                    "direction": order["direction"],
                },
            )

        amount = round(abs(shares) * price, 2)
        normalized.append(
            {
                "secid": secid,
                "name": order["name"],
                "direction": order["direction"],
                "price": price,
                "shares": shares,
                "order_shares": order_shares,
                "amount": amount,
            }
        )
        filled[secid] = filled.get(secid, 0) + abs(shares)

    partial = any(filled[secid] < abs(orders[secid]["order_shares"]) for secid in filled)
    return normalized, partial


# ── 读取（T4.2 GET / T4.4 历史） ────────────────────────────────


def _timeline(db: Session, exec_uid: str) -> list[dict[str, Any]]:
    """执行单时间线（审计序列，按创建时间升序）。"""
    rows = (
        db.execute(
            select(AuditLog)
            .where(AuditLog.entity == AUDIT_ENTITY, AuditLog.entity_id == exec_uid)
            .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        )
        .scalars()
        .all()
    )
    return [
        {
            "at": r.created_at.isoformat() if r.created_at else None,
            "action": r.action,
            "actor": r.actor,
            "payload": r.payload,
        }
        for r in rows
    ]


def detail(db: Session, row: ExecOrder) -> dict[str, Any]:
    """执行单详情（含完整 sheet 与状态时间线）。"""
    plan = db.execute(
        select(RebalancePlan).where(RebalancePlan.id == row.plan_id)
    ).scalar_one_or_none()
    return {
        "id": row.id,
        "exec_uid": row.exec_uid,
        "plan_id": row.plan_id,
        "plan_uid": plan.plan_uid if plan else None,
        "portfolio_id": row.portfolio_id,
        "capital_cny": float(row.capital_cny),
        "status": row.status,
        "created_at": row.created_at,
        "confirmed_at": row.confirmed_at,
        "confirmed_by": row.confirmed_by,
        "filled_at": row.filled_at,
        "fills": row.fills,
        "sheet": row.orders,
        "timeline": _timeline(db, row.exec_uid),
    }


def list_for_portfolio(db: Session, portfolio_id: int) -> list[dict[str, Any]]:
    """该组合的执行单列表（按创建时间倒序，不含大 sheet）。"""
    rows = (
        db.execute(
            select(ExecOrder)
            .where(ExecOrder.portfolio_id == portfolio_id)
            .order_by(ExecOrder.created_at.desc(), ExecOrder.id.desc())
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": r.id,
            "exec_uid": r.exec_uid,
            "plan_id": r.plan_id,
            "portfolio_id": r.portfolio_id,
            "capital_cny": float(r.capital_cny),
            "status": r.status,
            "created_at": r.created_at,
            "confirmed_at": r.confirmed_at,
            "filled_at": r.filled_at,
        }
        for r in rows
    ]


def rebalance_history(db: Session, pid: str) -> dict[str, Any]:
    """调仓历史：plans + exec_orders + fills 归一化时间线（§9.5 T4.4）。"""
    portfolio = get_portfolio_or_404(db, pid)

    plans = (
        db.execute(
            select(RebalancePlan)
            .where(RebalancePlan.portfolio_id == portfolio.id)
            .order_by(RebalancePlan.created_at.asc(), RebalancePlan.id.asc())
        )
        .scalars()
        .all()
    )
    orders = (
        db.execute(
            select(ExecOrder)
            .where(ExecOrder.portfolio_id == portfolio.id)
            .order_by(ExecOrder.created_at.asc(), ExecOrder.id.asc())
        )
        .scalars()
        .all()
    )

    items: list[dict[str, Any]] = []
    for p in plans:
        items.append(
            {
                "kind": "plan",
                "at": p.created_at,
                "plan_uid": p.plan_uid,
                "plan_id": p.id,
                "status": p.status,
                "summary": {
                    "as_of": (p.payload or {}).get("as_of"),
                    "turnover_est": (p.payload or {}).get("turnover_est"),
                    "cost_est_cny": (p.payload or {}).get("cost_est_cny"),
                    "n_trades": len((p.payload or {}).get("diff_trades") or []),
                },
            }
        )
    for o in orders:
        sheet = o.orders or {}
        items.append(
            {
                "kind": "exec_order",
                "at": o.created_at,
                "exec_uid": o.exec_uid,
                "exec_id": sheet.get("exec_id"),
                "plan_id": o.plan_id,
                "status": o.status,
                "summary": sheet.get("summary"),
                "fees": sheet.get("fees"),
            }
        )
        if o.fills:
            items.append(
                {
                    "kind": "fills",
                    "at": o.filled_at or o.created_at,
                    "exec_uid": o.exec_uid,
                    "status": "executed",
                    "summary": {
                        "n": o.fills.get("n"),
                        "total_amount": o.fills.get("total_amount"),
                        "partial": o.fills.get("partial"),
                    },
                }
            )
    items.sort(key=lambda it: (it["at"] or datetime.min.replace(tzinfo=UTC), it["kind"]))
    return {"portfolio_id": portfolio.pid, "items": items, "count": len(items)}


def list_audit(
    db: Session, entity: str | None, entity_id: str | None, limit: int
) -> list[dict[str, Any]]:
    """审计列表查询（§9.5 T4.4）：按 ``entity`` / ``entity_id`` 过滤，时间倒序。"""
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit)
    if entity is not None:
        stmt = stmt.where(AuditLog.entity == entity)
    if entity_id is not None:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    rows = db.execute(stmt).scalars().all()
    return [
        {
            "id": r.id,
            "entity": r.entity,
            "entity_id": r.entity_id,
            "action": r.action,
            "actor": r.actor,
            "payload": r.payload,
            "created_at": r.created_at,
        }
        for r in rows
    ]

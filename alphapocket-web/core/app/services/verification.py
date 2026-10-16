"""六项验证执行、入库与验证门禁（T1.6，§4.4 / §5.1 verify_results / §6.3）。

职责
----
1. ``run_and_store``：在给定 workspace 上依次执行 applib ``VERIFY_SUITE`` 六项
   验证，逐项独立捕获异常（任一 check 异常仅记 ``fail``，绝不让同步 job 失败），
   幂等写入 PG ``verify_results``（先删该 job 旧行再插 6 行）。
2. ``verify_hook``：``data_sync.sync_workspace`` 的钩子适配器；写库失败只记日志、
   返回 None，**绝不使同步 job failed**。
3. ``latest_verify_summary`` / ``gate_or_raise``：门禁服务层，供 review / backtest /
   rebalance_plan 等计算任务前置校验最近一次 ``data_sync`` job 是否六项全绿。

detail 结构（冻结，T1.5 依赖）
------------------------------
``{"text": <原始字符串>, "count": <int|None>, "exempt": <int|None>,
  "max_diff": <float|None>, "sources": <int|None>, "error": <str|None>}``

通过语义与 α 一致：值为 ``"pass"`` 或 ``"pass(...)"`` 前缀为 pass，否则 fail
（豁免计数属 pass，不阻断）。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from applib import runner as applib_runner
from applib.ctx import Ctx
from sqlalchemy import delete, select

from app.core.errors import AppError, ErrorCode
from app.db.session import session_scope
from app.models import Job, VerifyResult

logger = logging.getLogger("app.verification")

# 六项验证顺序（冻结；与 applib.runner.VERIFY_SUITE 顺序一致）
CHECKS: tuple[str, ...] = (
    "pit_audit",
    "golden_test",
    "cross_source",
    "calendar_check",
    "adjust_check",
    "provenance_check",
)

GATE_MESSAGE = "数据验证未全绿，禁止计算任务；请先完成数据同步并查看验证明细"

# 数据风险提示（评估不阻断，仅提示；status 为 fail / unknown 时的 message）
DATA_RISK_MESSAGES: dict[str, str] = {
    "fail": "数据验证未全绿，评估结果可能失真；建议先完成数据同步并查看验证明细",
    "unknown": "尚未完成数据同步与验证，评估结果可能基于不完整数据",
}


def data_risk_of(summary: dict[str, Any]) -> dict[str, Any] | None:
    """把验证汇总转成数据风险提示载荷；全绿返回 ``None``。

    评估（review）不阻断门禁：``fail`` / ``unknown`` 均返回
    ``{"status", "message", "failed", "job_id"}``，供响应与 ``runs.summary.data_risk``
    下发前端展示。
    """
    if summary["status"] == "pass":
        return None
    return {
        "status": summary["status"],
        "message": DATA_RISK_MESSAGES.get(summary["status"], DATA_RISK_MESSAGES["fail"]),
        "failed": summary["failed"],
        "job_id": summary["job_id"],
    }


# ── detail 解析（尽力解析，绝不因解析失败报错）───────────────────


def _parse_detail(check: str, text: str) -> dict[str, Any]:
    """从验证结果原文尽力解析结构化 detail（解析不到置 None）。"""
    detail: dict[str, Any] = {
        "text": text,
        "count": None,
        "exempt": None,
        "max_diff": None,
        "sources": None,
        "error": None,
    }
    try:
        if check == "golden_test":
            m = re.search(r"pass\((\d+)\s*项对账", text)
            if m:
                detail["count"] = int(m.group(1))
        elif check == "cross_source":
            m = re.search(r"pass\((\d+)\s*点对账", text)
            if m:
                detail["count"] = int(m.group(1))
        elif check == "calendar_check":
            m = re.search(r"日历(\d+)\s*日", text)
            if m:
                detail["count"] = int(m.group(1))
            e = re.search(r"豁免\s*(\d+)", text)
            if e:
                detail["exempt"] = int(e.group(1))
        elif check == "adjust_check":
            m = re.search(r"pass\((\d+)\s*点", text)
            if m:
                detail["count"] = int(m.group(1))
            d = re.search(r"最大偏差([\d.]+)\s*%", text)
            if d:
                detail["max_diff"] = float(d.group(1)) / 100
        elif check == "provenance_check":
            m = re.search(r"pass\((\d+)\s*数据集", text)
            if m:
                detail["sources"] = int(m.group(1))
    except Exception:  # noqa: BLE001 - 解析失败仅丢失字段，绝不上抛
        logger.exception("verify detail 解析失败 check=%s text=%r", check, text)
    return detail


def _error_detail(exc: BaseException) -> dict[str, Any]:
    """check 抛异常时的 detail（含 error 字段，text 标注 ERROR 前缀）。"""
    text = f"ERROR: {type(exc).__name__}: {exc}"
    return {
        "text": text,
        "count": None,
        "exempt": None,
        "max_diff": None,
        "sources": None,
        "error": str(exc),
    }


def _extract(res: Any, default_check: str) -> tuple[str, str]:
    """从验证函数返回体取 (原始值字符串, check 名)。

    优先用返回 dict 的 key 作为 check 名（契约：返回 ``{"<check>": "<value>"}``）。
    """
    if isinstance(res, dict) and res:
        if default_check in res:
            value = res[default_check]
            return (value if isinstance(value, str) else str(value)), default_check
        if len(res) == 1:
            key = next(iter(res))
            value = res[key]
            return (value if isinstance(value, str) else str(value)), key
    return (res if isinstance(res, str) else str(res)), default_check


# ── 执行六项验证 ────────────────────────────────────────────────


def _run_checks(ctx: Ctx) -> list[dict[str, Any]]:
    """依次执行六项验证，逐个独立捕获异常，返回按 CHECKS 排序的结果行。"""
    rows: list[dict[str, Any]] = []
    for fn in applib_runner.VERIFY_SUITE:
        default_check = fn.__name__.lstrip("_")
        try:
            res = fn(ctx)
        except Exception as exc:  # noqa: BLE001 - 单 check 异常隔离，绝不上抛
            logger.warning("六项验证 %s 执行异常（记 fail）：%s", default_check, exc)
            rows.append({"check": default_check, "status": "fail", "detail": _error_detail(exc)})
            continue
        text, check = _extract(res, default_check)
        status = "pass" if (text == "pass" or text.startswith("pass(")) else "fail"
        rows.append({"check": check, "status": status, "detail": _parse_detail(check, text)})

    order = {name: idx for idx, name in enumerate(CHECKS)}
    rows.sort(key=lambda row: order.get(row["check"], len(CHECKS)))
    return rows


def run_and_store(ws_root: str | Path, job_id: str) -> list[dict[str, Any]]:
    """在 ``ws_root`` 上执行六项验证并幂等写入 ``verify_results``。

    - 每个 check 独立捕获异常 → 该行 ``status="fail"`` 且 detail 含 error；
    - 写库前删除该 ``job_id`` 旧行（幂等），一次 ``session_scope()`` 事务完成；
    - 返回 ``[{"check","status","detail"}, ...]``（顺序与 ``CHECKS`` 一致）。
    """
    ctx = Ctx(root=Path(ws_root))
    rows = _run_checks(ctx)
    with session_scope() as db:
        db.execute(delete(VerifyResult).where(VerifyResult.job_id == job_id))
        for row in rows:
            db.add(
                VerifyResult(
                    job_id=job_id,
                    check=row["check"],
                    status=row["status"],
                    detail=row["detail"],
                )
            )
    return rows


def verify_hook(*, ctx: Any, job_id: str | None, result: dict[str, Any]) -> list[dict[str, Any]] | None:
    """``data_sync.sync_workspace`` 末尾钩子适配器。

    ``job_id`` 为空 → 返回 None（不写库）。写库异常只记日志并返回 None，
    **绝不让同步 job 因验证写库失败而 failed**。
    """
    if not job_id:
        return None
    try:
        if getattr(ctx, "root", None) is not None:
            ws_root = Path(ctx.root)
        else:
            ws_root = Path(ctx.sandbox_dir).parent
        return run_and_store(ws_root, job_id)
    except Exception:  # noqa: BLE001 - 验证/写库失败不得影响同步 job
        logger.exception("六项验证写库失败（已忽略，不影响同步 job）job_id=%s", job_id)
        return None


# ── 门禁 ────────────────────────────────────────────────────────


def latest_verify_summary(db: Any) -> dict[str, Any]:
    """最近一次 ``data_sync`` job 的验证汇总（无任何记录 → unknown）。"""
    first = db.execute(
        select(VerifyResult.job_id, Job.created_at)
        .join(Job, VerifyResult.job_id == Job.id)
        .where(Job.type == "data_sync")
        .order_by(Job.created_at.desc(), VerifyResult.id.desc())
        .limit(1)
    ).first()
    if first is None:
        return {"status": "unknown", "job_id": None, "created_at": None, "checks": [], "failed": []}

    job_id, created_at = first
    rows = (
        db.execute(select(VerifyResult).where(VerifyResult.job_id == job_id).order_by(VerifyResult.id))
        .scalars()
        .all()
    )
    checks = [{"check": r.check, "status": r.status, "detail": r.detail} for r in rows]
    failed = [
        {"check": r.check, "text": (r.detail or {}).get("text")}
        for r in rows
        if r.status != "pass"
    ]
    status = "pass" if checks and all(r.status == "pass" for r in rows) else "fail"
    return {
        "status": status,
        "job_id": job_id,
        "created_at": created_at.isoformat() if created_at is not None else None,
        "checks": checks,
        "failed": failed,
    }


def gate_or_raise(db: Any, ws_name: str = "default") -> dict[str, Any]:
    """验证门禁：最近 sync 非全绿则抛 ``409 VERIFY_GATE_FAILED``，全绿返回 summary。

    ``ws_name`` 仅占位保留（当前实现不区分 workspace）。
    """
    summary = latest_verify_summary(db)
    if summary["status"] != "pass":
        raise AppError(
            ErrorCode.VERIFY_GATE_FAILED,
            GATE_MESSAGE,
            detail={
                "status": summary["status"],
                "reason": "no_verification" if summary["status"] == "unknown" else "failed_checks",
                "failed": summary["failed"],
                "job_id": summary["job_id"],
            },
        )
    return summary

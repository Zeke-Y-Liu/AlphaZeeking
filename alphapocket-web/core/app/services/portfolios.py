"""组合校验、物化与审计（T2.2）。

职责
----
1. **校验**（冻结契约 §6.2）：标的已注册 / 权重>0 / 组合内 secid 去重 /
   权重和 ∈ [99.5, 100.5]（越界附归一建议）；``equal_weight`` 时忽略入参权重等分。
2. **物化**：把组合写入 ``workspace_root/<ws>/repo/portfolio/{pid}.json``，形态与
   V4-α ``remote/repo/portfolio/*.json`` 逐字段对齐（applib 工具零改动直接读）。
3. **审计**：变更写 ``audit_log``（``entity="portfolio"``，``action∈{create,update,delete}``）。

物化 JSON 说明（与 §5.2 / α 契约对齐）
--------------------------------------
顶层：``id / name / base_ccy / strategy_binding / note / positions``；
持仓：``secid / name / market / asset_type / weight / trade_ccy / settle / cost``。
- ``weight`` 为**百分数**（``20.0`` 非 ``0.2``），Numeric(6,3) 三位小数；
- ``name/asset_type/settle/cost`` 由 ``securities`` 表补全（股票缺省亦按 seed 口径：
  ``settle="A股 T+1"``、``cost="印花税卖出0.05%+佣金"``）；
- ``market`` 取 ``securities.market``，``trade_ccy`` 取组合 ``base_ccy``（当前 CNY）；
- ``strategy_binding`` 取组合 ``strategy.binding``，缺省 ``multi_asset_v1``
  （§5.2「当前默认引擎B规则」；α multi5 即此值，cn4 为 ``cn4_quality_v1``，可由
  ``strategy`` 入参覆盖）。
"""
from __future__ import annotations

import json
import re
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.models import AuditLog, Portfolio, Position, Run, Security
from app.services.workspace import WorkspaceManager

# 权重和窗口（冻结）
WEIGHT_LOWER = Decimal("99.5")
WEIGHT_UPPER = Decimal("100.5")
# 权重小数位（对齐 Numeric(6,3)）
_QUANT = Decimal("0.001")
# 物化 JSON 缺省策略绑定（§5.2 默认引擎B规则）
DEFAULT_STRATEGY_BINDING = "multi_asset_v1"
# 审计实体名与操作者（β 单用户；待 T5.2 接入后替换为登录用户）
AUDIT_ENTITY = "portfolio"
AUDIT_ACTOR = "system"

_PID_AUTO_RE = re.compile(r"^pf_(\d+)$")


def _q(value: Decimal) -> Decimal:
    """量化到三位小数（四舍五入），与 ``Numeric(6,3)`` 落库精度一致。"""
    return value.quantize(_QUANT, rounding=ROUND_HALF_UP)


# ── 校验 ────────────────────────────────────────────────────────


def normalize_weights(
    raw: Sequence[tuple[str, float | None]], equal_weight: bool
) -> list[tuple[str, Decimal]]:
    """校验并返回 ``[(secid, weight)]``（百分数，三位小数）。

    校验顺序：去重 → 权重存在且>0 → 权重和窗口。标的注册校验另见
    :func:`validate_securities`（需 DB 查询，单独调用）。
    """
    if not raw:
        raise AppError(ErrorCode.VALIDATION_ERROR, "positions 不能为空", detail={"field": "positions"})

    # 1) 组合内 secid 去重
    seen: set[str] = set()
    duplicates: list[str] = []
    for secid, _ in raw:
        if secid in seen:
            duplicates.append(secid)
        else:
            seen.add(secid)
    if duplicates:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "组合内 secid 重复",
            detail={"field": "positions", "duplicate_secids": sorted(set(duplicates))},
        )

    # 2) 计算权重
    if equal_weight:
        n = len(raw)
        each = _q(Decimal(100) / n)
        weights = [(secid, each) for secid, _ in raw]
    else:
        weights = []
        for secid, w in raw:
            if w is None:
                raise AppError(
                    ErrorCode.VALIDATION_ERROR,
                    "缺少权重（未启用 equal_weight）",
                    detail={"field": "weight", "secid": secid},
                )
            dec = Decimal(str(w))
            if dec <= 0:
                raise AppError(
                    ErrorCode.VALIDATION_ERROR,
                    "权重必须大于 0",
                    detail={"field": "weight", "secid": secid, "value": float(dec)},
                )
            weights.append((secid, _q(dec)))

    # 3) 权重和 ∈ [99.5, 100.5]，越界附归一建议
    total = sum((w for _, w in weights), Decimal(0))
    if not (WEIGHT_LOWER <= total <= WEIGHT_UPPER):
        suggestion = [
            {"secid": secid, "weight": float(_q(w / total * 100))} for secid, w in weights
        ]
        raise AppError(
            ErrorCode.WEIGHT_NOT_NORMALIZED,
            f"权重和 {float(total)} 超出窗口 [{float(WEIGHT_LOWER)}, {float(WEIGHT_UPPER)}]",
            detail={
                "given_sum": float(total),
                "window": [float(WEIGHT_LOWER), float(WEIGHT_UPPER)],
                "suggestion": suggestion,
            },
        )
    return weights


def validate_securities(db: Session, secids: Sequence[str]) -> dict[str, Security]:
    """校验标的均已注册；返回 ``secid -> Security`` 映射。缺失 → 422 SECURITY_NOT_FOUND。"""
    rows = db.execute(select(Security).where(Security.secid.in_(list(secids)))).scalars().all()
    found = {row.secid: row for row in rows}
    missing = [secid for secid in secids if secid not in found]
    if missing:
        raise AppError(
            ErrorCode.SECURITY_NOT_FOUND,
            "标的不存在或未注册",
            detail={"missing_secids": missing},
        )
    return found


def suggest_pid(db: Session) -> str:
    """自动生成唯一短码 ``pf_<n>``（n = 现有 ``pf_<n>`` 最大编号 + 1）。"""
    existing = set(
        db.execute(select(Portfolio.pid).where(Portfolio.pid.like("pf_%"))).scalars().all()
    )
    nums = [int(m.group(1)) for pid in existing if (m := _PID_AUTO_RE.match(pid))]
    n = max(nums, default=0) + 1
    while f"pf_{n}" in existing:
        n += 1
    return f"pf_{n}"


def ensure_pid_available(db: Session, pid: str) -> None:
    """显式 pid 与既有组合冲突 → 422（含冲突说明）。"""
    exists = db.execute(select(Portfolio.id).where(Portfolio.pid == pid)).scalar_one_or_none()
    if exists is not None:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"pid 已存在：{pid}",
            detail={"field": "pid", "conflict": pid},
        )


# ── 物化 ────────────────────────────────────────────────────────


def strategy_binding(portfolio: Portfolio) -> str:
    """从 ``strategy`` 取绑定；缺省 ``multi_asset_v1``。"""
    strategy = portfolio.strategy or {}
    binding = strategy.get("binding") if isinstance(strategy, dict) else None
    return binding or DEFAULT_STRATEGY_BINDING


def build_materialized(
    portfolio: Portfolio,
    positions: Sequence[tuple[str, Decimal]],
    secmap: dict[str, Security],
) -> dict[str, Any]:
    """按 §5.2 / α 契约构造物化字典。"""
    return {
        "id": portfolio.pid,
        "name": portfolio.name,
        "base_ccy": portfolio.base_ccy,
        "strategy_binding": strategy_binding(portfolio),
        "note": portfolio.note or "",
        "positions": [
            {
                "secid": secid,
                "name": secmap[secid].name,
                "market": secmap[secid].market,
                "asset_type": secmap[secid].asset_type,
                "weight": float(_q(weight)),
                "trade_ccy": portfolio.base_ccy,
                "settle": secmap[secid].settle,
                "cost": secmap[secid].cost,
            }
            for secid, weight in positions
        ],
    }


def materialize(
    portfolio: Portfolio,
    positions: Sequence[tuple[str, Decimal]],
    secmap: dict[str, Security],
    settings: Settings,
    ws_name: str = "default",
) -> Path:
    """写 ``workspace_root/<ws>/repo/portfolio/{pid}.json``，返回文件路径。"""
    root = WorkspaceManager(settings).ensure(ws_name)
    target_dir = root / "repo" / "portfolio"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{portfolio.pid}.json"
    data = build_materialized(portfolio, positions, secmap)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


# ── 审计 ────────────────────────────────────────────────────────


def write_audit(db: Session, pid: str, action: str, payload: dict[str, Any]) -> None:
    """写组合变更审计（不提交，由调用方随事务提交）。"""
    db.add(
        AuditLog(
            entity=AUDIT_ENTITY,
            entity_id=pid,
            action=action,
            actor=AUDIT_ACTOR,
            payload=payload,
        )
    )


# ── 读取 ────────────────────────────────────────────────────────


def positions_of(db: Session, portfolio_id: int) -> list[tuple[str, Decimal]]:
    """按插入顺序返回组合持仓 ``[(secid, weight)]``。"""
    rows = db.execute(
        select(Position.secid, Position.weight)
        .where(Position.portfolio_id == portfolio_id)
        .order_by(Position.id)
    ).all()
    return [(secid, weight) for secid, weight in rows]


def position_count(db: Session, portfolio_id: int) -> int:
    """组合持仓数。"""
    return int(
        db.execute(
            select(func.count()).select_from(Position).where(Position.portfolio_id == portfolio_id)
        ).scalar_one()
    )


def latest_run(db: Session, portfolio_id: int) -> dict[str, Any] | None:
    """组合最近一次 run 摘要（按 created_at/id 倒序取一条，读 ``summary``）。"""
    run = db.execute(
        select(Run)
        .where(Run.portfolio_id == portfolio_id)
        .order_by(Run.created_at.desc(), Run.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if run is None:
        return None
    return {
        "run_uid": run.run_uid,
        "type": run.type,
        "as_of": run.as_of,
        "created_at": run.created_at,
        "summary": run.summary,
    }


def position_details(
    db: Session, portfolio_id: int
) -> list[dict[str, Any]]:
    """详情用持仓（含 securities 补全字段），按插入顺序。"""
    rows = db.execute(
        select(Position, Security)
        .join(Security, Position.secid == Security.secid)
        .where(Position.portfolio_id == portfolio_id)
        .order_by(Position.id)
    ).all()
    return [
        {
            "secid": pos.secid,
            "weight": float(pos.weight),
            "name": sec.name,
            "asset_type": sec.asset_type,
            "settle": sec.settle,
            "cost": sec.cost,
        }
        for pos, sec in rows
    ]

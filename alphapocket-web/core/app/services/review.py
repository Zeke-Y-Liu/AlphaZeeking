"""评估 run 核心逻辑（T2.3，§6.2 ``POST /portfolios/{id}/review`` / §9.3）。

职责
----
``run_review`` 为异步 job 函数体（由 ``POST /portfolios/{id}/review`` 提交）：

1. ``Ctx(root=ws_root)`` + ``ensure_sandbox()``；
2. **物化**：``data_sync.materialize_from_pg(ctx)``（把 PG 的 securities + active
   portfolios 写到工具实读位 ``<ws>/sandbox/repo``；注意 T2.2 组合 CRUD 物化写的是
   ``<ws>/repo/portfolio/``，与 applib 实读位不一致，故 review 必须先物化再跑工具）；
   组合文件不存在 → ``AppError(NOT_FOUND)``；
3. **缺口自愈**（best-effort）：``_ensure_gap`` 在 ``review_auto_incremental`` 开关
   为真时，若持仓在 ``daily_bars.json`` 中缺 bar，则尝试增量同步；全程 try/except，
   失败只 ``logger.warning`` 不阻断；
4. ``portfolio_review`` / ``portfolio_rebalance_plan``（applib 工具）→ 渲染报告 →
   上传产物（MinIO）→ 写 ``runs`` 记录 → 返回 ``{run_uid, artifact_keys, summary}``。

β 阶段限制（登记，不单方面改契约）
---------------------------------
- ``_ensure_gap`` **直接调用** ``sync_workspace`` 未持 sync 全局锁（§4.2 限频/熔断
  与全局互斥）；当前 review job 持 compute 锁，缺口自愈与独立 sync job 之间缺少
  sync 锁保护。δ 阶段接入 ``WorkspaceManager.sync_lock()`` 后再收紧。
- ``summary.signal_states`` 口径修正：applib ``compute_beta`` 产出的 signal 项**无**
  ``state`` 字段，真实状态在 ``positions[].beta.state``（above/below/neutral）。故按
  持仓的 ``beta.state`` 计数；仅当持仓无 beta 结构时退回 ``signals[].state``。缺失/
  未知一律归 ``neutral``。
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from applib import report, tools
from applib.ctx import Ctx, new_run_id
from sqlalchemy import select

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.db.session import session_scope
from app.models import Portfolio, Run
from app.services import artifacts, verification

logger = logging.getLogger("app.review")

_SIGNAL_STATES = ("above", "below", "neutral")


def _ensure_gap(
    ctx: Ctx, pid: str, progress: Callable[[int, str | None], None]
) -> None:
    """缺口自愈（best-effort）：持仓缺 bar 时尝试增量同步；失败不阻断。

    仅当 ``review_auto_incremental`` 为真时执行；未持 sync 全局锁（见模块 docstring）。
    """
    try:
        if not get_settings().review_auto_incremental:
            return
        pf_path = ctx.local_repo / "portfolio" / f"{pid}.json"
        if not pf_path.exists():
            return
        pf = json.loads(pf_path.read_text(encoding="utf-8"))
        bars_path = ctx.local_repo / "market" / "daily_bars.json"
        bars = json.loads(bars_path.read_text(encoding="utf-8")) if bars_path.exists() else {}
        if not isinstance(bars, dict):
            bars = {}
        missing = [
            p["secid"]
            for p in pf.get("positions", [])
            if isinstance(p, dict) and p.get("secid") and not bars.get(p["secid"])
        ]
        if not missing:
            return
        logger.info("组合 %s 存在缺口标的 %s，尝试增量同步自愈", pid, missing)
        from app.services.data_sync import sync_workspace

        sync_workspace(ctx.root, mode="incremental", progress=progress)
    except Exception:  # noqa: BLE001 - 缺口自愈失败不得阻断 review
        logger.warning("缺口自愈失败（忽略，不阻断 review）pid=%s", pid, exc_info=True)


def _signal_states(review: dict[str, Any]) -> dict[str, int]:
    """按持仓状态计数（applib 真实状态在 ``positions[].beta.state``）。

    applib ``compute_beta`` 的 signal 项无 ``state`` 字段；故优先取
    ``positions[].beta.state``（above/below/neutral），仅当持仓无 beta 结构时退回
    ``signals[].state``。缺失/未知归 ``neutral``。
    """
    counts = dict.fromkeys(_SIGNAL_STATES, 0)
    states: list[str] = []
    for pos in review.get("positions") or []:
        beta = pos.get("beta") if isinstance(pos, dict) else None
        if isinstance(beta, dict) and beta.get("state"):
            states.append(beta["state"])
    if not states:
        for sig in review.get("signals") or []:
            state = sig.get("state") if isinstance(sig, dict) else None
            if state:
                states.append(state)
    for state in states:
        counts[state if state in counts else "neutral"] += 1
    return counts


def _macro_summary(review: dict[str, Any]) -> dict[str, Any]:
    """引擎C 精简摘要（字段冻结）：三市场档位 + 全局 + state；unavailable 原样。

    冻结字段：``status?`` / ``markets`` / ``global`` / ``global_score`` / ``state`` /
    ``as_of`` / ``error?``。引擎C 为市场级条件变量，不参与权重决策与门禁。
    """
    ml = review.get("macro_liquidity") or {}
    if not ml or ml.get("status") == "unavailable":    # 缺失/降级 → unavailable 原样
        return {"status": "unavailable", "error": ml.get("error")}
    markets = {}
    for mkt, m in (ml.get("markets") or {}).items():
        if isinstance(m, dict) and "level" in m:
            markets[mkt] = m["level"]
    g = ml.get("global") or {}
    return {
        "markets": markets,                        # {"A": 1, "HK": -1, "US": 0}
        "global": g.get("level"),                  # int | None
        "global_score": g.get("score"),            # float | None（保留两位由前端格式化）
        "state": g.get("state"),                   # neutral | tight_stress | easy_froth
        "as_of": ml.get("as_of"),
    }


def _index_trend_summary(review: dict[str, Any]) -> dict[str, Any]:
    """引擎D 精简摘要（字段冻结，2026-10-26）：四指数位置/趋势一屏；unavailable 原样。

    冻结字段：``status?`` / ``indexes`` / ``as_of`` / ``error?``，其中
    ``indexes = {指数id: {position, pos_q, trend, ret_20d, close, name, market}}``。
    引擎D 为市场级条件变量，不参与权重决策与门禁（纪律同引擎C）。
    """
    it = review.get("index_trend") or {}
    if not it or it.get("status") == "unavailable":    # 缺失/降级 → unavailable 原样
        return {"status": "unavailable", "error": (it or {}).get("error")}
    idx = {}
    for iid, e in (it.get("indexes") or {}).items():
        if not isinstance(e, dict) or e.get("status") not in (None, "active"):
            continue                                  # insufficient/unavailable/inactive 不入摘要
        idx[iid] = {"position": e.get("position"), "pos_q": e.get("pos_q"),
                    "trend": e.get("trend"), "ret_20d": e.get("ret_20d"),
                    "close": e.get("close"), "name": e.get("name"),
                    "market": e.get("market")}
    return {"indexes": idx, "as_of": it.get("as_of")}


def _build_summary(review: dict[str, Any]) -> dict[str, Any]:
    """组装 ``runs.summary``（字段冻结，供 T6.4 前端直接读）。

    ``data_risk`` 由 ``run_review`` 在落库前按当时验证状态补充（全绿为 ``None``）。
    """
    return {
        "as_of": review.get("as_of"),
        "portfolio_metrics": review.get("portfolio_metrics"),
        "position_count": len(review.get("positions") or []),
        "signal_count": len(review.get("signals") or []),
        "signal_states": _signal_states(review),
        "macro_liquidity": _macro_summary(review),
        "index_trend": _index_trend_summary(review),
    }


def run_review(
    ws_root: str | Path,
    pid: str,
    job_id: str,
    progress: Callable[[int, str | None], None],
) -> dict[str, Any]:
    """评估 job 主体：物化 → 缺口自愈 → 评估/调仓 → 渲染 → 上传 → 写 runs。

    返回 ``{"run_uid", "artifact_keys", "summary"}``（落 ``jobs.result``）。
    """
    ctx = Ctx(root=Path(ws_root))
    ctx.ensure_sandbox()

    # 2) 物化：PG securities + active portfolios → <ws>/sandbox/repo（applib 实读位）
    from app.services.data_sync import materialize_from_pg

    materialize_from_pg(ctx)
    if not (ctx.local_repo / "portfolio" / f"{pid}.json").exists():
        raise AppError(ErrorCode.NOT_FOUND, f"组合不存在或未物化：{pid}")

    # 3) 缺口自愈（best-effort）
    _ensure_gap(ctx, pid, progress)

    # 4) 引擎 A/B 评估
    progress(30, "引擎A/B 评估")
    review = tools.portfolio_review({"portfolio": pid}, ctx)
    if "error" in review:
        raise AppError(ErrorCode.INTERNAL_ERROR, str(review["error"]))

    # 5) 调仓建议（失败降级空 plan，报告尽力渲染）
    progress(60, "生成调仓建议")
    plan = tools.portfolio_rebalance_plan({"portfolio": pid}, ctx)
    if "error" in plan:
        logger.warning("调仓建议生成失败（降级空 plan）：%s", plan["error"])
        plan = {}

    # 6) 渲染报告
    progress(75, "渲染报告")
    run_uid = new_run_id("run")
    art_dir = ctx.artifacts_dir / run_uid
    art_dir.mkdir(parents=True, exist_ok=True)
    report.render_all(review, plan, ctx, run_uid, art_dir)

    # 7) 上传产物（失败向上抛 → job failed）
    progress(90, "上传产物")
    files = {p.name: p for p in sorted(art_dir.glob("*")) if p.is_file()}
    keys = artifacts.put_run_artifacts(run_uid, files)

    # 8) summary（字段冻结；data_risk 按评估时点验证状态补充，全绿为 None）
    summary = _build_summary(review)

    # 9) 写 runs
    as_of = date.fromisoformat(review["as_of"]) if review.get("as_of") else None
    with session_scope() as db:
        summary["data_risk"] = verification.data_risk_of(
            verification.latest_verify_summary(db)
        )
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
                type="review",
                as_of=as_of,
                summary=summary,
                artifact_keys=keys,
            )
        )

    # 10) 返回
    return {"run_uid": run_uid, "artifact_keys": keys, "summary": summary}

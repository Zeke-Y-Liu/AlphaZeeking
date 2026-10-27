"""今日结论聚合服务（2026-10-26 快速获取组合操作结论页）。

职责
----
把「每个组合最近一次 review run 的冻结 summary」（``runs.summary``，见
``review._build_summary``）翻译成一屏可读的中文 highlight 短句 + 整体倾向
（level），并附服务器当前时间与收盘自动评估状态，供 ``GET /api/v1/today``
直接返回。

设计纪律
--------
- **零新表**：全部数据取自现有 runs / jobs / portfolios 表——收盘后调度器
  （``scheduler.py``）已把各组合评估跑完落 runs，本模块只读聚合，页面不做
  任何计算；
- **纯函数可测**：``fmt_pct`` / ``build_highlights`` / ``build_level`` 不碰
  DB，``tests/test_today.py`` 直接对拍；
- **降级**：组合无 run → highlights 给「暂无评估」提示 + level="stale"，
  绝不抛错；summary 缺字段 → 对应短句跳过；
- **字段冻结**：只消费 ``review._build_summary`` 的冻结键（as_of /
  portfolio_metrics / position_count / signal_states / macro_liquidity /
  index_trend / data_risk）。

值域（对齐 applib 事实源）
--------------------------
- 引擎D ``index_trend.indexes[].position``：``high_zone`` / ``mid_zone`` /
  ``low_zone``；``trend``：``up`` / ``down`` / ``range``（factors.py）；
- 引擎C ``macro_liquidity.state``：``neutral`` / ``tight_stress`` /
  ``easy_froth``（review.py 冻结摘要）。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import Job, Portfolio, Run

logger = logging.getLogger("app.today")

# 中文映射（值域见模块 docstring）
_POS_ZH = {"high_zone": "高位", "mid_zone": "中位", "low_zone": "低位"}
_TREND_ZH = {"up": "上行", "down": "下行", "range": "震荡"}
_TREND_ARROW = {"up": "↑", "down": "↓", "range": "→"}
_MACRO_ZH = {"neutral": "中性", "tight_stress": "偏紧承压", "easy_froth": "宽松偏热"}


def fmt_pct(value: Any) -> str:
    """小数收益率 → ``"+1.23%"``；None/非法 → ``"—"``（正数带 + 号）。"""
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"
    return f"{v * 100:+.2f}%"


def build_highlights(summary: dict[str, Any] | None) -> list[str]:
    """冻结 summary → 中文 highlight 短句（按重要性排序，缺字段自动跳过）。

    顺序：数据风险警示 > 当日/近20日收益 > 信号倾向（操作建议）> 宏观流动性 >
    指数位置趋势。无 summary → 单条「暂无评估」提示。
    """
    if not summary:
        return ["暂无评估结论：等待收盘后自动评估，或到组合页手动触发评估"]
    highlights: list[str] = []

    # 1) 数据风险（最优先警示；全绿为 None）
    data_risk = summary.get("data_risk")
    if data_risk:
        highlights.append(f"数据验证未全绿：{data_risk}")

    # 2) 收益（portfolio_metrics 冻结键：day_ret / ret_20d）
    metrics = summary.get("portfolio_metrics") or {}
    parts = [f"今日 {fmt_pct(metrics.get('day_ret'))}"]
    ret_20d = metrics.get("ret_20d")
    if ret_20d is not None:
        parts.append(f"近20日 {fmt_pct(ret_20d)}")
    highlights.append("，".join(parts))

    # 3) 信号倾向（signal_states：above/below/neutral 计数）→ 操作建议
    states = summary.get("signal_states") or {}
    above = int(states.get("above") or 0)
    below = int(states.get("below") or 0)
    neutral = int(states.get("neutral") or 0)
    if not any((above, below, neutral)):
        highlights.append("持仓暂无有效信号，建议先补齐标的因子数据")
    elif above > below:
        highlights.append(
            f"信号偏多（{above} 上 / {below} 下 / {neutral} 中性），持仓可继续持有"
        )
    elif below > above:
        highlights.append(
            f"信号偏空（{above} 上 / {below} 下 / {neutral} 中性），关注减仓或对冲"
        )
    else:
        highlights.append(
            f"信号均衡（{above} 上 / {below} 下 / {neutral} 中性），维持现有仓位"
        )

    # 4) 宏观流动性（引擎C 精简摘要；unavailable 跳过）
    macro = summary.get("macro_liquidity") or {}
    state = macro.get("state")
    if macro.get("status") != "unavailable" and state:
        highlights.append(f"宏观流动性：{_MACRO_ZH.get(state, str(state))}")

    # 5) 指数位置趋势（引擎D 精简摘要；一条合并四指数）
    idx = (summary.get("index_trend") or {}).get("indexes") or {}
    if idx:
        chips = []
        for entry in idx.values():
            pos = _POS_ZH.get(entry.get("position"), "位置未知")
            trend = entry.get("trend")
            trend_zh = _TREND_ZH.get(trend, "趋势未知")
            arrow = _TREND_ARROW.get(trend, "·")
            chips.append(f"{entry.get('name') or '?'} {pos}{arrow}{trend_zh}")
        highlights.append("指数：" + "，".join(chips))

    return highlights


def build_level(summary: dict[str, Any] | None) -> str:
    """整体倾向四档：``good`` / ``bad`` / ``warn`` / ``flat`` / ``stale``。

    - 无 summary → ``stale``（页面灰显「暂无评估」）；
    - 数据风险 → ``warn``（黄牌优先于收益倾向）；
    - 当日收益与信号同向偏多 → ``good``；同向偏空 → ``bad``；其余 ``flat``。
    """
    if not summary:
        return "stale"
    if summary.get("data_risk"):
        return "warn"
    metrics = summary.get("portfolio_metrics") or {}
    states = summary.get("signal_states") or {}
    day_ret = metrics.get("day_ret")
    above = int(states.get("above") or 0)
    below = int(states.get("below") or 0)
    if day_ret is None:
        return "flat"
    if day_ret >= 0 and above > below:
        return "good"
    if day_ret < 0 and below > above:
        return "bad"
    return "flat"


def _latest_review(db: Session, portfolio_id: int) -> Run | None:
    """该组合最近一次 review run（created_at desc，同 GET /runs 排序口径）。"""
    stmt = (
        select(Run)
        .where(Run.type == "review", Run.portfolio_id == portfolio_id)
        .order_by(Run.created_at.desc(), Run.id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def build_auto_status(db: Session, settings: Settings, today) -> dict[str, Any]:
    """收盘自动评估状态（页面横幅：是否开启 / 今日同步与评估进度）。

    未开启 → ``{"enabled": False}``；开启 → 附触发时点、今日 close_auto 增量
    同步状态、今日 close_auto review job 计数（含 failed，供用户判断是否要
    手动补跑）。
    """
    if not settings.auto_review_enabled:
        return {"enabled": False}
    from app.services.scheduler import TRIGGER, close_auto_sync_status

    sync = close_auto_sync_status(db, today)
    rows = db.execute(
        select(Job.status)
        .where(
            Job.type == "review",
            Job.params["trigger"].as_string() == TRIGGER,
            Job.params["date"].as_string() == today.isoformat(),
        )
    ).scalars().all()
    done = sum(1 for s in rows if s == "done")
    failed = sum(1 for s in rows if s == "failed")
    return {
        "enabled": True,
        "fire_time": settings.auto_review_time,
        "sync": sync,                  # None/queued/running/done/failed
        "review_submitted": len(rows),
        "review_done": done,
        "review_failed": failed,
    }


def build_today(db: Session, settings: Settings, now: datetime) -> dict[str, Any]:
    """聚合全部 active 组合的今日结论（``GET /api/v1/today`` 响应体）。

    - ``server_time``/``generated_at``：ISO 本地时区（前端时钟校准 + 缓存调试）；
    - ``portfolios[]``：每组合最近一次 review 的 highlights/level/metrics 精简
      投影 + ``fresh``（as_of 是否等于今天）；
    - ``auto_review``：调度状态（见 ``build_auto_status``）。
    """
    today = now.date()
    portfolios = db.execute(
        select(Portfolio.id, Portfolio.pid, Portfolio.name).where(
            Portfolio.status == "active"
        )
    ).all()

    items: list[dict[str, Any]] = []
    for pf_id, pid, name in portfolios:
        run = _latest_review(db, pf_id)
        summary = run.summary if run is not None else None
        metrics = (summary or {}).get("portfolio_metrics")
        items.append(
            {
                "portfolio_id": pf_id,
                "pid": pid,
                "name": name,
                "run_uid": run.run_uid if run is not None else None,
                "as_of": run.as_of.isoformat() if run is not None and run.as_of else None,
                "created_at": (
                    run.created_at.isoformat() if run is not None and run.created_at else None
                ),
                "fresh": bool(run is not None and run.as_of and run.as_of == today),
                "position_count": (summary or {}).get("position_count"),
                "metrics": metrics,
                "signal_states": (summary or {}).get("signal_states"),
                "highlights": build_highlights(summary),
                "level": build_level(summary),
            }
        )

    iso_now = now.isoformat(timespec="seconds")
    return {
        "server_time": iso_now,
        "date": today.isoformat(),
        "generated_at": iso_now,
        "auto_review": build_auto_status(db, settings, today),
        "portfolios": items,
    }

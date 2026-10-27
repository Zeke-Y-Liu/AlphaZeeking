"""收盘后自动评估调度器（2026-10-26 今日结论页配套）。

语义
----
每个交易日收盘后（``AP_AUTO_REVIEW_TIME``，默认 16:30 本地时区）分两阶段：

1. **数据同步**：提交一个增量同步 job（``type="data_sync"``，与
   ``POST /data/sync {mode: incremental}`` 同链路）拉取当日行情/指数/宏观数据；
   同步未完成前不进入评估（tick 轮询其状态）；
2. **逐组合评估**：同步 done/failed 后，对全部 ``active`` 组合逐个提交 review
   job，使「今日结论页」（``GET /api/v1/today``）随时取到当日最新评估。

纪律（对齐 §6.1 锁语义）：
- 复用 ``JobRunner.submit``（sync 全局锁 / compute 互斥锁）——锁冲突抛
  ``JOB_LOCKED``，调度器吞掉并在下一 tick 重试，绝不排队、绝不绕开锁直接调
  applib；
- 幂等标记：job ``params.trigger="close_auto"`` + ``params.date=YYYY-MM-DD``。
  一天至多一次同步 + 一天一组合至多一次评估（任意状态含 failed 均视为已尝试
  ——避免坏数据死循环重试，失败可见于 jobs 表，人工复盘）；
- 上游熔断（``circuit_open``）期间静默跳过，下轮 tick 重试；
- 交易日判断：读 workspace ``remote/repo/market/trade_calendar.json``（A 股
  日历，与 ``runner._calendar_check`` 同源）；文件缺失/损坏 → 退化为每天尝试
  （非交易日跑一次评估无害：as_of 不前移，仅产生一条冗余 run）；
- 默认关闭（``AP_AUTO_REVIEW=0``）：测试/离线环境零副作用；生产显式开启。

决策逻辑拆为纯函数（``parse_fire_time`` / ``is_trading_day`` / ``pending_pids``
/ ``phase``），``tests/test_today.py`` 直接对拍；线程壳只做「tick → 查询 →
submit」。
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.db.session import session_scope
from app.models import Job, Portfolio

logger = logging.getLogger("app.scheduler")

TICK_SECONDS = 60
TRIGGER = "close_auto"


def parse_fire_time(value: str) -> tuple[int, int]:
    """``"HH:MM"`` → ``(HH, MM)``；非法格式按 23:59 兜底（当日不再触发）。"""
    try:
        hh, mm = value.strip().split(":")
        h, m = int(hh), int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except (ValueError, AttributeError):
        pass
    logger.warning("auto_review_time 非法：%r（按 23:59 兜底）", value)
    return 23, 59


def is_trading_day(ws_root: Path, today: date) -> bool:
    """A 股交易日判断（与 ``runner._calendar_check`` 同源的日历文件）。

    日历缺失/损坏/当日无记录 → True（退化为每天尝试，见模块 docstring）。
    日历形态：``{"2026-01-02": 1, ...}``（值为交易日标志）或日期列表。
    """
    cal_path = ws_root / "remote" / "repo" / "market" / "trade_calendar.json"
    try:
        raw = json.loads(cal_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    key = today.isoformat()
    if isinstance(raw, dict):
        if key not in raw:
            return True                      # 日历未覆盖（旧快照）→ 不阻断
        v = raw[key]
        return bool(v) if not isinstance(v, (int, str)) else str(v) not in ("0", "false", "")
    if isinstance(raw, list):
        return key in raw or not raw         # 覆盖即按内容；空表退化
    return True


def pending_pids(
    now: datetime,
    fire_at: tuple[int, int],
    trading_day: bool,
    portfolios: list[tuple[int, str]],      # [(id, pid)]，active 组合
    done_ids: set[int],                     # 今日已提交过 close_auto 的 portfolio_id
) -> list[str]:
    """决策纯函数：返回本轮应提交的 ``pid`` 列表。

    时间窗（``now`` 为本地时区）未到 / 非交易日 / 全部已跑 → 空列表。
    调度器每 tick 用「全部 active − 今日已跑」做增量推进：锁冲突的组合
    下轮 tick 自然重试，跑完的组合不再重复。
    """
    if not portfolios:
        return []
    if not trading_day:
        return []
    if (now.hour, now.minute) < fire_at:
        return []
    return [pid for pid_id, pid in portfolios if pid_id not in done_ids]


def phase(sync_status: str | None) -> str:
    """两阶段决策纯函数：今日 close_auto 同步状态 → 本轮动作。

    - ``None`` → ``"sync"``：今日尚未提交同步，先提交增量同步；
    - ``"queued"`` / ``"running"`` → ``"wait"``：同步进行中，本轮只轮询；
    - ``"done"`` / ``"failed"`` → ``"review"``：同步终态后进入逐组合评估。
      （``failed`` 不阻断评估：增量同步失败时评估按既有数据出结论
      ——``as_of`` 不前移、``review_auto_incremental`` 缺口自愈兜底，
      好过当天整页无结论。）
    """
    if sync_status is None:
        return "sync"
    if sync_status in ("queued", "running"):
        return "wait"
    return "review"


def closed_auto_job_ids(db: Any, today: date) -> set[int]:
    """今日已存在 close_auto review job 的 ``portfolio_id`` 集合（幂等差集输入）。

    任意状态（queued/running/done/failed）均算已尝试——failed 不重试（防死循环），
    失败详情在 jobs 表可查。``params`` 形态由本模块 submit 时写入：
    ``{"pid": ..., "trigger": "close_auto", "date": "YYYY-MM-DD"}``。
    """
    stmt = (
        select(Job.portfolio_id)
        .where(
            Job.type == "review",
            Job.params["trigger"].as_string() == TRIGGER,
            Job.params["date"].as_string() == today.isoformat(),
        )
    )
    rows = db.execute(stmt).scalars().all()
    return {pid for pid in rows if pid is not None}


def close_auto_sync_status(db: Any, today: date) -> str | None:
    """今日 close_auto 同步 job 的状态（``data_sync`` 类型幂等查询）。

    返回 ``None``（尚未提交）/ ``"queued"`` / ``"running"`` / ``"done"`` /
    ``"failed"``。多个同日记录时取最新一条。
    """
    stmt = (
        select(Job.status)
        .where(
            Job.type == "data_sync",
            Job.params["trigger"].as_string() == TRIGGER,
            Job.params["date"].as_string() == today.isoformat(),
        )
        .order_by(Job.id.desc())
        .limit(1)
    )
    row = db.execute(stmt).scalar_one_or_none()
    return row


def submit_sync(runner: Any, today: date) -> str | None:
    """提交 close_auto 增量同步 job（与 ``POST /data/sync`` 同链路）。

    sync 锁被占（JOB_LOCKED）/ 熔断开（UPSTREAM_RATE_LIMITED）→ None（下轮重试）。
    """
    from app.services.data_sync import circuit_open, sync_workspace

    if circuit_open():
        return None
    try:
        return runner.submit(
            type="data_sync",
            params={
                "mode": "incremental",
                "trigger": TRIGGER,
                "date": today.isoformat(),
            },
            portfolio_id=None,
            fn=lambda ctx: sync_workspace(
                ctx.ws_root,
                mode="incremental",
                progress=ctx.report,
                job_id=ctx.job_id,
            ),
            lock="sync",
        )
    except AppError as exc:
        if exc.code is ErrorCode.JOB_LOCKED:
            return None
        raise


def submit_one(runner: Any, portfolio_id: int, pid: str, today: date) -> str | None:
    """提交单组合 close_auto review job；锁冲突（JOB_LOCKED）返回 None。

    与 ``POST /portfolios/{pid}/review`` 同构（``fn/lock/ws_name`` 一致），
    仅 params 追加幂等标记。
    """
    from app.services.review import run_review

    try:
        return runner.submit(
            type="review",
            params={"pid": pid, "trigger": TRIGGER, "date": today.isoformat()},
            portfolio_id=portfolio_id,
            fn=lambda ctx: run_review(ctx.ws_root, pid, ctx.job_id, ctx.report),
            lock="compute",
            ws_name="default",
        )
    except AppError as exc:
        if exc.code is ErrorCode.JOB_LOCKED:
            return None                          # 下一 tick 重试该组合
        raise


class CloseReviewScheduler:
    """收盘后自动评估的 daemon 线程壳：tick → 决策 → 提交（每轮至多全量 try）。"""

    def __init__(self, settings: Settings, runner: Any, ws_root: Path) -> None:
        self._settings = settings
        self._runner = runner
        self._ws_root = ws_root
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name="ap-close-review", daemon=True
        )

    # ── 生命周期 ──────────────────────────────────────────────
    def start(self) -> None:
        fire_at = parse_fire_time(self._settings.auto_review_time)
        logger.info(
            "收盘自动评估调度器启动：触发时点 %02d:%02d（交易日生效）",
            fire_at[0], fire_at[1],
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=TICK_SECONDS)

    # ── tick 主体 ────────────────────────────────────────────
    def _loop(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                self._tick_once()
            except Exception:  # noqa: BLE001 - 调度 tick 异常绝不杀死线程
                logger.warning("close_auto tick 异常（忽略，下轮重试）", exc_info=True)

    def _tick_once(self) -> None:
        """两阶段 tick：先保证当日数据同步完成，再逐组合提交评估。

        - 未到触发时点 / 非交易日 → 直接返回（两阶段都不启动）；
        - 阶段一（``phase="sync"``）：提交增量同步后返回，等同步终态；
        - 阶段二（``phase="review"``）：「全部 active − 今日已跑」逐个提交
          review job（锁冲突的组合下轮 tick 自然重试）。
        """
        now = datetime.now()
        today = now.date()
        fire_at = parse_fire_time(self._settings.auto_review_time)
        trading_day = is_trading_day(self._ws_root, today)
        if (now.hour, now.minute) < fire_at or not trading_day:
            return

        with session_scope() as db:
            sync_status = close_auto_sync_status(db, today)

        act = phase(sync_status)
        if act == "sync":
            if submit_sync(self._runner, today) is not None:
                logger.info("close_auto 提交增量同步 job（%s）", today.isoformat())
            return                          # 提交失败（锁/熔断）→ 下轮重试
        if act == "wait":
            return

        # 阶段二：逐组合评估
        with session_scope() as db:
            portfolios = db.execute(
                select(Portfolio.id, Portfolio.pid).where(Portfolio.status == "active")
            ).all()
            done = closed_auto_job_ids(db, today)

        todo = pending_pids(
            now, fire_at, trading_day, list(portfolios), done
        )
        if not todo:
            return
        id_by_pid = {pid: pid_id for pid_id, pid in portfolios}
        submitted = []
        for pid in todo:
            if submit_one(self._runner, id_by_pid[pid], pid, today) is not None:
                submitted.append(pid)
        if submitted:
            logger.info("close_auto 提交评估 job：%s", ", ".join(submitted))

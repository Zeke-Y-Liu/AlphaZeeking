"""异步 Job 运行器（T1.2，§6.1）。

设计
----
- ``ThreadPoolExecutor(max_workers=2)``；job id 形态 ``job_<ts>_<rand>``（§5.1）。
- 状态机（写 PG ``jobs`` 表）：``queued → running → done|failed``，字段更新
  ``status/progress/started_at/finished_at/result/error``。
- 锁语义：``lock="compute"`` 用 ``WorkspaceManager.compute_lock(ws_name)``（同 workspace
  互斥）；``lock="sync"`` 用 ``sync_lock()``（全局唯一）。**在 submit 时非阻塞获取**，
  冲突抛 ``AppError(ErrorCode.JOB_LOCKED)``，由 HTTP 层转 ``409``（§6.1：不排队）。
- 线程内 ``set_job_id(job_id)``（contextvar），使该 job 的日志自动带 ``job_id``。

后续任务（T2.3/T3.2/T4.1）用法::

    runner = request.app.state.job_runner          # 或 Depends(get_job_runner)
    job_id = runner.submit(
        type="review", params={...}, portfolio_id=pid,
        fn=lambda ctx: {"run_uid": do_review(ctx.ws_root, ctx.params)},
        lock="compute", ws_name="default",
    )
    return JSONResponse(status_code=202,
                        content={"job_id": job_id, "poll": f"/api/v1/jobs/{job_id}"})

``fn`` 内可调用 ``ctx.report(progress, note=...)`` 上报进度；返回值 dict 落 ``jobs.result``。

α 坑：DuckDB 单文件单写——``fn`` 内所有 applib 调用均在 submit 获取的 workspace 锁
（compute/sync）保护下串行执行，勿在锁外直接调用 applib。
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as _wait_futures
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

from sqlalchemy import update

from app.core.errors import AppError, ErrorCode
from app.core.logging import set_job_id
from app.db.session import session_scope
from app.models import Job
from app.services.workspace import WorkspaceManager

logger = logging.getLogger("app.jobs")

# error 落库前的截断长度（避免堆栈/超长文本污染 jobs.error）
MAX_ERROR_LEN = 2000
# 启动自愈：running 超过该分钟数且进程已退出 → 置 failed
STALE_TIMEOUT_MINUTES = 30
STALE_ERROR = "进程异常退出，启动自愈置失败"

VALID_LOCKS = frozenset({"compute", "sync"})


@dataclass
class JobContext:
    """传给 job 函数 ``fn`` 的上下文。

    - ``job_id``：当前 job id；
    - ``report(progress, note=None)``：写 PG ``jobs.progress``（0-100，自动夹紧）；
      ``note`` 仅进日志（``jobs`` 表无 note 列）；
    - ``ws_root``：该 job 所属 workspace 根目录（已 ``ensure``）；
    - ``params`` / ``portfolio_id``：提交时透传，供 ``fn`` 读取。
    """

    job_id: str
    ws_root: Path
    params: dict[str, Any]
    portfolio_id: int | None
    _reporter: Callable[[int, str | None], None] = field(repr=False)

    def report(self, progress: int, note: str | None = None) -> None:
        self._reporter(progress, note)


class JobRunner:
    """进程级异步 job 运行器（PG 持久化状态 + workspace 互斥锁）。"""

    def __init__(self, workspace: WorkspaceManager, max_workers: int = 2) -> None:
        self._workspace = workspace
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ap-job")
        self._futures: set[Future] = set()
        self._futures_guard = threading.Lock()

    # ── 内部：job id / future 追踪 ──────────────────────────────
    @staticmethod
    def _new_job_id() -> str:
        return f"job_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

    def _track(self, future: Future) -> None:
        with self._futures_guard:
            self._futures.add(future)
        future.add_done_callback(self._discard)

    def _discard(self, future: Future) -> None:
        with self._futures_guard:
            self._futures.discard(future)

    # ── 提交 ────────────────────────────────────────────────────
    def submit(
        self,
        *,
        type: str,
        params: dict,
        portfolio_id: int | None,
        fn: Callable[[JobContext], dict | None],
        lock: str = "compute",
        ws_name: str = "default",
    ) -> str:
        """提交一个异步 job，返回 ``job_id``。

        锁冲突时抛 ``AppError(ErrorCode.JOB_LOCKED)``（§6.1，不排队）。锁在 submit 时
        获取、job 终态时释放，保证「同 workspace 同时最多 1 个计算 job / sync 全局唯一」。
        """
        if lock not in VALID_LOCKS:
            raise ValueError(f"未知 lock 类型：{lock!r}（应为 'compute' 或 'sync'）")

        if lock == "sync":
            acquired = self._workspace.try_acquire_sync()
            release = self._workspace.release_sync
            conflict = "sync 全局任务正在运行"
        else:
            acquired = self._workspace.try_acquire_compute(ws_name)
            release = partial(self._workspace.release_compute, ws_name)
            conflict = f"workspace '{ws_name}' 已有计算任务在运行"

        if not acquired:
            raise AppError(
                ErrorCode.JOB_LOCKED,
                f"任务被拒：{conflict}（§6.1 同一 workspace 计算互斥 / sync 全局唯一）",
            )

        job_id = self._new_job_id()
        params = params or {}
        try:
            ws_root = self._workspace.ensure(ws_name)
            with session_scope() as db:
                db.add(
                    Job(
                        id=job_id,
                        type=type,
                        portfolio_id=portfolio_id,
                        status="queued",
                        params=params,
                        progress=0,
                    )
                )
            future = self._executor.submit(
                self._run, job_id, fn, ws_root, release, params, portfolio_id
            )
            self._track(future)
        except Exception as exc:  # noqa: BLE001 - 提交失败需释放锁并落 failed，绝不静默丢锁
            self._best_effort_fail(job_id, f"提交失败：{exc.__class__.__name__}: {exc}")
            release()
            raise
        return job_id

    # ── 执行（worker 线程） ─────────────────────────────────────
    def _run(
        self,
        job_id: str,
        fn: Callable[[JobContext], dict | None],
        ws_root: Path,
        release: Callable[[], None],
        params: dict[str, Any],
        portfolio_id: int | None,
    ) -> None:
        set_job_id(job_id)
        try:
            self._update(job_id, status="running", progress=0, started_at=datetime.now(UTC))
            ctx = JobContext(
                job_id=job_id,
                ws_root=ws_root,
                params=params,
                portfolio_id=portfolio_id,
                _reporter=partial(self._report, job_id),
            )
            result = fn(ctx)
            self._update(
                job_id,
                status="done",
                progress=100,
                finished_at=datetime.now(UTC),
                result=result or {},
            )
        except Exception as exc:  # noqa: BLE001 - job 异常必须落 failed，绝不让线程静默死亡
            # 堆栈只进日志；jobs.error 只存「类型: 摘要」并截断
            logger.exception("job %s 执行失败", job_id)
            self._update(
                job_id,
                status="failed",
                finished_at=datetime.now(UTC),
                error=self._format_error(exc),
            )
        finally:
            release()
            set_job_id(None)

    def _report(self, job_id: str, progress: int, note: str | None = None) -> None:
        """progress 上报：夹紧到 0-100 写库；note 仅记日志。"""
        try:
            value = max(0, min(100, int(progress)))
        except (TypeError, ValueError):
            return
        if note:
            logger.info("job %s progress=%d note=%s", job_id, value, note)
        self._update(job_id, progress=value)

    # ── PG 更新 ─────────────────────────────────────────────────
    @staticmethod
    def _update(job_id: str, **values: Any) -> None:
        with session_scope() as db:
            db.execute(update(Job).where(Job.id == job_id).values(**values))

    def _best_effort_fail(self, job_id: str, message: str) -> None:
        try:
            self._update(
                job_id,
                status="failed",
                finished_at=datetime.now(UTC),
                error=message[:MAX_ERROR_LEN],
            )
        except Exception:  # noqa: BLE001 - 提交失败的兜底，不再外抛
            logger.exception("job %s 提交失败兜底写入 failed 时出错", job_id)

    @staticmethod
    def _format_error(exc: Exception) -> str:
        text = f"{type(exc).__name__}: {exc}".strip() or type(exc).__name__
        if len(text) > MAX_ERROR_LEN:
            text = text[: MAX_ERROR_LEN - 1] + "…"
        return text

    # ── 启动自愈 ────────────────────────────────────────────────
    def recover_stale(self, timeout_minutes: int = STALE_TIMEOUT_MINUTES) -> int:
        """启动自愈：``running`` 且 ``started_at`` 早于阈值 → 置 ``failed``，返回受影响行数。"""
        threshold = datetime.now(UTC) - timedelta(minutes=timeout_minutes)
        with session_scope() as db:
            result = db.execute(
                update(Job)
                .where(Job.status == "running", Job.started_at < threshold)
                .values(status="failed", finished_at=datetime.now(UTC), error=STALE_ERROR)
            )
            count = result.rowcount or 0
        if count:
            logger.warning("启动自愈：%d 个超时 running job 置为 failed", count)
        return count

    # ── 关闭 ────────────────────────────────────────────────────
    def shutdown(self, wait: bool = True, timeout: float | None = 10.0) -> None:
        """优雅关闭 executor（``cancel_futures=False``，等待有超时）。"""
        self._executor.shutdown(wait=False, cancel_futures=False)
        if not wait:
            return
        with self._futures_guard:
            pending = list(self._futures)
        if pending:
            _wait_futures(pending, timeout=timeout)

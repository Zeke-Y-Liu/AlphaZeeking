"""T1.2 Job 运行器 / WorkspaceManager / GET /jobs 测试。

- 不修改 ``conftest.py``（并行任务在改它），本文件自带 fixture；
- PG 不可达时整体 ``pytest.skip``；
- 用例确定性、不依赖外网；sleep 用短值，总时长 ~20s 内。
- 注：按 §6.1「冲突返回 409 JOB_LOCKED（不排队）」语义，submit 在锁被占用时立即
  409；因此「并发提交 3 个」的用例采用「提交失败即短退避重试」的并发提交线程，
  以验证三个 job 最终被接受且**串行执行**（共享计数器证明无并行）。
"""
from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, text

from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_job_id
from app.db.session import SessionLocal, session_scope
from app.models import Job
from app.services.jobs import MAX_ERROR_LEN, STALE_ERROR, JobRunner
from app.services.workspace import WorkspaceManager

# ── fixtures ────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def pg_ready():
    """真实 PG 可达性探测；不可达则跳过（非纯 PG 环境下 make test 仍绿）。"""
    from app.db.session import engine

    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
    except Exception as exc:  # noqa: BLE001 - 探测失败即跳过
        pytest.skip(f"PostgreSQL 不可达：{type(exc).__name__}: {exc}")
    yield engine


@pytest.fixture
def workspace(tmp_path, pg_ready) -> WorkspaceManager:
    """以临时目录为 workspace 根的 WorkspaceManager。"""
    return WorkspaceManager(Settings(workspace_root=str(tmp_path / "ws")))


@pytest.fixture
def runner(workspace: WorkspaceManager) -> Iterator[JobRunner]:
    job_runner = JobRunner(workspace)
    yield job_runner
    job_runner.shutdown(wait=True, timeout=5)


@pytest.fixture
def tracked(pg_ready) -> Iterator[list[str]]:
    """收集本用例创建的 job id，结束后清理，避免污染 PG。"""
    ids: list[str] = []
    yield ids
    if ids:
        with session_scope() as db:
            db.execute(delete(Job).where(Job.id.in_(ids)))


@pytest.fixture
def api_client(pg_ready) -> Iterator[TestClient]:
    from app.main import create_app

    with TestClient(create_app()) as client:
        yield client


# ── helpers ─────────────────────────────────────────────────────


def _sleep_fn(seconds: float, result: dict | None = None):
    def fn(_ctx) -> dict | None:
        time.sleep(seconds)
        return result

    return fn


def _wait_terminal(job_id: str, timeout: float = 15.0) -> Job:
    """轮询 PG 直到 job 到达终态（done/failed）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            if job is not None and job.status in {"done", "failed"}:
                return job
        time.sleep(0.03)
    raise AssertionError(f"job {job_id} 未在 {timeout}s 内到达终态")


# ── 用例 ────────────────────────────────────────────────────────


def test_three_compute_jobs_serialize_and_persist(runner, tracked, pg_ready) -> None:
    """用例 1：并发提交 3 个 sleep 计算任务 → 互斥串行、终态 done、PG 记录一致。"""
    guard = threading.Lock()
    active = {"now": 0, "max": 0}

    def make_fn(i: int):
        def fn(ctx) -> dict:
            with guard:
                active["now"] += 1
                active["max"] = max(active["max"], active["now"])
            time.sleep(0.08)
            ctx.report(50, note=f"half-{i}")
            time.sleep(0.06)
            with guard:
                active["now"] -= 1
            return {"idx": i}

        return fn

    job_ids: list[str] = []
    ids_guard = threading.Lock()

    def submitter(i: int) -> None:
        deadline = time.time() + 12
        while True:
            try:
                jid = runner.submit(
                    type="review",
                    params={"i": i},
                    portfolio_id=None,
                    fn=make_fn(i),
                    lock="compute",
                    ws_name="ws-serial",
                )
            except AppError as exc:
                assert exc.code == ErrorCode.JOB_LOCKED.value
                if time.time() > deadline:
                    raise AssertionError("并发提交重试超时") from exc
                time.sleep(0.02)
                continue
            with ids_guard:
                job_ids.append(jid)
            return

    threads = [threading.Thread(target=submitter, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    tracked.extend(job_ids)

    assert len(job_ids) == 3
    rows = [_wait_terminal(jid) for jid in job_ids]
    assert active["max"] == 1, "同一 workspace 计算 job 发生了并行（互斥失败）"
    assert all(r.status == "done" for r in rows)
    assert all(r.progress == 100 for r in rows)
    assert all(r.started_at is not None and r.finished_at is not None for r in rows)
    assert {r.result["idx"] for r in rows} == {0, 1, 2}


def test_second_compute_submit_raises_job_locked(runner, tracked, pg_ready) -> None:
    """用例 2：计算锁被占用时第二次 submit(lock="compute") 抛 JOB_LOCKED。"""
    first = runner.submit(
        type="review", params={}, portfolio_id=None,
        fn=_sleep_fn(0.25, {"ok": True}), lock="compute", ws_name="ws-lock",
    )
    tracked.append(first)
    with pytest.raises(AppError) as excinfo:
        runner.submit(
            type="review", params={}, portfolio_id=None,
            fn=_sleep_fn(0.05), lock="compute", ws_name="ws-lock",
        )
    assert excinfo.value.code == ErrorCode.JOB_LOCKED.value
    assert excinfo.value.status_code == 409
    assert _wait_terminal(first).status == "done"


def test_sync_lock_global_and_independent_of_compute(runner, tracked, pg_ready) -> None:
    """用例 3：sync 全局唯一（不同 ws 也互斥），且与 compute 锁互不干扰。"""
    sync_job = runner.submit(
        type="data_sync", params={}, portfolio_id=None,
        fn=_sleep_fn(0.25, {"ok": True}), lock="sync", ws_name="ws-a",
    )
    tracked.append(sync_job)
    # compute 使用不同锁，仍可提交
    comp_job = runner.submit(
        type="review", params={}, portfolio_id=None,
        fn=_sleep_fn(0.05, {"ok": True}), lock="compute", ws_name="ws-a",
    )
    tracked.append(comp_job)
    # sync 全局唯一：即便 ws_name 不同也拒绝
    with pytest.raises(AppError) as excinfo:
        runner.submit(
            type="data_sync", params={}, portfolio_id=None,
            fn=_sleep_fn(0.05), lock="sync", ws_name="ws-b",
        )
    assert excinfo.value.code == ErrorCode.JOB_LOCKED.value
    assert _wait_terminal(sync_job).status == "done"
    assert _wait_terminal(comp_job).status == "done"


def test_failed_job_records_truncated_error(runner, tracked, pg_ready) -> None:
    """用例 4：任务抛异常 → job failed、error 非空且截断（无堆栈全文）、不卡 running。"""

    def boom(_ctx) -> dict:
        raise ValueError("boom-" + "x" * 5000)

    jid = runner.submit(
        type="review", params={}, portfolio_id=None, fn=boom,
        lock="compute", ws_name="ws-fail",
    )
    tracked.append(jid)
    job = _wait_terminal(jid)
    assert job.status == "failed"
    assert job.error
    assert len(job.error) <= MAX_ERROR_LEN
    assert "Traceback" not in job.error
    assert job.finished_at is not None


def test_recover_stale_marks_old_running_failed(runner, tracked, pg_ready) -> None:
    """用例 5：recover_stale 将 40 分钟前的 running job 置 failed。"""
    jid = f"job_probe_stale_{uuid.uuid4().hex[:8]}"
    tracked.append(jid)
    old = datetime.now(UTC) - timedelta(minutes=40)
    with session_scope() as db:
        db.add(Job(id=jid, type="review", status="running", params={}, progress=10, started_at=old))

    affected = runner.recover_stale(30)
    assert affected >= 1
    with SessionLocal() as db:
        job = db.get(Job, jid)
    assert job.status == "failed"
    assert job.error == STALE_ERROR


def test_get_job_endpoint_ok_and_404(api_client, tracked, pg_ready) -> None:
    """用例 6：GET /api/v1/jobs/{id} 六字段正确；不存在 → 404 统一错误体。"""
    jid = f"job_probe_get_{uuid.uuid4().hex[:8]}"
    tracked.append(jid)
    with session_scope() as db:
        db.add(Job(id=jid, type="review", status="queued", params={}, progress=0))

    resp = api_client.get(f"/api/v1/jobs/{jid}")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"id", "type", "status", "progress", "result", "error"}
    assert body == {
        "id": jid, "type": "review", "status": "queued",
        "progress": 0, "result": None, "error": None,
    }

    resp404 = api_client.get("/api/v1/jobs/job_does_not_exist")
    assert resp404.status_code == 404
    err = resp404.json()
    assert set(err) == {"code", "message", "detail"}
    assert err["code"] == ErrorCode.NOT_FOUND.value


def test_job_id_contextvar_inside_job(runner, tracked, pg_ready) -> None:
    """用例 7：任务内 set_job_id 生效——get_job_id() 等于该 job_id（日志贯穿）。"""
    seen: dict[str, str | None] = {}

    def fn(ctx) -> dict:
        seen["ctx_job_id"] = ctx.job_id
        seen["contextvar"] = get_job_id()
        return {"job_id": ctx.job_id}

    jid = runner.submit(
        type="review", params={}, portfolio_id=None, fn=fn,
        lock="compute", ws_name="ws-ctx",
    )
    tracked.append(jid)
    job = _wait_terminal(jid)
    assert job.status == "done"
    assert seen["contextvar"] == jid
    assert seen["ctx_job_id"] == jid
    assert job.result == {"job_id": jid}

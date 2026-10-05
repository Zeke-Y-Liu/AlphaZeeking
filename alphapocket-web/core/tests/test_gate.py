"""T1.6 验证门禁测试（§4.4 / §5.1 verify_results / §6.3）。

覆盖：
1. 六项全 pass → ``gate_or_raise`` 不抛、summary.status == pass；
2. golden_test=fail → ``AppError`` code=VERIFY_GATE_FAILED、status 409、detail.failed 含 golden_test；
3. 无任何验证记录 → unknown、gate 抛 409 且 detail.reason == no_verification；
4. ``run_and_store`` 在空/不存在数据的临时 workspace → 不抛、6 条全 fail、detail 含 error/text；
5. detail 解析（monkeypatch applib VERIFY_SUITE 假返回）→ count/exempt/max_diff/sources 数值正确。

- 自带 fixture，不改 conftest；
- PG 不可达整体 skip；不依赖外网；
- 唯一 job_id 前缀 ``job_test_gate_<uuid>``，并用 tracked fixture 清理新增行，避免污染。
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from applib import runner as applib_runner
from applib.ctx import Ctx
from sqlalchemy import delete, select, text

from app.core.errors import AppError, ErrorCode
from app.db.session import SessionLocal, session_scope
from app.models import Job, VerifyResult
from app.services import verification
from app.services.verification import CHECKS, gate_or_raise, latest_verify_summary, run_and_store

JOB_PREFIX = "job_test_gate_"


# ── fixtures ────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def pg_ready():
    from app.db.session import engine

    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
    except Exception as exc:  # noqa: BLE001 - 探测失败即跳过
        pytest.skip(f"PostgreSQL 不可达：{type(exc).__name__}: {exc}")
    yield engine


@pytest.fixture
def seeded(pg_ready, alembic_config) -> None:
    """确保 schema 已迁移（幂等）。"""
    from alembic import command

    try:
        command.upgrade(alembic_config, "head")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"schema 不可用（PG 未迁移？）：{type(exc).__name__}: {exc}")


@pytest.fixture
def tracked(pg_ready) -> Iterator[list[str]]:
    """收集本用例创建的 job id，结束后按 FK 顺序清理（verify_results → runs → jobs）。"""
    ids: list[str] = []
    yield ids
    if ids:
        with session_scope() as db:
            db.execute(delete(VerifyResult).where(VerifyResult.job_id.in_(ids)))
            db.execute(delete(Job).where(Job.id.in_(ids)))


# ── helpers ─────────────────────────────────────────────────────


def _new_job_id() -> str:
    return f"{JOB_PREFIX}{uuid.uuid4().hex[:8]}"


def _add_job(job_id: str) -> None:
    with session_scope() as db:
        db.add(Job(id=job_id, type="data_sync", status="done", params={}, progress=100))


def _add_verify(job_id: str, check: str, status: str, text_value: str = "") -> None:
    detail: dict[str, Any] = {
        "text": text_value,
        "count": None,
        "exempt": None,
        "max_diff": None,
        "sources": None,
        "error": None,
    }
    with session_scope() as db:
        db.add(VerifyResult(job_id=job_id, check=check, status=status, detail=detail))


def _all_pass_rows(job_id: str) -> None:
    for check in CHECKS:
        _add_verify(job_id, check, "pass", text_value=f"pass({check})")


# ── 用例 1：全绿放行 ────────────────────────────────────────────


def test_gate_passes_when_all_checks_pass(seeded, tracked) -> None:
    job_id = _new_job_id()
    tracked.append(job_id)
    _add_job(job_id)
    _all_pass_rows(job_id)

    with SessionLocal() as db:
        summary = latest_verify_summary(db)
        assert summary["status"] == "pass"
        assert summary["job_id"] == job_id
        assert len(summary["checks"]) == 6
        assert summary["failed"] == []
        # 全绿不抛，返回同一 summary
        returned = gate_or_raise(db)
        assert returned["status"] == "pass"


# ── 用例 2：golden fail → 409 ───────────────────────────────────


def test_gate_raises_409_on_golden_fail(seeded, tracked) -> None:
    job_id = _new_job_id()
    tracked.append(job_id)
    _add_job(job_id)
    for check in CHECKS:
        if check == "golden_test":
            _add_verify(job_id, check, "fail", text_value="FAIL(510300.SH.ma20_break 缺失)")
        else:
            _add_verify(job_id, check, "pass", text_value=f"pass({check})")

    with SessionLocal() as db:
        assert latest_verify_summary(db)["status"] == "fail"
        with pytest.raises(AppError) as excinfo:
            gate_or_raise(db)

    exc = excinfo.value
    assert exc.code == ErrorCode.VERIFY_GATE_FAILED.value
    assert exc.status_code == 409
    assert exc.detail["reason"] == "failed_checks"
    assert any(item["check"] == "golden_test" for item in exc.detail["failed"])
    assert exc.detail["job_id"] == job_id


# ── 用例 3：无记录 → unknown + 409 no_verification ──────────────


def test_gate_unknown_when_no_verification(seeded, tracked) -> None:
    # 清空所有 data_sync 的验证记录，构造"无任何记录"确定性前置
    with session_scope() as db:
        ds_jobs = select(Job.id).where(Job.type == "data_sync").scalar_subquery()
        db.execute(delete(VerifyResult).where(VerifyResult.job_id.in_(ds_jobs)))

    with SessionLocal() as db:
        summary = latest_verify_summary(db)
        assert summary["status"] == "unknown"
        assert summary["job_id"] is None
        assert summary["checks"] == []
        with pytest.raises(AppError) as excinfo:
            gate_or_raise(db)

    exc = excinfo.value
    assert exc.code == ErrorCode.VERIFY_GATE_FAILED.value
    assert exc.status_code == 409
    assert exc.detail["reason"] == "no_verification"


# ── 用例 4：空 workspace 不抛、6 条全 fail ──────────────────────


def test_run_and_store_on_empty_workspace_absorbs_errors(tmp_path, seeded, tracked) -> None:
    job_id = _new_job_id()
    tracked.append(job_id)
    _add_job(job_id)

    empty_ws = tmp_path / "empty_ws"  # 不存在 / 无任何数据文件

    rows = run_and_store(empty_ws, job_id)  # 不得抛异常

    assert [r["check"] for r in rows] == list(CHECKS)
    assert len(rows) == 6
    assert all(r["status"] == "fail" for r in rows)
    assert all(r["detail"]["text"] and r["detail"]["error"] for r in rows)

    with SessionLocal() as db:
        stored = (
            db.execute(select(VerifyResult).where(VerifyResult.job_id == job_id))
            .scalars()
            .all()
        )
    assert len(stored) == 6
    assert all(s.status == "fail" for s in stored)


# ── 用例 5：detail 解析 ─────────────────────────────────────────


def _fake_suite() -> list:
    def pit_audit(ctx):
        return {"pit_audit": "pass"}

    def golden_test(ctx):
        return {"golden_test": "pass(99 项对账)"}

    def cross_source(ctx):
        return {"cross_source": "pass(90 点对账)"}

    def calendar_check(ctx):
        return {"calendar_check": "pass(末根=2026-09-30·日历1393日·停牌豁免3日)"}

    def adjust_check(ctx):
        return {"adjust_check": "pass(5531 点·最大偏差0.2339%)"}

    def provenance_check(ctx):
        return {"provenance_check": "pass(7 数据集)"}

    return [pit_audit, golden_test, cross_source, calendar_check, adjust_check, provenance_check]


def test_detail_parsing_from_suite(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(applib_runner, "VERIFY_SUITE", _fake_suite())

    rows = verification._run_checks(Ctx(root=tmp_path))
    by_check = {r["check"]: r for r in rows}

    assert by_check["pit_audit"]["status"] == "pass"
    assert by_check["golden_test"]["detail"]["count"] == 99
    assert by_check["cross_source"]["detail"]["count"] == 90
    assert by_check["calendar_check"]["detail"]["count"] == 1393
    assert by_check["calendar_check"]["detail"]["exempt"] == 3
    assert by_check["adjust_check"]["detail"]["count"] == 5531
    assert by_check["adjust_check"]["detail"]["max_diff"] == pytest.approx(0.002339)
    assert by_check["provenance_check"]["detail"]["sources"] == 7

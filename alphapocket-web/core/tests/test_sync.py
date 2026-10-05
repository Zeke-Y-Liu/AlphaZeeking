"""T1.4 数据同步 job 测试（§4.2 三 mode / §4.3 存储分治 / §6.2 端点）。

- 不修改 ``conftest.py``（并行任务在改它），本文件自带 fixture；
- PG 不可达时整体 ``pytest.skip``；**不依赖外网**——``applib.datasource.fetch_all``
  被 monkeypatch 为确定性假实现（写 fake repo 文件）；
- Redis 用内存假客户端替换（不依赖真实 Redis，熔断用例确定）；
- 测试自行清理本次新增的 runs/jobs 行。
"""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from applib import datasource
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text

from app.core.config import get_settings
from app.core.errors import ErrorCode
from app.db.session import SessionLocal, session_scope
from app.models import Job, Portfolio, Run, Security, VerifyResult
from app.services import data_sync
from app.services.data_sync import FAIL_THRESHOLD, sync_workspace

MULTI5_ORDER = ["510300.SH", "501018.SH", "518880.SH", "513100.SH", "511260.SH"]

# α 快照 repo（回归只读，不依赖外网）
ALPHA_SNAPSHOT = Path(os.environ.get("AP_ALPHA_SNAPSHOT", "/workspace/alphapocket/remote/repo"))


# ── 假 Redis（内存，覆盖 exists/get/incr/expire/set/delete）───────


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, Any] = {}

    def exists(self, key: str) -> int:
        return 1 if key in self.store else 0

    def get(self, key: str) -> Any:
        return self.store.get(key)

    def incr(self, key: str) -> int:
        value = int(self.store.get(key, 0)) + 1
        self.store[key] = value
        return value

    def expire(self, key: str, ttl: int) -> bool:
        return True

    def set(self, key: str, value: Any, ex: int | None = None) -> bool:
        self.store[key] = value
        return True

    def delete(self, *keys: str) -> int:
        for key in keys:
            self.store.pop(key, None)
        return 1


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
def ws_root(tmp_path, monkeypatch) -> Iterator[Path]:
    """以临时目录覆盖 workspace_root。"""
    root = tmp_path / "ws"
    monkeypatch.setenv("AP_WORKSPACE_ROOT", str(root))
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


@pytest.fixture
def seeded(pg_ready, alembic_config) -> None:
    """确保 schema 已迁移且 seed 数据（九标的 + 两组合）存在（幂等）。"""
    from alembic import command

    from app.seed import run_seed

    try:
        command.upgrade(alembic_config, "head")
        run_seed()
    except Exception as exc:  # noqa: BLE001 - PG 未就绪则跳过
        pytest.skip(f"seed 数据不可用（PG 未迁移？）：{type(exc).__name__}: {exc}")


@pytest.fixture
def api_client(ws_root, seeded) -> Iterator[TestClient]:
    from app.main import create_app

    with TestClient(create_app()) as client:
        yield client


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch) -> FakeRedis:
    """每个用例独立的假 Redis，替换 data_sync 的 get_redis（熔断确定）。"""
    client = FakeRedis()
    monkeypatch.setattr(data_sync, "get_redis", lambda: client)
    return client


@pytest.fixture
def tracked(pg_ready) -> Iterator[dict[str, list[str]]]:
    """收集本用例创建的 job id / run_uid，结束后按 FK 顺序清理。"""
    state: dict[str, list[str]] = {"jobs": [], "runs": []}
    yield state
    with session_scope() as db:
        if state["runs"]:
            db.execute(delete(Run).where(Run.run_uid.in_(state["runs"])))
        if state["jobs"]:
            # T1.6：同步 job 末尾默认写 verify_results，先清子表再删 jobs（FK 无级联）
            db.execute(delete(VerifyResult).where(VerifyResult.job_id.in_(state["jobs"])))
            db.execute(delete(Run).where(Run.job_id.in_(state["jobs"])))
            db.execute(delete(Job).where(Job.id.in_(state["jobs"])))


# ── helpers ─────────────────────────────────────────────────────


def _pg_secids() -> list[str]:
    with SessionLocal() as db:
        return list(db.execute(select(Security.secid).order_by(Security.secid)).scalars().all())


def _pg_security_map() -> dict[str, dict[str, str]]:
    with SessionLocal() as db:
        return {
            s.secid: {"name": s.name, "asset_type": s.asset_type}
            for s in db.execute(select(Security)).scalars().all()
        }


def _pg_active_pids() -> list[str]:
    with SessionLocal() as db:
        return list(
            db.execute(
                select(Portfolio.pid).where(Portfolio.status == "active").order_by(Portfolio.id)
            )
            .scalars()
            .all()
        )


def _install_fake_fetch_all(
    monkeypatch,
    captured: dict[str, Any],
    *,
    bars: dict[str, list[dict]] | None = None,
    fail: bool = False,
    delay: float = 0.0,
):
    """确定性假 fetch_all：记录入参，写 fake repo 文件（不联网）。"""

    def fake(ctx, start="2021-01-01", fin_start_year="2019", universe=None, incremental_from=None):
        captured["start"] = start
        captured["universe"] = list(universe) if universe is not None else None
        captured["incremental_from"] = (
            dict(incremental_from) if incremental_from else incremental_from
        )
        if delay:
            time.sleep(delay)
        if fail:
            raise RuntimeError("upstream down")
        repo = ctx.repo_dir
        for sub in ("market", "factors", "portfolio", "dim"):
            (repo / sub).mkdir(parents=True, exist_ok=True)
        data = bars or {
            "510300.SH": [
                {"dt": "2026-09-29", "close": 4.0, "pct": None},
                {"dt": "2026-09-30", "close": 4.1, "pct": 0.025},
            ]
        }
        (repo / "market" / "daily_bars.json").write_text(json.dumps(data, ensure_ascii=False))
        (repo / "market" / "trade_calendar.json").write_text(json.dumps(["2026-09-30"]))
        (repo / "market" / "provenance.json").write_text(
            json.dumps({"fetched_at": "2026-10-05T18:30:00", "sources": []})
        )
        per = {s: {"bars": len(arr), "last_dt": arr[-1]["dt"] if arr else None} for s, arr in data.items()}
        return {
            "fetched": True,
            "universe": list(universe or []),
            "start": start,
            "datasets": [],
            "as_of_bars": "2026-09-30",
            "universe_fetched": list(per),
            "universe_missing": [],
            "per_secid": per,
            "incremental_from": incremental_from or {},
        }

    monkeypatch.setattr(datasource, "fetch_all", fake)
    return fake


def _wait_terminal(job_id: str, timeout: float = 15.0) -> Job:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            if job is not None and job.status in {"done", "failed"}:
                return job
        time.sleep(0.03)
    raise AssertionError(f"job {job_id} 未在 {timeout}s 内到达终态")


def _sync_and_wait(api_client: TestClient, tracked, payload: dict) -> Job:
    """POST /data/sync 并在锁冲突时短退避重试，等待终态（circuit 用例用）。"""
    for _ in range(100):
        resp = api_client.post("/api/v1/data/sync", json=payload)
        if resp.status_code == 409:
            time.sleep(0.02)
            continue
        assert resp.status_code == 202, resp.text
        job_id = resp.json()["job_id"]
        tracked["jobs"].append(job_id)
        job = _wait_terminal(job_id)
        time.sleep(0.03)  # 等 JobRunner 释放 sync 锁
        return job
    raise AssertionError("sync 提交持续冲突（锁未释放？）")


# ── 三 mode 参数（直接调 sync_workspace）─────────────────────────

def test_bootstrap_params(ws_root, seeded, monkeypatch) -> None:
    """bootstrap：universe=PG securities 全集、start=2021-01-01、incremental_from=None。"""
    captured: dict[str, Any] = {}
    _install_fake_fetch_all(monkeypatch, captured)

    result = sync_workspace(ws_root, "bootstrap")

    assert set(captured["universe"]) == set(_pg_secids())
    assert captured["start"] == "2021-01-01"
    assert captured["incremental_from"] is None
    assert result["mode"] == "bootstrap"


def test_incremental_uses_last_bar_date(ws_root, seeded, monkeypatch) -> None:
    """incremental：预置 daily_bars.json → incremental_from=各标的最后日期。"""
    target = ws_root / "sandbox" / "repo" / "market"
    target.mkdir(parents=True, exist_ok=True)
    (target / "daily_bars.json").write_text(
        json.dumps(
            {
                "510300.SH": [{"dt": "2026-09-28"}, {"dt": "2026-09-30"}],
                "601058.SH": [{"dt": "2026-09-25"}],
            }
        ),
        encoding="utf-8",
    )
    captured: dict[str, Any] = {}
    _install_fake_fetch_all(monkeypatch, captured)

    sync_workspace(ws_root, "incremental")

    assert captured["incremental_from"] == {
        "510300.SH": "2026-09-30",
        "601058.SH": "2026-09-25",
    }
    assert set(captured["universe"]) == set(_pg_secids())


def test_refresh_params(ws_root, seeded, monkeypatch) -> None:
    """refresh：universe==[secid]、start≈today-5d。"""
    captured: dict[str, Any] = {}
    _install_fake_fetch_all(monkeypatch, captured)

    sync_workspace(ws_root, "refresh", secid="510300.SH")

    assert captured["universe"] == ["510300.SH"]
    assert captured["start"] == (date.today() - timedelta(days=5)).isoformat()
    assert captured["incremental_from"] is None


def test_invalid_mode_raises_validation(ws_root, seeded, monkeypatch) -> None:
    """直接调用亦校验 mode（防御性）。"""
    captured: dict[str, Any] = {}
    _install_fake_fetch_all(monkeypatch, captured)
    with pytest.raises(Exception) as excinfo:
        sync_workspace(ws_root, "bogus")
    assert getattr(excinfo.value, "code", None) == ErrorCode.VALIDATION_ERROR.value


# ── clone + materialize ─────────────────────────────────────────

def test_clone_and_materialize_from_pg(ws_root, seeded, monkeypatch) -> None:
    """clone 生成 sandbox/repo；materialize 的 dim/portfolio 内容来自 PG。"""
    captured: dict[str, Any] = {}
    _install_fake_fetch_all(monkeypatch, captured)

    sync_workspace(ws_root, "bootstrap")

    repo = ws_root / "sandbox" / "repo"
    # clone：fake 写 remote/repo，clone 后 sandbox/repo 可见
    assert (repo / "market" / "daily_bars.json").exists()

    dim = json.loads((repo / "dim" / "security.json").read_text(encoding="utf-8"))
    assert {d["secid"] for d in dim} == set(_pg_secids())
    pg_sec = _pg_security_map()
    for row in dim:
        assert row["name"] == pg_sec[row["secid"]]["name"]
        assert row["asset_type"] == pg_sec[row["secid"]]["asset_type"]

    active = _pg_active_pids()
    files = {p.stem for p in (repo / "portfolio").glob("*.json")}
    assert files == set(active)
    if "multi5" in active:
        multi5 = json.loads((repo / "portfolio" / "multi5.json").read_text(encoding="utf-8"))
        assert [p["secid"] for p in multi5["positions"]] == MULTI5_ORDER
        assert [p["weight"] for p in multi5["positions"]] == [20.0] * 5


# ── DuckDB 物化回归（T1.4：pit_audit / golden_test 直读 ap.duckdb）──

def test_sync_pipeline_materializes_duckdb(ws_root, seeded, monkeypatch) -> None:
    """bootstrap 全链（fetch_all → clone → materialize → build DuckDB）后 DuckDB 落盘。

    回归背景：同步主链曾缺失 DuckDB 物化步骤，``sandbox/data/ap.duckdb`` 不存在，
    六项验证的 ``pit_audit`` / ``golden_test`` 直读 ``ctx.db_path`` 恒以
    ``Cannot open database ... does not exist`` 判 fail。本用例以 α 只读快照为
    fetch_all 输入（不联网），守卫该物化确实被主链调用且因子表非空。
    """
    if not ALPHA_SNAPSHOT.exists():
        pytest.skip(f"α 快照缺失：{ALPHA_SNAPSHOT}")

    bars = json.loads((ALPHA_SNAPSHOT / "market" / "daily_bars.json").read_text(encoding="utf-8"))

    def fake(ctx, start="2021-01-01", fin_start_year="2019", universe=None, incremental_from=None):
        shutil.copytree(ALPHA_SNAPSHOT, ctx.repo_dir, dirs_exist_ok=True)
        per = {
            s: {"bars": len(arr), "last_dt": arr[-1]["dt"] if arr else None}
            for s, arr in bars.items()
        }
        return {
            "fetched": True,
            "universe": list(universe or []),
            "start": start,
            "as_of_bars": max((v["last_dt"] for v in per.values() if v["last_dt"]), default=None),
            "universe_fetched": list(per),
            "universe_missing": [],
            "per_secid": per,
            "incremental_from": incremental_from or {},
        }

    monkeypatch.setattr(datasource, "fetch_all", fake)

    result = sync_workspace(ws_root, "bootstrap")
    assert result["duckdb"]["built"] is True, result["duckdb"]

    db_path = ws_root / "sandbox" / "data" / "ap.duckdb"
    assert db_path.exists()

    import duckdb

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        tables = {row[0] for row in con.execute("show tables").fetchall()}
        counts = {
            t: con.execute(f"select count(*) from {t}").fetchone()[0]
            for t in ("factor_value_daily", "factor_pit_meta")
        }
    finally:
        con.close()

    assert {"factor_value_daily", "factor_pit_meta"} <= tables
    assert counts["factor_value_daily"] > 0
    assert counts["factor_pit_meta"] > 0


# ── runs 记录 ───────────────────────────────────────────────────

def test_runs_row_recorded(ws_root, seeded, monkeypatch, tracked) -> None:
    """job_id 非空时落 runs（type=data_sync、artifact_keys=[]、summary 含 universe/耗时）。"""
    captured: dict[str, Any] = {}
    _install_fake_fetch_all(monkeypatch, captured)

    job_id = f"job_test_sync_{uuid.uuid4().hex[:8]}"
    with session_scope() as db:
        db.add(Job(id=job_id, type="data_sync", status="running", params={}, progress=0))
    tracked["jobs"].append(job_id)

    result = sync_workspace(ws_root, "bootstrap", job_id=job_id)
    assert result["run_uid"]
    tracked["runs"].append(result["run_uid"])

    with SessionLocal() as db:
        run = db.execute(select(Run).where(Run.run_uid == result["run_uid"])).scalar_one()
    assert run.type == "data_sync"
    assert run.job_id == job_id
    assert run.portfolio_id is None
    assert run.artifact_keys == []
    assert run.as_of == date(2026, 9, 30)
    assert run.summary["mode"] == "bootstrap"
    assert run.summary["universe_count"] == len(_pg_secids())
    assert "duration_s" in run.summary
    assert result["universe_missing"] == []


def test_universe_missing_recorded_not_failed(ws_root, seeded, monkeypatch) -> None:
    """universe_missing 透明记录、不判失败（既有设计限制，登记）。"""

    def fake(ctx, start="2021-01-01", fin_start_year="2019", universe=None, incremental_from=None):
        repo = ctx.repo_dir
        (repo / "market").mkdir(parents=True, exist_ok=True)
        (repo / "market" / "daily_bars.json").write_text(json.dumps({}))
        return {
            "fetched": True,
            "universe": list(universe or []),
            "start": start,
            "as_of_bars": None,
            "universe_fetched": [],
            "universe_missing": ["999999.SH"],
            "per_secid": {},
        }

    monkeypatch.setattr(datasource, "fetch_all", fake)

    result = sync_workspace(ws_root, "bootstrap")
    assert result["universe_missing"] == ["999999.SH"]


# ── 端点 ────────────────────────────────────────────────────────

def test_post_sync_returns_202_and_poll(api_client, seeded, monkeypatch, tracked) -> None:
    """POST /data/sync → 202 {job_id, poll}；job 终态 done 且落 run。"""
    captured: dict[str, Any] = {}
    _install_fake_fetch_all(monkeypatch, captured, delay=0.05)

    resp = api_client.post("/api/v1/data/sync", json={"mode": "incremental"})
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert set(body) == {"job_id", "poll"}
    assert body["poll"] == f"/api/v1/jobs/{body['job_id']}"
    tracked["jobs"].append(body["job_id"])

    job = _wait_terminal(body["job_id"])
    assert job.status == "done"
    with SessionLocal() as db:
        run = db.execute(select(Run).where(Run.job_id == body["job_id"])).scalar_one()
    tracked["runs"].append(run.run_uid)
    assert run.type == "data_sync"


def test_second_sync_conflicts_409(api_client, seeded, monkeypatch, tracked) -> None:
    """首个 sync 运行中，第二次 POST → 409 JOB_LOCKED。"""
    captured: dict[str, Any] = {}
    _install_fake_fetch_all(monkeypatch, captured, delay=0.4)

    first = api_client.post("/api/v1/data/sync", json={"mode": "bootstrap"})
    assert first.status_code == 202, first.text
    job_id = first.json()["job_id"]
    tracked["jobs"].append(job_id)

    second = api_client.post("/api/v1/data/sync", json={"mode": "incremental"})
    assert second.status_code == 409, second.text
    assert second.json()["code"] == ErrorCode.JOB_LOCKED.value

    assert _wait_terminal(job_id).status == "done"
    with SessionLocal() as db:
        run = db.execute(select(Run).where(Run.job_id == job_id)).scalar_one_or_none()
    if run is not None:
        tracked["runs"].append(run.run_uid)


def test_invalid_mode_and_missing_secid_422(api_client, seeded) -> None:
    """mode 非法 → 422；refresh 缺 secid → 422（均 VALIDATION_ERROR）。"""
    bad_mode = api_client.post("/api/v1/data/sync", json={"mode": "bogus"})
    assert bad_mode.status_code == 422, bad_mode.text
    assert bad_mode.json()["code"] == ErrorCode.VALIDATION_ERROR.value

    missing_secid = api_client.post("/api/v1/data/sync", json={"mode": "refresh"})
    assert missing_secid.status_code == 422, missing_secid.text
    assert missing_secid.json()["code"] == ErrorCode.VALIDATION_ERROR.value


def test_circuit_opens_after_three_failures_503(
    api_client, seeded, monkeypatch, tracked, fake_redis
) -> None:
    """连续 3 次上游失败 → 熔断开 → 下次 POST 503 UPSTREAM_RATE_LIMITED。"""
    _install_fake_fetch_all(monkeypatch, {}, fail=True)

    for _ in range(FAIL_THRESHOLD):
        job = _sync_and_wait(api_client, tracked, {"mode": "incremental"})
        assert job.status == "failed"

    assert fake_redis.exists(data_sync.CIRCUIT_KEY) == 1
    blocked = api_client.post("/api/v1/data/sync", json={"mode": "incremental"})
    assert blocked.status_code == 503, blocked.text
    assert blocked.json()["code"] == ErrorCode.UPSTREAM_RATE_LIMITED.value

"""T1.5 数据状态 API 测试（``GET /data/status`` / ``GET /data/verifications``）。

- 不修改 ``conftest.py``（并行任务在改它），本文件自带 fixture；
- 用真实 PG + ``tmp_path`` 造 workspace（``sandbox/repo/market/daily_bars.json``）；
- 不依赖外网；PG 不可达时整体 ``pytest.skip``；
- 所有落库行用 ``test_ds_`` 唯一前缀并在用例结束后清理（先删 verify_results 再删 jobs）。
"""
from __future__ import annotations

import json
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text, update

from app.core.config import get_settings
from app.db.session import SessionLocal, session_scope
from app.models import Job, Security, VerifyResult
from app.services.data_status import build_status, build_verifications, load_last_bar_dates
from app.services.verification import CHECKS

FUTURE = datetime(2099, 1, 1, tzinfo=UTC)
DETAIL_PASS = {
    "text": "pass",
    "count": None,
    "exempt": None,
    "max_diff": None,
    "sources": None,
    "error": None,
}


# ── fixtures ────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def pg_ready():
    """真实 PG 可达性探测；不可达则跳过依赖它的用例。"""
    from app.db.session import engine

    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
    except Exception as exc:  # noqa: BLE001 - 探测失败即跳过
        pytest.skip(f"PostgreSQL 不可达：{type(exc).__name__}: {exc}")
    yield engine


@pytest.fixture
def cleanup(pg_ready):
    """收集本用例落库的 secid / job_id，结束后按 FK 顺序清理。"""
    secids: list[str] = []
    job_ids: list[str] = []
    yield {"secids": secids, "job_ids": job_ids}
    with session_scope() as db:
        if job_ids:
            db.execute(delete(VerifyResult).where(VerifyResult.job_id.in_(job_ids)))
            db.execute(delete(Job).where(Job.id.in_(job_ids)))
        if secids:
            db.execute(delete(Security).where(Security.secid.in_(secids)))


@pytest.fixture
def settings_env(tmp_path, monkeypatch):
    """覆盖 ``AP_WORKSPACE_ROOT`` 到 tmp_path 并清理 settings 缓存。"""
    monkeypatch.setenv("AP_WORKSPACE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture
def api_client(settings_env) -> TestClient:
    from app.main import create_app

    with TestClient(create_app()) as client:
        yield client
    get_settings.cache_clear()


# ── helpers ─────────────────────────────────────────────────────


def _write_bars(ws_root: Path, mapping: dict[str, list[str]]) -> Path:
    path = ws_root / "sandbox" / "repo" / "market" / "daily_bars.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        secid: [{"dt": dt, "pct": 0.01} for dt in dates] for secid, dates in mapping.items()
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _upsert_security(secid: str, code: str, asset_type: str, cleanup: dict) -> None:
    with session_scope() as db:
        db.execute(delete(Security).where(Security.secid == secid))
        db.add(
            Security(
                secid=secid,
                code=code,
                name=f"测试{code}",
                market="A",
                asset_type=asset_type,
                sina_symbol=f"test{code}",
            )
        )
    cleanup["secids"].append(secid)


def _insert_sync_job(cleanup: dict, *, status: str = "done", progress: int = 100) -> str:
    jid = f"job_test_ds_{uuid.uuid4().hex[:10]}"
    with session_scope() as db:
        db.add(
            Job(
                id=jid,
                type="data_sync",
                status=status,
                params={},
                progress=progress,
                created_at=FUTURE,
                finished_at=FUTURE,
            )
        )
    cleanup["job_ids"].append(jid)
    return jid


def _insert_verify_results(job_id: str, statuses: dict[str, str]) -> None:
    with session_scope() as db:
        for check in CHECKS:
            st = statuses.get(check, "pass")
            db.add(
                VerifyResult(
                    job_id=job_id,
                    check=check,
                    status=st,
                    detail={**DETAIL_PASS, "text": "pass" if st == "pass" else "fail(测试)"},
                )
            )


def _item(status: dict, secid: str) -> dict:
    return next(row for row in status["securities"] if row["secid"] == secid)


# ── load_last_bar_dates ─────────────────────────────────────────


def test_load_last_bar_dates_missing_dir_returns_empty(tmp_path) -> None:
    """不存在的 workspace 目录 → 空 dict，绝不抛异常。"""
    assert load_last_bar_dates(tmp_path / "does" / "not" / "exist") == {}


def test_load_last_bar_dates_takes_max_unordered(tmp_path) -> None:
    """dt 未必有序 → 取 max；remote 回退位亦可读到。"""
    _write_bars(tmp_path, {"X.SH": ["2026-01-05", "2026-01-01", "2026-01-03"]})
    assert load_last_bar_dates(tmp_path) == {"X.SH": "2026-01-05"}

    remote = tmp_path / "remote" / "repo" / "market" / "daily_bars.json"
    remote.parent.mkdir(parents=True, exist_ok=True)
    remote.write_text(json.dumps({"Y.SZ": [{"dt": "2026-02-09"}]}), encoding="utf-8")
    # sandbox 位存在 → 优先；删除后回退 remote
    assert load_last_bar_dates(tmp_path) == {"X.SH": "2026-01-05"}
    (tmp_path / "sandbox" / "repo" / "market" / "daily_bars.json").unlink()
    assert load_last_bar_dates(tmp_path) == {"Y.SZ": "2026-02-09"}


def test_load_last_bar_dates_corrupt_file_returns_empty(tmp_path) -> None:
    path = tmp_path / "sandbox" / "repo" / "market" / "daily_bars.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not-json", encoding="utf-8")
    assert load_last_bar_dates(tmp_path) == {}


# ── build_status ────────────────────────────────────────────────


def test_build_status_missing_and_lag(tmp_path, pg_ready, cleanup) -> None:
    """as_of 为最大 dt；A missing=0；B missing/lag_days 正确。"""
    _write_bars(
        tmp_path,
        {
            "test_ds_a.SH": ["2026-01-01", "2026-01-02", "2026-01-05", "2026-01-06"],
            "test_ds_b.SZ": ["2026-01-01", "2026-01-02"],
        },
    )
    _upsert_security("test_ds_a.SH", "900001", "STOCK", cleanup)
    _upsert_security("test_ds_b.SZ", "910001", "ETF", cleanup)

    with SessionLocal() as db:
        status = build_status(db, tmp_path)

    assert status["as_of"] == "2026-01-06"

    a = _item(status, "test_ds_a.SH")
    assert a["last_bar"] == "2026-01-06"
    assert a["missing"] == 0
    assert a["lag_days"] == 0
    assert a["has_bars"] is True
    assert a["asset_type"] == "STOCK"

    b = _item(status, "test_ds_b.SZ")
    assert b["last_bar"] == "2026-01-02"
    # 并集中晚于 B.last_bar 的交易日 = {01-05, 01-06} → 2
    assert b["missing"] == 2
    assert b["lag_days"] == 4
    assert b["has_bars"] is True


def test_build_status_universe_counts(tmp_path, pg_ready, cleanup) -> None:
    """universe.count 与 types 计数与 PG security 表一致。"""
    _write_bars(tmp_path, {"test_ds_a.SH": ["2026-01-06"]})
    _upsert_security("test_ds_a.SH", "900002", "STOCK", cleanup)
    _upsert_security("test_ds_b.SZ", "910002", "ETF", cleanup)

    with SessionLocal() as db:
        expected_types = Counter(db.execute(select(Security.asset_type)).scalars().all())
        expected_count = sum(expected_types.values())
        status = build_status(db, tmp_path)

    assert status["universe"]["count"] == expected_count
    assert status["universe"]["types"]["STOCK"] == expected_types.get("STOCK", 0)
    assert status["universe"]["types"]["ETF"] == expected_types.get("ETF", 0)
    assert status["universe"]["types"]["LOF"] == expected_types.get("LOF", 0)
    assert status["universe"]["count"] >= 2


def test_build_status_tolerates_missing_bars(tmp_path, pg_ready, cleanup) -> None:
    """bars 缺失 → 各项 None/False，接口不抛异常。"""
    _upsert_security("test_ds_a.SH", "900003", "STOCK", cleanup)

    with SessionLocal() as db:
        status = build_status(db, tmp_path)

    assert status["as_of"] is None
    a = _item(status, "test_ds_a.SH")
    assert a["last_bar"] is None
    assert a["missing"] is None
    assert a["lag_days"] is None
    assert a["has_bars"] is False


def test_build_status_verification_pass_then_fail(tmp_path, pg_ready, cleanup) -> None:
    """六项全 pass → pass；改一条 fail → fail。"""
    job_id = _insert_sync_job(cleanup)
    _insert_verify_results(job_id, {c: "pass" for c in CHECKS})

    with SessionLocal() as db:
        status = build_status(db, tmp_path)
    assert status["verification"]["status"] == "pass"
    assert status["verification"]["job_id"] == job_id
    assert len(status["verification"]["checks"]) == len(CHECKS)
    assert status["verification"]["failed"] == []

    with session_scope() as db:
        db.execute(
            update(VerifyResult)
            .where(VerifyResult.job_id == job_id, VerifyResult.check == "golden_test")
            .values(status="fail", detail={**DETAIL_PASS, "text": "fail(测试)"})
        )

    with SessionLocal() as db:
        status = build_status(db, tmp_path)
    assert status["verification"]["status"] == "fail"
    assert any(f["check"] == "golden_test" for f in status["verification"]["failed"])


def test_build_status_latest_sync(tmp_path, pg_ready, cleanup) -> None:
    """latest_sync 取最近 data_sync job 的 job_id/status/progress/finished_at。"""
    job_id = _insert_sync_job(cleanup, status="running", progress=42)

    with SessionLocal() as db:
        status = build_status(db, tmp_path)

    assert status["latest_sync"]["job_id"] == job_id
    assert status["latest_sync"]["status"] == "running"
    assert status["latest_sync"]["progress"] == 42
    assert status["latest_sync"]["finished_at"] == FUTURE.isoformat()


# ── build_verifications ─────────────────────────────────────────


def test_build_verifications_order_and_limit(tmp_path, pg_ready, cleanup) -> None:
    """按 created_at desc / id desc 排序 + limit 截断。"""
    job_id = _insert_sync_job(cleanup)
    _insert_verify_results(job_id, {c: "pass" for c in CHECKS})

    with SessionLocal() as db:
        rows = build_verifications(db, 3)
    assert len(rows) == 3
    assert set(rows[0]) == {"job_id", "check", "status", "detail", "created_at"}


# ── 端点层 ──────────────────────────────────────────────────────


def test_endpoint_status_ok(api_client, tmp_path, pg_ready, cleanup) -> None:
    """GET /api/v1/data/status → 200 且字段齐全（V4-β 多市场字段）。"""
    _write_bars(tmp_path, {"test_ds_a.SH": ["2026-01-06"]})
    _upsert_security("test_ds_a.SH", "900004", "STOCK", cleanup)

    resp = api_client.get("/api/v1/data/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"as_of", "as_of_by_market", "universe", "securities",
                         "verification", "latest_sync"}
    assert body["as_of"] == "2026-01-06"
    assert set(body["universe"]) == {"count", "types", "markets"}
    assert set(body["universe"]["types"]) == {"STOCK", "ETF", "LOF"}
    assert set(body["universe"]["markets"]) == {"A", "HK", "US"}
    a = _item(body, "test_ds_a.SH")
    assert set(a) == {
        "secid",
        "code",
        "name",
        "market",
        "asset_type",
        "currency",
        "hk_connect",
        "last_bar",
        "lag_days",
        "missing",
        "has_bars",
    }
    assert a["market"] == "A"


def test_endpoint_verifications_limit(api_client, pg_ready, cleanup) -> None:
    """GET /api/v1/data/verifications?limit=1 → 至多 1 条，字段齐全。"""
    job_id = _insert_sync_job(cleanup)
    _insert_verify_results(job_id, {c: "pass" for c in CHECKS})

    resp = api_client.get("/api/v1/data/verifications", params={"limit": 1})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"items", "count", "limit"}
    assert body["limit"] == 1
    assert body["count"] == len(body["items"]) == 1
    assert set(body["items"][0]) == {"job_id", "check", "status", "detail", "created_at"}


def test_endpoint_verifications_default_and_validation(api_client, pg_ready) -> None:
    """默认 limit=20；limit=0 / 999 越界 → 422。"""
    ok = api_client.get("/api/v1/data/verifications")
    assert ok.status_code == 200, ok.text
    assert ok.json()["limit"] == 20

    assert api_client.get("/api/v1/data/verifications", params={"limit": 0}).status_code == 422
    assert api_client.get("/api/v1/data/verifications", params={"limit": 999}).status_code == 422

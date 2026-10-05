"""T3.2 回测 API 测试（``POST /portfolios/{id}/backtest``）。

- 不修改 ``conftest.py``（并行任务在改它），本文件自带 fixture；
- 用真实 PG + ``tmp_path`` 造 workspace（``sandbox/repo/market/daily_bars.json``）；
  **MinIO 不可达 → 上传必须 mock**；**不依赖外网**；PG 不可达时整体 ``pytest.skip``；
- 门禁用 ``monkeypatch app.services.verification.latest_verify_summary`` 造全绿/unknown
  （确定性，避免受其它测试残留影响）；
- 所有落库行用 ``test_bt_`` 唯一前缀并在用例结束后清理，
  顺序：runs → verify_results → jobs → positions → portfolios → securities
  （``verify_results.job_id`` 无 ON DELETE CASCADE，故先删子表）。
"""
from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text

from app.core.config import get_settings
from app.core.errors import ErrorCode
from app.db.session import SessionLocal, session_scope
from app.models import Job, Portfolio, Position, Run, Security, VerifyResult
from app.services.jobs import JobRunner

FUTURE = datetime(2099, 1, 1, tzinfo=UTC)
BAR_DATES = ["2025-01-02", "2025-01-03", "2025-01-06"]
ARTIFACT_NAMES = ("backtest.html", "backtest.json", "backtest.md")
SUMMARY_KEYS = {
    "strategy_metrics",
    "benchmark",
    "excess",
    "rebalance",
    "benchmark_secid",
    "start",
    "end",
    "n_days",
    "n_rebalances",
    "cost_bps",
    "rf",
    "cost",
}

# 最小合法 bt dict（覆盖 ``_build_summary`` 读取的全部键）
FIXTURE_BT = {
    "portfolio": "pid",
    "portfolio_name": "回测组合",
    "strategy": "等权·月度再平衡",
    "rebalance": "monthly",
    "benchmark_secid": "510300.SH",
    "start": "2025-01-02",
    "end": "2025-12-31",
    "n_days": 240,
    "n_rebalances": 12,
    "commission": 0.00025,
    "cost_bps": 2.5,
    "rf": 0.0,
    "series": [["2025-01-02", 1.0], ["2025-12-31", 1.2]],
    "benchmark": {
        "secid": "510300.SH",
        "name": "沪深300ETF",
        "metrics": {"total_return": -0.1, "cagr": -0.1},
    },
    "strategy_metrics": {"total_return": 0.2, "cagr": 0.2},
    "excess": {"total": 0.3, "cagr": 0.3},
    "monthly": {},
    "yearly": {},
    "per_asset": [],
    "cost": {"n": 12, "total": 0.001, "avg_turnover": 0.05},
    "data_quality": {"missing_bars": {}},
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
def ws_root(tmp_path, monkeypatch) -> Iterator[Path]:
    """以临时目录覆盖 ``AP_WORKSPACE_ROOT`` 并清理 settings 缓存。"""
    root = tmp_path / "ws"
    monkeypatch.setenv("AP_WORKSPACE_ROOT", str(root))
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


@pytest.fixture
def api_client(ws_root, pg_ready) -> Iterator[TestClient]:
    from app.main import create_app

    with TestClient(create_app()) as client:
        yield client
    get_settings.cache_clear()


@pytest.fixture
def cleanup(pg_ready) -> Iterator[dict]:
    """收集本用例落库的 job/run/secid/pid，结束后按 FK 顺序清理。"""
    state: dict = {"job_ids": [], "run_uids": [], "secids": [], "pids": []}
    yield state
    with session_scope() as db:
        if state["run_uids"]:
            db.execute(delete(Run).where(Run.run_uid.in_(state["run_uids"])))
        if state["job_ids"]:
            db.execute(delete(VerifyResult).where(VerifyResult.job_id.in_(state["job_ids"])))
            db.execute(delete(Job).where(Job.id.in_(state["job_ids"])))
        if state["pids"]:
            ids = [
                row
                for (row,) in db.execute(
                    select(Portfolio.id).where(Portfolio.pid.in_(state["pids"]))
                ).all()
            ]
            if ids:
                db.execute(delete(Position).where(Position.portfolio_id.in_(ids)))
                db.execute(delete(Portfolio).where(Portfolio.id.in_(ids)))
        if state["secids"]:
            db.execute(delete(Security).where(Security.secid.in_(state["secids"])))


# ── helpers ─────────────────────────────────────────────────────


def _rand(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _make_portfolio(cleanup: dict, pid: str, n: int = 2) -> tuple[int, list[str]]:
    """真实创建 Security + Portfolio + Position，返回 ``(portfolio_id, secids)``。"""
    secids = [f"test_bt_{uuid.uuid4().hex[:8]}.SH" for _ in range(n)]
    weight = Decimal("100") / n
    with session_scope() as db:
        for secid in secids:
            db.add(
                Security(
                    secid=secid,
                    code=secid.split(".")[0][-6:],
                    name=f"测试{secid[:8]}",
                    market="A",
                    asset_type="ETF",
                    sina_symbol=f"test{secid[:8]}",
                )
            )
        db.flush()
        pf = Portfolio(
            pid=pid, name="回测组合", note="t3.2", base_ccy="CNY",
            strategy={}, status="active",
        )
        db.add(pf)
        db.flush()
        pf_id = pf.id
        for secid in secids:
            db.add(Position(portfolio_id=pf_id, secid=secid, weight=weight))
    cleanup["secids"].extend(secids)
    cleanup["pids"].append(pid)
    return pf_id, secids


def _write_bars(ws_root: Path, secids: list[str]) -> Path:
    """在工具实读位写 ``sandbox/repo/market/daily_bars.json``（含 benchmark）。"""
    path = ws_root / "sandbox" / "repo" / "market" / "daily_bars.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        secid: [{"dt": dt, "pct": 0.01} for dt in BAR_DATES] for secid in secids
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _green_gate(monkeypatch) -> None:
    """monkeypatch 门禁为全绿（确定性）。"""
    monkeypatch.setattr(
        "app.services.verification.latest_verify_summary",
        lambda db: {"status": "pass", "job_id": "job_test_bt_sync",
                    "created_at": None, "checks": [], "failed": []},
    )


def _wait_job(api_client: TestClient, job_id: str, timeout: float = 15.0) -> dict:
    """轮询 job 到终态（done/failed）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = api_client.get(f"/api/v1/jobs/{job_id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body["status"] in {"done", "failed"}:
            return body
        time.sleep(0.03)
    raise AssertionError(f"job {job_id} 未在 {timeout}s 内到达终态")


def _mock_backtest_io(monkeypatch) -> dict:
    """mock 回测引擎 / 产物渲染 / 上传（MinIO 不可达）。返回 calls 记录。"""
    calls: dict = {}

    def fake_run_backtest(bars, pf, **kwargs):
        calls["bars"] = bars
        calls["pf"] = pf
        calls["kwargs"] = kwargs
        return dict(FIXTURE_BT)

    def fake_write_all(bt, art_dir, css):
        art_dir = Path(art_dir)
        art_dir.mkdir(parents=True, exist_ok=True)
        for name in ("backtest.json", "backtest.md", "backtest.html"):
            (art_dir / name).write_text("x", encoding="utf-8")
        return {
            "json": str(art_dir / "backtest.json"),
            "md": str(art_dir / "backtest.md"),
            "html": str(art_dir / "backtest.html"),
        }

    def fake_put(run_uid, files):
        return sorted(f"runs/{run_uid}/{name}" for name in ARTIFACT_NAMES)

    monkeypatch.setattr("app.services.backtest_svc.BT.run_backtest", fake_run_backtest)
    monkeypatch.setattr("app.services.backtest_svc.BT.write_all", fake_write_all)
    monkeypatch.setattr("app.services.backtest_svc.artifacts.put_run_artifacts", fake_put)
    return calls


# ── 参数校验 422 ────────────────────────────────────────────────


def test_backtest_invalid_rebalance_422(api_client, ws_root, cleanup, monkeypatch) -> None:
    """rebalance 非法 → 422 VALIDATION_ERROR，不提交 job。"""
    pid = _rand("test_bt_reb")
    _make_portfolio(cleanup, pid, n=2)
    _green_gate(monkeypatch)

    resp = api_client.post(f"/api/v1/portfolios/{pid}/backtest", json={"rebalance": "weekly"})
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == ErrorCode.VALIDATION_ERROR.value


def test_backtest_invalid_range_422(api_client, ws_root, cleanup, monkeypatch) -> None:
    """start > end → 422 VALIDATION_ERROR。"""
    pid = _rand("test_bt_rng")
    _make_portfolio(cleanup, pid, n=2)
    _green_gate(monkeypatch)

    resp = api_client.post(
        f"/api/v1/portfolios/{pid}/backtest",
        json={"start": "2025-02-01", "end": "2025-01-01"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == ErrorCode.VALIDATION_ERROR.value


def test_backtest_benchmark_not_in_bars_422(api_client, ws_root, cleanup, monkeypatch) -> None:
    """benchmark_secid 不在 bars → 422 VALIDATION_ERROR。"""
    pid = _rand("test_bt_bench")
    _, secids = _make_portfolio(cleanup, pid, n=2)
    _green_gate(monkeypatch)
    _write_bars(ws_root, [*secids, "510300.SH"])

    resp = api_client.post(
        f"/api/v1/portfolios/{pid}/backtest", json={"benchmark_secid": "999999.SH"}
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == ErrorCode.VALIDATION_ERROR.value


# ── 404 / 409 ───────────────────────────────────────────────────


def test_backtest_missing_portfolio_404(api_client) -> None:
    """不存在的 pid → 404 NOT_FOUND（优先于门禁）。"""
    resp = api_client.post("/api/v1/portfolios/test_bt_no_such_pid/backtest")
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == ErrorCode.NOT_FOUND.value


def test_backtest_gate_failed_409_no_job(api_client, cleanup, monkeypatch) -> None:
    """门禁非全绿 → 409 VERIFY_GATE_FAILED，且不提交 job。"""
    pid = _rand("test_bt_gate")
    pf_id, _ = _make_portfolio(cleanup, pid, n=1)

    monkeypatch.setattr(
        "app.services.verification.latest_verify_summary",
        lambda db: {"status": "unknown", "job_id": None, "created_at": None,
                    "checks": [], "failed": []},
    )
    called = {"n": 0}

    def _boom(self, *args, **kwargs):
        called["n"] += 1
        raise AssertionError("门禁失败时不应提交 job")

    monkeypatch.setattr(JobRunner, "submit", _boom)

    resp = api_client.post(f"/api/v1/portfolios/{pid}/backtest")
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == ErrorCode.VERIFY_GATE_FAILED.value
    assert called["n"] == 0

    with SessionLocal() as db:
        n = db.execute(
            select(func.count()).select_from(Job).where(
                Job.type == "backtest", Job.portfolio_id == pf_id
            )
        ).scalar_one()
    assert n == 0


# ── 成功路径 ────────────────────────────────────────────────────


def test_backtest_green_gate_job_succeeds(api_client, ws_root, cleanup, monkeypatch) -> None:
    """门禁全绿 → 202 + job 成功；runs 落库、summary/artifact_keys 正确。"""
    pid = _rand("test_bt_ok")
    pf_id, secids = _make_portfolio(cleanup, pid, n=2)
    _green_gate(monkeypatch)
    _write_bars(ws_root, [*secids, "510300.SH"])
    calls = _mock_backtest_io(monkeypatch)

    resp = api_client.post(f"/api/v1/portfolios/{pid}/backtest")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    job_id = body["job_id"]
    cleanup["job_ids"].append(job_id)
    assert body["poll"] == f"/api/v1/jobs/{job_id}"

    job = _wait_job(api_client, job_id)
    assert job["status"] == "done", job
    result = job["result"]
    assert set(result) == {"run_uid", "artifact_keys", "summary"}
    assert result["run_uid"].startswith("bt_")
    cleanup["run_uids"].append(result["run_uid"])

    # 物化已写工具实读位 <ws>/sandbox/repo/portfolio/{pid}.json
    assert (ws_root / "sandbox" / "repo" / "portfolio" / f"{pid}.json").exists()

    summary = result["summary"]
    assert set(summary) == SUMMARY_KEYS
    assert summary["strategy_metrics"] == FIXTURE_BT["strategy_metrics"]
    assert summary["benchmark"] == {
        "secid": "510300.SH", "name": "沪深300ETF",
        "metrics": FIXTURE_BT["benchmark"]["metrics"],
    }
    assert summary["excess"] == FIXTURE_BT["excess"]
    assert summary["rebalance"] == "monthly"
    assert summary["benchmark_secid"] == "510300.SH"
    assert summary["end"] == "2025-12-31"
    assert result["artifact_keys"] == sorted(
        f"runs/{result['run_uid']}/{name}" for name in ARTIFACT_NAMES
    )
    # 默认参数透传到 applib run_backtest
    assert calls["kwargs"] == {
        "start": None, "end": None, "rf": 0.0, "rebalance": "monthly",
        "benchmark_secid": "510300.SH", "cost_bps": 2.5,
    }

    with SessionLocal() as db:
        run = db.execute(select(Run).where(Run.job_id == job_id)).scalar_one()
    assert run.type == "backtest"
    assert run.portfolio_id == pf_id
    assert run.as_of == date(2025, 12, 31)
    assert run.artifact_keys == result["artifact_keys"]
    assert run.summary["benchmark_secid"] == "510300.SH"


def test_backtest_buyhold_passthrough(api_client, ws_root, cleanup, monkeypatch) -> None:
    """``rebalance="buyhold"`` 透传到 applib ``run_backtest`` kwargs。"""
    pid = _rand("test_bt_bh")
    _, secids = _make_portfolio(cleanup, pid, n=2)
    _green_gate(monkeypatch)
    _write_bars(ws_root, [*secids, "510300.SH"])
    calls = _mock_backtest_io(monkeypatch)

    resp = api_client.post(
        f"/api/v1/portfolios/{pid}/backtest",
        json={"rebalance": "buyhold", "benchmark_secid": "510300.SH", "cost_bps": 1.0},
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]
    cleanup["job_ids"].append(job_id)

    job = _wait_job(api_client, job_id)
    assert job["status"] == "done", job
    cleanup["run_uids"].append(job["result"]["run_uid"])

    assert calls["kwargs"]["rebalance"] == "buyhold"
    assert calls["kwargs"]["benchmark_secid"] == "510300.SH"
    assert calls["kwargs"]["cost_bps"] == 1.0

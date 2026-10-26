"""T2.3 评估 run 测试（``POST /portfolios/{id}/review`` + ``GET /runs``）。

门禁语义（本用例集冻结）：评估**不被数据门禁阻断**——非全绿照常 202 + job，
但响应与 ``runs.summary.data_risk`` 携带数据风险提示；全绿时 ``data_risk=None``。

- 不修改 ``conftest.py``（并行任务在改它），本文件自带 fixture；
- 用真实 PG + ``tmp_path`` 造 workspace；**MinIO 不可达 → 上传必须 mock**；
- 不依赖外网；PG 不可达时整体 ``pytest.skip``；
- 所有落库行用 ``test_rv_`` 唯一前缀并在用例结束后清理，
  顺序：runs → verify_results → jobs → positions → portfolios → securities
  （``verify_results.job_id`` 无 ON DELETE CASCADE，故先删子表）。
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.db.session import SessionLocal, session_scope
from app.models import Job, Portfolio, Position, Run, Security, VerifyResult
from app.services.review import run_review
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
ARTIFACT_NAMES = ("report.html", "report.md", "review.json", "rebalance_plan.json")


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
    secids = [f"test_rv_{uuid.uuid4().hex[:8]}.SH" for _ in range(n)]
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
            pid=pid, name="测试组合", note="t2.3", base_ccy="CNY",
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


def _insert_sync_job(cleanup: dict) -> str:
    """插入一个最近（created_at=FUTURE）的 data_sync job。"""
    jid = f"job_test_rv_{uuid.uuid4().hex[:10]}"
    with session_scope() as db:
        db.add(
            Job(
                id=jid, type="data_sync", status="done", params={},
                progress=100, created_at=FUTURE, finished_at=FUTURE,
            )
        )
    cleanup["job_ids"].append(jid)
    return jid


def _insert_verify_all_pass(job_id: str) -> None:
    with session_scope() as db:
        for check in CHECKS:
            db.add(
                VerifyResult(job_id=job_id, check=check, status="pass", detail={**DETAIL_PASS})
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


# ── 用例 ────────────────────────────────────────────────────────


def test_review_gate_failed_not_blocked_but_warns(api_client, ws_root, cleanup, monkeypatch) -> None:
    """用例 1：数据门禁非全绿 → 不阻断评估；202 响应与 runs.summary 均提示 data_risk。"""
    pid = _rand("test_rv_gate")
    pf_id, _ = _make_portfolio(cleanup, pid, n=1)

    fake_summary = {
        "status": "fail",
        "job_id": "job_gate_fail_x",
        "created_at": "2026-01-01T00:00:00+00:00",
        "checks": [],
        "failed": [{"check": "golden_test", "text": "FAIL(510300.SH 缺失)"}],
    }
    monkeypatch.setattr(
        "app.services.verification.latest_verify_summary", lambda db: fake_summary
    )
    monkeypatch.setattr("app.services.review._ensure_gap", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.services.review.tools.portfolio_review",
        lambda a, c: {"as_of": "2026-01-06", "portfolio_metrics": {}, "positions": [], "signals": []},
    )
    monkeypatch.setattr(
        "app.services.review.tools.portfolio_rebalance_plan", lambda a, c: {}
    )

    def fake_render_all(review, plan, ctx, run_id, art_dir, verify=None):
        art_dir = Path(art_dir)
        for name in ARTIFACT_NAMES:
            (art_dir / name).write_text(name, encoding="utf-8")
        return {"html": str(art_dir / "report.html"), "md": str(art_dir / "report.md")}

    monkeypatch.setattr("app.services.review.report.render_all", fake_render_all)
    monkeypatch.setattr(
        "app.services.review.artifacts.put_run_artifacts",
        lambda run_uid, files: sorted(f"runs/{run_uid}/{n}" for n in files),
    )

    resp = api_client.post(f"/api/v1/portfolios/{pid}/review")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    job_id = body["job_id"]
    cleanup["job_ids"].append(job_id)
    # 门禁失败不拦截：job 照常提交，响应携带数据风险提示
    assert body["data_risk"]["status"] == "fail"
    assert body["data_risk"]["message"]
    assert [f["check"] for f in body["data_risk"]["failed"]] == ["golden_test"]

    job = _wait_job(api_client, job_id)
    assert job["status"] == "done", job
    summary = job["result"]["summary"]
    cleanup["run_uids"].append(job["result"]["run_uid"])
    # 落库的 runs.summary 同样携带 data_risk（供前端历史回看）
    assert summary["data_risk"]["status"] == "fail"
    assert [f["check"] for f in summary["data_risk"]["failed"]] == ["golden_test"]

    with SessionLocal() as db:
        run = db.execute(select(Run).where(Run.job_id == job_id)).scalar_one()
        assert run.portfolio_id == pf_id
        assert run.summary["data_risk"]["failed"][0]["check"] == "golden_test"


def test_review_green_gate_job_succeeds(api_client, ws_root, cleanup, monkeypatch) -> None:
    """用例 2：门禁全绿 → 202 + job 成功；runs 落库、summary/artifact_keys 正确。"""
    pid = _rand("test_rv_ok")
    pf_id, _ = _make_portfolio(cleanup, pid, n=2)
    sync_job = _insert_sync_job(cleanup)
    _insert_verify_all_pass(sync_job)

    fixture_review = {
        "as_of": "2026-01-06",
        "portfolio_metrics": {"day_ret": 0.01, "ret_20d": 0.02},
        "positions": [
            {"secid": "a", "beta": {"state": "above"}},
            {"secid": "b", "beta": {"state": "below"}},
        ],
        "signals": [
            {"signal_id": "s1", "state": "above"},
            {"signal_id": "s2", "state": "above"},
            {"signal_id": "s3", "state": "below"},
            {"signal_id": "s4"},
            {"signal_id": "s5", "state": "bogus"},
        ],
    }
    fixture_plan = {"plan_id": "RP-TEST", "status": "draft", "diff_trades": []}

    def fake_render_all(review, plan, ctx, run_id, art_dir, verify=None):
        art_dir = Path(art_dir)
        for name in ARTIFACT_NAMES:
            (art_dir / name).write_text(name, encoding="utf-8")
        return {"html": str(art_dir / "report.html"), "md": str(art_dir / "report.md")}

    def fake_put(run_uid, files):
        return sorted(f"runs/{run_uid}/{name}" for name in files)

    monkeypatch.setattr("app.services.review._ensure_gap", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.services.review.tools.portfolio_review", lambda a, c: dict(fixture_review)
    )
    monkeypatch.setattr(
        "app.services.review.tools.portfolio_rebalance_plan", lambda a, c: dict(fixture_plan)
    )
    monkeypatch.setattr("app.services.review.report.render_all", fake_render_all)
    monkeypatch.setattr("app.services.review.artifacts.put_run_artifacts", fake_put)

    resp = api_client.post(f"/api/v1/portfolios/{pid}/review")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    job_id = body["job_id"]
    cleanup["job_ids"].append(job_id)
    assert body["poll"] == f"/api/v1/jobs/{job_id}"
    # 门禁全绿：无数据风险提示
    assert body["data_risk"] is None

    job = _wait_job(api_client, job_id)
    assert job["status"] == "done", job
    result = job["result"]
    assert set(result) == {"run_uid", "artifact_keys", "summary"}
    cleanup["run_uids"].append(result["run_uid"])

    # 物化已写工具实读位 <ws>/sandbox/repo/portfolio/{pid}.json
    assert (ws_root / "sandbox" / "repo" / "portfolio" / f"{pid}.json").exists()

    summary = result["summary"]
    assert summary["as_of"] == "2026-01-06"
    assert summary["position_count"] == 2
    assert summary["signal_count"] == 5
    assert summary["signal_states"] == {"above": 1, "below": 1, "neutral": 0}
    assert summary["data_risk"] is None
    assert result["artifact_keys"] == sorted(
        f"runs/{result['run_uid']}/{name}" for name in ARTIFACT_NAMES
    )

    with SessionLocal() as db:
        run = db.execute(select(Run).where(Run.job_id == job_id)).scalar_one()
    assert run.type == "review"
    assert run.portfolio_id == pf_id
    assert run.as_of == date(2026, 1, 6)
    assert run.artifact_keys == result["artifact_keys"]
    assert run.summary["signal_states"] == {"above": 1, "below": 1, "neutral": 0}


def test_get_runs_filters_and_limit(api_client, cleanup) -> None:
    """用例 3：GET /runs 过滤命中、按 created_at desc、limit 生效、limit=0 → 422。"""
    pid = _rand("test_rv_runs")
    pf_id, _ = _make_portfolio(cleanup, pid, n=1)
    job_id = f"job_test_rv_{uuid.uuid4().hex[:10]}"
    with session_scope() as db:
        db.add(
            Job(id=job_id, type="review", portfolio_id=pf_id, status="done",
                params={}, progress=100, created_at=FUTURE, finished_at=FUTURE)
        )
    cleanup["job_ids"].append(job_id)

    early_uid = f"run_test_rv_early_{uuid.uuid4().hex[:6]}"
    late_uid = f"run_test_rv_late_{uuid.uuid4().hex[:6]}"
    with session_scope() as db:
        db.add(
            Run(run_uid=early_uid, job_id=job_id, portfolio_id=pf_id, type="review",
                as_of=date(2026, 1, 1), summary={"n": 1}, artifact_keys=["k1"],
                created_at=datetime(2026, 1, 1, tzinfo=UTC))
        )
        db.add(
            Run(run_uid=late_uid, job_id=job_id, portfolio_id=pf_id, type="review",
                as_of=date(2026, 2, 1), summary={"n": 2}, artifact_keys=["k2", "k3"],
                created_at=datetime(2026, 2, 1, tzinfo=UTC))
        )
    cleanup["run_uids"].extend([early_uid, late_uid])

    resp = api_client.get(
        "/api/v1/runs", params={"type": "review", "portfolio_id": pf_id}
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert [i["run_uid"] for i in items] == [late_uid, early_uid]
    assert set(items[0]) == {
        "run_uid", "type", "portfolio_id", "as_of", "summary", "artifact_keys", "created_at",
    }
    assert items[0]["as_of"] == "2026-02-01"
    assert items[0]["artifact_keys"] == ["k2", "k3"]
    assert items[0]["created_at"] is not None

    limited = api_client.get(
        "/api/v1/runs", params={"type": "review", "portfolio_id": pf_id, "limit": 1}
    )
    assert limited.status_code == 200, limited.text
    assert [i["run_uid"] for i in limited.json()] == [late_uid]

    assert api_client.get("/api/v1/runs", params={"limit": 0}).status_code == 422


def test_review_missing_portfolio_404(api_client) -> None:
    """用例 4：不存在的 pid → 404 NOT_FOUND（统一错误体）。"""
    resp = api_client.post("/api/v1/portfolios/test_rv_no_such_pid/review")
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == ErrorCode.NOT_FOUND.value


def test_run_review_unmaterialized_raises_not_found(tmp_path, pg_ready) -> None:
    """用例 5：组合未物化时 ``run_review`` 抛 ``NOT_FOUND``。"""
    with pytest.raises(AppError) as excinfo:
        run_review(
            str(tmp_path / "ws5"),
            "test_rv_missing_xyz",
            "job_test_rv_missing",
            lambda p, n=None: None,
        )
    assert excinfo.value.code == ErrorCode.NOT_FOUND.value


# ── T-C6：runs.summary.macro_liquidity 冻结字段（纯函数单测，不触 PG）──
from app.services.review import _build_summary, _macro_summary  # noqa: E402


def test_build_summary_macro_fields() -> None:
    """含正常 macro_liquidity 的 review → summary.macro_liquidity 三市场档位 + 全局。"""
    review = {
        "as_of": "2026-09-13",
        "portfolio_metrics": {"nav": 1.05},
        "positions": [{"beta": {"state": "above"}}],
        "signals": [],
        "macro_liquidity": {
            "as_of": "2026-09-10",
            "markets": {"A": {"score": 0.62, "level": 1, "dims": {}},
                        "HK": {"status": "unavailable"},
                        "US": {"score": 0.1, "level": 0, "dims": {}}},
            "global": {"score": 0.4583, "level": 1, "state": "neutral"},
            "indicators": {}, "notes": [],
        },
    }
    ms = _build_summary(review)["macro_liquidity"]
    assert ms == {"markets": {"A": 1, "US": 0}, "global": 1, "global_score": 0.4583,
                  "state": "neutral", "as_of": "2026-09-10"}   # HK unavailable → 不进 markets
    assert _macro_summary(review) == ms


def test_build_summary_macro_unavailable() -> None:
    """unavailable 原样透传（无 markets 键）；缺 macro_liquidity 键 → unavailable 兜底。"""
    bad = _macro_summary({"macro_liquidity": {"status": "unavailable", "error": "x"}})
    assert bad == {"status": "unavailable", "error": "x"}
    assert "markets" not in bad
    assert _macro_summary({}) == {"status": "unavailable", "error": None}


# ── 引擎D：runs.summary.index_trend 冻结字段（2026-10-26，纯函数单测）──
from app.services.review import _index_trend_summary  # noqa: E402


def test_build_summary_index_trend_fields() -> None:
    """正常 review → summary.index_trend 四指数位置/趋势一屏（insufficient 不入摘要）。"""
    review = {
        "as_of": "2026-10-26",
        "portfolio_metrics": {"nav": 1.05},
        "positions": [{"beta": {"state": "above"}}],
        "signals": [],
        "index_trend": {
            "as_of": "2026-10-24",
            "indexes": {
                "ndx": {"position": "high_zone", "pos_q": 88.5, "trend": "up",
                        "ret_20d": 0.041, "close": 26100.3,
                        "name": "纳斯达克综合", "market": "US"},
                "hstech": {"position": "low_zone", "pos_q": 12.4, "trend": "down",
                           "ret_20d": -0.062, "close": 3888.1,
                           "name": "恒生科技", "market": "HK"},
                "chinext": {"status": "insufficient", "note": "序列不足",
                            "name": "创业板指", "market": "A"},
            },
        },
    }
    its = _build_summary(review)["index_trend"]
    assert its == {
        "as_of": "2026-10-24",
        "indexes": {
            "ndx": {"position": "high_zone", "pos_q": 88.5, "trend": "up",
                    "ret_20d": 0.041, "close": 26100.3,
                    "name": "纳斯达克综合", "market": "US"},
            "hstech": {"position": "low_zone", "pos_q": 12.4, "trend": "down",
                       "ret_20d": -0.062, "close": 3888.1,
                       "name": "恒生科技", "market": "HK"},
        },                                    # insufficient 条目不入摘要
    }
    assert _index_trend_summary(review) == its


def test_build_summary_index_trend_unavailable() -> None:
    """unavailable 原样透传；缺 index_trend 键 → unavailable 兜底（error=None）。"""
    bad = _index_trend_summary({"index_trend": {"status": "unavailable", "error": "数据未同步"}})
    assert bad == {"status": "unavailable", "error": "数据未同步"}
    assert "indexes" not in bad
    assert _index_trend_summary({}) == {"status": "unavailable", "error": None}

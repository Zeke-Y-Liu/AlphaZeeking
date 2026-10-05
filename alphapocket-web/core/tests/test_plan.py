"""T4.1 调仓评估单测试（``POST /portfolios/{id}/rebalance-plan`` + ``GET /plans/*``）。

- 不修改 ``conftest.py``（并行任务在改它），本文件自带 fixture；
- 用真实 PG + ``tmp_path`` 造 workspace；**MinIO 不可达 → 上传必须 mock**；
- 不依赖外网；PG 不可达时整体 ``pytest.skip``；
- 门禁用 ``monkeypatch app.services.verification.latest_verify_summary`` 造全绿/unknown
  （确定性，避免受其它测试残留影响）；
- 所有落库行用 ``test_pl_`` 唯一前缀并在用例结束后清理，
  顺序：rebalance_plans → runs → verify_results → jobs → positions → portfolios → securities
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
from applib import tools
from applib.ctx import Ctx
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text

from app.core.config import get_settings
from app.core.errors import ErrorCode
from app.db.session import SessionLocal, session_scope
from app.models import (
    Job,
    Portfolio,
    Position,
    RebalancePlan,
    Run,
    Security,
    VerifyResult,
)
from app.services import plan_svc
from app.services.jobs import JobRunner

FUTURE = datetime(2099, 1, 1, tzinfo=UTC)


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
    """收集本用例落库的 plan/run/job/secid/pid，结束后按 FK 顺序清理。"""
    state: dict = {
        "job_ids": [],
        "run_uids": [],
        "secids": [],
        "pids": [],
        "plan_uids": [],
    }
    yield state
    with session_scope() as db:
        ids = []
        if state["pids"]:
            ids = [
                row
                for (row,) in db.execute(
                    select(Portfolio.id).where(Portfolio.pid.in_(state["pids"]))
                ).all()
            ]
        if state["plan_uids"]:
            db.execute(
                delete(RebalancePlan).where(RebalancePlan.plan_uid.in_(state["plan_uids"]))
            )
        if ids:
            db.execute(delete(RebalancePlan).where(RebalancePlan.portfolio_id.in_(ids)))
        if state["run_uids"]:
            db.execute(delete(Run).where(Run.run_uid.in_(state["run_uids"])))
        if state["job_ids"]:
            db.execute(delete(Run).where(Run.job_id.in_(state["job_ids"])))
            db.execute(delete(VerifyResult).where(VerifyResult.job_id.in_(state["job_ids"])))
            db.execute(delete(Job).where(Job.id.in_(state["job_ids"])))
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
    secids = [f"test_pl_{uuid.uuid4().hex[:8]}.SH" for _ in range(n)]
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
            pid=pid, name="调仓组合", note="t4.1", base_ccy="CNY",
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


def _insert_job(cleanup: dict, jtype: str, portfolio_id: int) -> str:
    """插入一个 done 的 job（Run.job_id 外键依赖）。"""
    jid = f"job_test_pl_{uuid.uuid4().hex[:10]}"
    with session_scope() as db:
        db.add(
            Job(
                id=jid, type=jtype, portfolio_id=portfolio_id, status="done",
                params={}, progress=100, created_at=FUTURE, finished_at=FUTURE,
            )
        )
    cleanup["job_ids"].append(jid)
    return jid


def _synthetic_review(asset_type: str = "ETF", n: int = 2, weight: float = 20.0) -> dict:
    """合成 review：n 个持仓，beta.state="below" → 触发 n 笔 evaluate_sell（to=12.0）。"""
    positions = [
        {
            "secid": f"51030{i}.SH",
            "name": f"标的{i}",
            "weight": weight,
            "asset_type": asset_type,
            "settle": "A股 T+1",
            "cost": "印花税卖出0.05%+佣金",
            "beta": {"state": "below", "ma20_break": -0.05, "mom_20d": -0.03},
            "alpha": {"vol_250": 0.2},
        }
        for i in range(n)
    ]
    return {"as_of": "2026-09-30", "positions": positions}


def _fixture_plan(pid: str) -> dict:
    """最小合法 plan（覆盖 summary 读取的全部键）。"""
    return {
        "plan_id": f"RP-20260930-{pid.upper()}",
        "portfolio": pid,
        "generated_by": "engine_a+engine_b（规则版）",
        "as_of": "2026-09-30",
        "diff_trades": [
            {
                "side": "evaluate_sell", "secid": "510300.SH", "name": "沪深300ETF",
                "from": 20.0, "to": 12.0, "reason": "r", "evidence": "E2",
                "settle": "A股 T+1", "cost": "印花税卖出0.05%+佣金",
            }
        ],
        "turnover_est": "8.0%（单边）",
        "cost_est_cny": 40,
        "cost_note": "ETF/LOF：双边佣金万2.5×2（免印花税）",
        "status": "draft → 待用户确认（引擎永不直接动仓位）",
    }


def _green_gate(monkeypatch) -> None:
    """monkeypatch 门禁为全绿（确定性）。"""
    monkeypatch.setattr(
        "app.services.verification.latest_verify_summary",
        lambda db: {"status": "pass", "job_id": "job_test_pl_sync",
                    "created_at": None, "checks": [], "failed": []},
    )


def _unknown_gate(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.verification.latest_verify_summary",
        lambda db: {"status": "unknown", "job_id": None, "created_at": None,
                    "checks": [], "failed": []},
    )


def _mock_plan_io(monkeypatch, pid: str) -> None:
    """mock plan 生成与上传（MinIO 不可达）；materialize_from_pg 走真实 PG。"""
    monkeypatch.setattr(
        "app.services.plan_svc.tools.portfolio_rebalance_plan",
        lambda a, c: _fixture_plan(pid),
    )
    monkeypatch.setattr(
        "app.services.plan_svc.artifacts.put_run_artifacts",
        lambda ru, files: sorted(f"runs/{ru}/{name}" for name in files),
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


# ── 1. C2 单测（核心）──────────────────────────────────────────


def test_c2_etf_cost_est_fixed(tmp_path, monkeypatch) -> None:
    """ETF 两笔卖出（20.0→12.0）：turnover 8.0%，cost_est ≈ ¥40（旧口径误为 ¥400）。"""
    ctx = Ctx(root=tmp_path)
    monkeypatch.setattr(
        "applib.tools.portfolio_review", lambda a, c: _synthetic_review("ETF")
    )
    plan = tools.portfolio_rebalance_plan({"portfolio": "test_pl_c2"}, ctx)

    assert len(plan["diff_trades"]) == 2
    assert plan["turnover_est"] == "8.0%（单边）"
    assert abs(plan["cost_est_cny"] - 40) <= 1
    assert plan["cost_est_cny"] == 40


def test_c2_stock_cost_includes_stamp_duty(tmp_path, monkeypatch) -> None:
    """STOCK 卖出另计印花税：单笔 佣金20+印花税40=60，两笔 ¥120 > 纯佣金 ¥40。"""
    ctx = Ctx(root=tmp_path)
    monkeypatch.setattr(
        "applib.tools.portfolio_review", lambda a, c: _synthetic_review("STOCK")
    )
    plan = tools.portfolio_rebalance_plan({"portfolio": "test_pl_c2s"}, ctx)

    assert plan["turnover_est"] == "8.0%（单边）"
    assert plan["cost_est_cny"] == 120
    assert plan["cost_est_cny"] > 40


# ── 2. supersede 不变量 ─────────────────────────────────────────


def test_supersede_on_regenerate(ws_root, cleanup, monkeypatch) -> None:
    """同组合连续生成两次 plan：旧 draft → superseded，新 → draft；plan_uid 追加后缀。"""
    pid = _rand("test_pl_sup")
    pf_id, _ = _make_portfolio(cleanup, pid, n=2)
    job1 = _insert_job(cleanup, "rebalance_plan", pf_id)
    job2 = _insert_job(cleanup, "rebalance_plan", pf_id)
    monkeypatch.setattr(
        "app.services.plan_svc.tools.portfolio_rebalance_plan",
        lambda a, c: _fixture_plan(pid),
    )
    monkeypatch.setattr(
        "app.services.plan_svc.artifacts.put_run_artifacts",
        lambda ru, files: sorted(f"runs/{ru}/{name}" for name in files),
    )

    r1 = plan_svc.run_plan_job(str(ws_root), pid, job1, lambda p, n=None: None)
    r2 = plan_svc.run_plan_job(str(ws_root), pid, job2, lambda p, n=None: None)
    cleanup["plan_uids"].extend([r1["plan_uid"], r2["plan_uid"]])

    base = f"RP-20260930-{pid.upper()}"
    assert r1["plan_uid"] == base
    assert r2["plan_uid"] == f"{base}-2"          # 冲突后缀
    assert r1["plan_id"] == r2["plan_id"] == base  # applib 原始 plan_id 不变
    assert r2["summary"]["cost_est_cny"] == 40
    assert r2["summary"]["turnover_est"] == "8.0%（单边）"
    assert r2["summary"]["n_trades"] == 1
    assert len(r2["artifact_keys"]) == 1
    assert r2["artifact_keys"][0].endswith("/rebalance_plan.json")
    assert r2["artifact_keys"][0].startswith("runs/rp_")

    with SessionLocal() as db:
        plans = (
            db.execute(
                select(RebalancePlan)
                .where(RebalancePlan.portfolio_id == pf_id)
                .order_by(RebalancePlan.id)
            )
            .scalars()
            .all()
        )
    assert len(plans) == 2
    assert plans[0].status == "superseded"
    assert plans[1].status == "draft"

    with SessionLocal() as db:
        runs = (
            db.execute(
                select(Run).where(Run.job_id.in_([job1, job2])).order_by(Run.id)
            )
            .scalars()
            .all()
        )
    assert [r.type for r in runs] == ["rebalance_plan", "rebalance_plan"]
    assert runs[1].as_of == date(2026, 9, 30)
    assert runs[1].summary["cost_est_cny"] == 40
    assert runs[1].artifact_keys == r2["artifact_keys"]


# ── 3. 端点：404 / 409 / 成功 ───────────────────────────────────


def test_plan_missing_portfolio_404(api_client) -> None:
    """不存在的 pid → 404 NOT_FOUND（优先于门禁）。"""
    resp = api_client.post("/api/v1/portfolios/test_pl_no_such_pid/rebalance-plan")
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == ErrorCode.NOT_FOUND.value


def test_plan_gate_failed_409_no_job(api_client, cleanup, monkeypatch) -> None:
    """门禁非全绿 → 409 VERIFY_GATE_FAILED，且不提交 job。"""
    pid = _rand("test_pl_gate")
    pf_id, _ = _make_portfolio(cleanup, pid, n=1)
    _unknown_gate(monkeypatch)
    called = {"n": 0}

    def _boom(self, *args, **kwargs):
        called["n"] += 1
        raise AssertionError("门禁失败时不应提交 job")

    monkeypatch.setattr(JobRunner, "submit", _boom)

    resp = api_client.post(f"/api/v1/portfolios/{pid}/rebalance-plan")
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == ErrorCode.VERIFY_GATE_FAILED.value
    assert called["n"] == 0

    with SessionLocal() as db:
        n = db.execute(
            select(func.count()).select_from(Job).where(
                Job.type == "rebalance_plan", Job.portfolio_id == pf_id
            )
        ).scalar_one()
    assert n == 0


def test_plan_green_gate_job_succeeds(api_client, ws_root, cleanup, monkeypatch) -> None:
    """门禁全绿 → 202 + job 成功；runs + rebalance_plans(draft) 落库。"""
    pid = _rand("test_pl_ok")
    pf_id, _ = _make_portfolio(cleanup, pid, n=2)
    _green_gate(monkeypatch)
    _mock_plan_io(monkeypatch, pid)

    resp = api_client.post(f"/api/v1/portfolios/{pid}/rebalance-plan")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    job_id = body["job_id"]
    cleanup["job_ids"].append(job_id)
    assert body["poll"] == f"/api/v1/jobs/{job_id}"

    job = _wait_job(api_client, job_id)
    assert job["status"] == "done", job
    result = job["result"]
    assert set(result) == {"plan_uid", "plan_id", "artifact_keys", "summary"}
    assert result["plan_uid"] == f"RP-20260930-{pid.upper()}"
    assert result["plan_id"] == f"RP-20260930-{pid.upper()}"
    assert result["summary"] == {
        "plan_uid": f"RP-20260930-{pid.upper()}",
        "as_of": "2026-09-30",
        "turnover_est": "8.0%（单边）",
        "cost_est_cny": 40,
        "n_trades": 1,
    }
    assert len(result["artifact_keys"]) == 1
    assert result["artifact_keys"][0].endswith("/rebalance_plan.json")
    cleanup["plan_uids"].append(result["plan_uid"])

    # 物化已写工具实读位 <ws>/sandbox/repo/portfolio/{pid}.json
    assert (ws_root / "sandbox" / "repo" / "portfolio" / f"{pid}.json").exists()

    with SessionLocal() as db:
        run = db.execute(select(Run).where(Run.job_id == job_id)).scalar_one()
        plan_row = db.execute(
            select(RebalancePlan).where(RebalancePlan.run_id == run.id)
        ).scalar_one()
    assert run.type == "rebalance_plan"
    assert run.portfolio_id == pf_id
    assert run.as_of == date(2026, 9, 30)
    assert run.artifact_keys == result["artifact_keys"]
    assert run.summary["cost_est_cny"] == 40
    assert plan_row.status == "draft"
    assert plan_row.plan_uid == result["plan_uid"]
    assert plan_row.payload["plan_id"] == result["plan_id"]


# ── 4. GET 详情 / 列表 ──────────────────────────────────────────


def test_get_plan_detail_and_list_order(api_client, cleanup) -> None:
    """``GET /plans/{id}`` 200/404；``GET /portfolios/{pid}/plans`` draft 优先。"""
    pid = _rand("test_pl_get")
    pf_id, _ = _make_portfolio(cleanup, pid, n=1)
    early_uid = f"RP-20260101-{pid.upper()}"
    late_uid = f"RP-20260202-{pid.upper()}"
    with session_scope() as db:
        db.add(
            RebalancePlan(
                plan_uid=early_uid, portfolio_id=pf_id, run_id=None, status="draft",
                payload={"plan_id": early_uid, "cost_est_cny": 11},
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        db.add(
            RebalancePlan(
                plan_uid=late_uid, portfolio_id=pf_id, run_id=None, status="superseded",
                payload={"plan_id": late_uid, "cost_est_cny": 22},
                created_at=datetime(2026, 2, 2, tzinfo=UTC),
            )
        )
    cleanup["plan_uids"].extend([early_uid, late_uid])
    with SessionLocal() as db:
        early_id = db.execute(
            select(RebalancePlan.id).where(RebalancePlan.plan_uid == early_uid)
        ).scalar_one()

    # 详情：按数字 id
    resp = api_client.get(f"/api/v1/plans/{early_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {
        "id", "plan_uid", "portfolio_id", "run_id", "status", "payload", "created_at",
    }
    assert body["plan_uid"] == early_uid
    assert body["status"] == "draft"
    assert body["payload"]["cost_est_cny"] == 11

    # 详情：按 plan_uid
    resp_uid = api_client.get(f"/api/v1/plans/{late_uid}")
    assert resp_uid.status_code == 200, resp_uid.text
    assert resp_uid.json()["plan_uid"] == late_uid

    # 详情 404
    assert api_client.get("/api/v1/plans/999999999").status_code == 404
    assert api_client.get("/api/v1/plans/RP-NOPE").status_code == 404

    # 列表：draft 优先（早的 draft 排在晚的 superseded 之前）
    lst = api_client.get(f"/api/v1/portfolios/{pid}/plans")
    assert lst.status_code == 200, lst.text
    items = lst.json()
    assert [i["plan_uid"] for i in items] == [early_uid, late_uid]
    assert set(items[0]) == {
        "id", "plan_uid", "portfolio_id", "run_id", "status", "cost_est_cny", "created_at",
    }
    assert items[0]["status"] == "draft"
    assert items[0]["cost_est_cny"] == 11
    assert items[1]["status"] == "superseded"

    # 列表：不存在组合 → 404
    assert api_client.get("/api/v1/portfolios/test_pl_nope/plans").status_code == 404

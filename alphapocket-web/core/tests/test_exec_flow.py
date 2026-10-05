"""T4.2 / T4.3 / T4.4 执行单全链测试（§9.5）。

覆盖：

- **T4.2**：``POST /plans/{id}/exec-orders`` 同步生成（2 卖单 / 整手 / 费用）、
  plan 404、plan 过老 409 ``DATA_STALE``、``GET /exec-orders/{id}`` 详情/404、
  capital=100,000 时整手取整；
- **T4.3**：``confirm`` 缺 ACK → 422 ``ACK_REQUIRED``；完整状态机
  ``draft→confirmed→executed``；非法 fills（越界 secid / 价格≤0 / 非整手 / 方向不符）
  422；未确认即登记 422；``cancelled`` 路径；
- **T4.4**：``GET /portfolios/{id}/rebalance-history`` 时间线顺序与 kinds；
  ``GET /audit`` 过滤。

不依赖外网；MinIO 上传与 review 现算均 mock（确定性）。PG 不可达 → 整体 skip。
清理顺序（FK）：audit_log → exec_orders → rebalance_plans → runs → jobs →
positions → portfolios → securities。
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text

from app.core.config import get_settings
from app.core.errors import ErrorCode
from app.db.session import SessionLocal, session_scope
from app.models import (
    AuditLog,
    ExecOrder,
    Job,
    Portfolio,
    Position,
    RebalancePlan,
    Run,
    Security,
)
from app.services import exec_svc

AS_OF = "2026-09-30"


# ── fixtures ────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def pg_ready():
    from app.db.session import engine

    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL 不可达：{type(exc).__name__}: {exc}")
    yield engine


@pytest.fixture
def ws_root(tmp_path, monkeypatch) -> Iterator[Path]:
    root = tmp_path / "ws"
    monkeypatch.setenv("AP_WORKSPACE_ROOT", str(root))
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


@pytest.fixture
def api_client(ws_root, pg_ready, monkeypatch) -> Iterator[TestClient]:
    # MinIO 不可达 → 上传 mock（返回 key 列表，形状同 artifacts.put_run_artifacts）
    monkeypatch.setattr(
        exec_svc.artifacts,
        "put_run_artifacts",
        lambda ru, files: sorted(f"runs/{ru}/{name}" for name in files),
    )
    from app.main import create_app

    with TestClient(create_app()) as client:
        yield client
    get_settings.cache_clear()


@pytest.fixture
def cleanup(pg_ready) -> Iterator[dict]:
    state: dict[str, list] = {"pids": [], "secids": [], "exec_uids": []}
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
        if state["exec_uids"]:
            db.execute(delete(AuditLog).where(AuditLog.entity_id.in_(state["exec_uids"])))
            db.execute(delete(ExecOrder).where(ExecOrder.exec_uid.in_(state["exec_uids"])))
        if ids:
            db.execute(delete(ExecOrder).where(ExecOrder.portfolio_id.in_(ids)))
            db.execute(delete(RebalancePlan).where(RebalancePlan.portfolio_id.in_(ids)))
            db.execute(delete(Run).where(Run.portfolio_id.in_(ids)))
            db.execute(delete(Job).where(Job.portfolio_id.in_(ids)))
            db.execute(delete(Position).where(Position.portfolio_id.in_(ids)))
            db.execute(delete(Portfolio).where(Portfolio.id.in_(ids)))
        if state["secids"]:
            db.execute(delete(Security).where(Security.secid.in_(state["secids"])))


# ── helpers ─────────────────────────────────────────────────────


def _rand(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _make_portfolio(cleanup: dict, pid: str, n: int = 2) -> int:
    secids = [f"test_ex_{uuid.uuid4().hex[:8]}.SH" for _ in range(n)]
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
            pid=pid, name="执行单组合", note="t4.2/3/4", base_ccy="CNY",
            strategy={}, status="active",
        )
        db.add(pf)
        db.flush()
        for secid in secids:
            db.add(Position(portfolio_id=pf.id, secid=secid, weight=Decimal("50")))
        pf_id = pf.id
    cleanup["secids"].extend(secids)
    cleanup["pids"].append(pid)
    return pf_id


def _synthetic_review() -> dict:
    """2 笔 ETF 卖出（20%→12%）；close 取整便于断言。"""
    positions = [
        {
            "secid": "510300.SH", "name": "沪深300ETF", "industry": "A股宽基指数",
            "weight": 20.0, "asset_type": "ETF", "settle": "A股 T+1",
            "cost": "ETF 免印花税·佣金约万2.5", "close": 4.0, "day_pct": 0.1,
            "alpha": {"vol_250": 0.2},
            "beta": {"state": "below", "band_low": 3.9, "band_high": 4.1},
        },
        {
            "secid": "159915.SZ", "name": "创业板ETF", "industry": "创业板指数",
            "weight": 20.0, "asset_type": "ETF", "settle": "A股 T+1",
            "cost": "ETF 免印花税·佣金约万2.5", "close": 2.0, "day_pct": -0.2,
            "alpha": {"vol_250": 0.3},
            "beta": {"state": "below", "band_low": 1.95, "band_high": 2.05},
        },
    ]
    return {
        "id": "multi5", "name": "多资产五标的", "as_of": AS_OF, "base_ccy": "CNY",
        "next_trade_date": "2026-10-08", "holiday_days": 7,
        "positions": positions,
    }


def _plan_payload(pid: str, as_of: str = AS_OF) -> dict:
    return {
        "plan_id": f"RP-{as_of.replace('-', '')}-{pid.upper()}",
        "portfolio": pid, "generated_by": "engine_a+engine_b（规则版）", "as_of": as_of,
        "diff_trades": [
            {"side": "evaluate_sell", "secid": "510300.SH", "name": "沪深300ETF",
             "from": 20.0, "to": 12.0, "reason": "r", "evidence": "E2",
             "settle": "A股 T+1", "cost": "ETF 免印花税·佣金约万2.5"},
            {"side": "evaluate_sell", "secid": "159915.SZ", "name": "创业板ETF",
             "from": 20.0, "to": 12.0, "reason": "r", "evidence": "E2",
             "settle": "A股 T+1", "cost": "ETF 免印花税·佣金约万2.5"},
        ],
        "turnover_est": "8.0%（单边）", "cost_est_cny": 40,
        "cost_note": "ETF/LOF：双边佣金万2.5×2（免印花税）",
        "status": "draft → 待用户确认（引擎永不直接动仓位）",
    }


def _insert_plan(cleanup: dict, pid: str, as_of: str = AS_OF) -> tuple[int, str]:
    pf_id = _make_portfolio(cleanup, pid, n=2)
    uid = f"RP-{as_of.replace('-', '')}-{pid.upper()}"
    with session_scope() as db:
        row = RebalancePlan(
            plan_uid=uid, portfolio_id=pf_id, run_id=None, status="draft",
            payload=_plan_payload(pid, as_of),
        )
        db.add(row)
        db.flush()
        plan_id = row.id
    return plan_id, uid


def _mock_review(monkeypatch, review: dict | None = None) -> dict:
    rv = review or _synthetic_review()
    monkeypatch.setattr(exec_svc, "_review_now", lambda ws_root, pid: rv)
    return rv


def _create_order(api_client: TestClient, plan_id: int, capital: float | None = None) -> dict:
    body = {} if capital is None else {"capital_cny": capital}
    resp = api_client.post(f"/api/v1/plans/{plan_id}/exec-orders", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── T4.2 生成 ───────────────────────────────────────────────────


def test_create_exec_order_success_and_detail(api_client, cleanup, monkeypatch) -> None:
    pid = _rand("test_ex_ok")
    plan_id, _ = _insert_plan(cleanup, pid)
    _mock_review(monkeypatch)

    body = _create_order(api_client, plan_id)
    cleanup["exec_uids"].append(body["exec_uid"])

    assert body["status"] == "draft"
    assert body["capital_cny"] == 1_000_000.0
    assert body["plan_id"] == plan_id
    assert body["plan_uid"] == f"RP-20260930-{pid.upper()}"
    sheet = body["sheet"]
    assert sheet["summary"]["n_orders"] == 2
    assert sheet["summary"]["n_sell"] == 2
    assert [o["order_shares"] for o in sheet["orders"] if o["order_shares"]] == [-20000, -40000]
    # 全部整手
    assert all(o["order_shares"] % 100 == 0 for o in sheet["orders"])
    # 时间线含 create
    assert [t["action"] for t in body["timeline"]] == ["create"]

    # 落库：jobs(done) + runs(exec_orders) + exec_orders(draft) + 3 产物 key
    with SessionLocal() as db:
        row = db.execute(
            select(ExecOrder).where(ExecOrder.exec_uid == body["exec_uid"])
        ).scalar_one()
        assert row.status == "draft"
        run = db.execute(
            select(Run).where(Run.run_uid == body["exec_uid"])
        ).scalar_one()
        assert run.type == "exec_orders"
        job = db.execute(select(Job).where(Job.id == run.job_id)).scalar_one()
        assert job.status == "done" and job.type == "exec_orders"
    assert len(sheet["orders"]) == 2

    # GET 详情（按 exec_uid 与数字 id 均可）
    got = api_client.get(f"/api/v1/exec-orders/{body['exec_uid']}")
    assert got.status_code == 200, got.text
    assert got.json()["exec_uid"] == body["exec_uid"]
    got_num = api_client.get(f"/api/v1/exec-orders/{body['id']}")
    assert got_num.status_code == 200
    assert api_client.get("/api/v1/exec-orders/EX-NOPE").status_code == 404


def test_create_exec_order_plan_404(api_client) -> None:
    resp = api_client.post("/api/v1/plans/999999999/exec-orders", json={})
    assert resp.status_code == 404
    assert resp.json()["code"] == ErrorCode.NOT_FOUND.value


def test_create_exec_order_stale_plan_409(api_client, cleanup, monkeypatch) -> None:
    """plan.as_of 非最新交易日 → 409 DATA_STALE，提示先重跑评估。"""
    pid = _rand("test_ex_stale")
    plan_id, _ = _insert_plan(cleanup, pid, as_of="2026-09-29")
    _mock_review(monkeypatch)  # review as_of=2026-09-30

    resp = api_client.post(f"/api/v1/plans/{plan_id}/exec-orders", json={})
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == ErrorCode.DATA_STALE.value
    assert resp.json()["detail"]["plan_as_of"] == "2026-09-29"
    assert resp.json()["detail"]["latest_as_of"] == AS_OF


def test_create_exec_order_lot_rounding_small_capital(api_client, cleanup, monkeypatch) -> None:
    """capital=100,000：委托数量恒为 100 整数倍（整手取整）。"""
    pid = _rand("test_ex_lot")
    plan_id, _ = _insert_plan(cleanup, pid)
    _mock_review(monkeypatch)

    body = _create_order(api_client, plan_id, capital=100_000)
    cleanup["exec_uids"].append(body["exec_uid"])
    sheet = body["sheet"]
    assert body["capital_cny"] == 100_000.0
    assert all(o["order_shares"] % 100 == 0 for o in sheet["orders"])
    assert all(o["lots"] == abs(o["order_shares"]) // 100 for o in sheet["orders"])
    # 20%→12% @close4.0 & 2.0 → -2000 / -4000
    assert [o["order_shares"] for o in sheet["orders"] if o["order_shares"]] == [-2000, -4000]


# ── T4.3 状态机 ─────────────────────────────────────────────────


def _confirmed_order(api_client, cleanup, monkeypatch) -> dict:
    pid = _rand("test_ex_flow")
    plan_id, _ = _insert_plan(cleanup, pid)
    _mock_review(monkeypatch)
    body = _create_order(api_client, plan_id)
    cleanup["exec_uids"].append(body["exec_uid"])
    return body


def test_confirm_requires_ack_422(api_client, cleanup, monkeypatch) -> None:
    body = _confirmed_order(api_client, cleanup, monkeypatch)
    resp = api_client.post(f"/api/v1/exec-orders/{body['exec_uid']}/confirm", json={})
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == ErrorCode.ACK_REQUIRED.value

    # 状态未变
    with SessionLocal() as db:
        row = db.execute(
            select(ExecOrder).where(ExecOrder.exec_uid == body["exec_uid"])
        ).scalar_one()
        assert row.status == "draft"


def test_full_state_machine_draft_confirmed_executed(api_client, cleanup, monkeypatch) -> None:
    body = _confirmed_order(api_client, cleanup, monkeypatch)
    uid = body["exec_uid"]

    # fill 未确认 → 422
    bad = api_client.post(
        f"/api/v1/exec-orders/{uid}/fill",
        json={"fills": [{"secid": "510300.SH", "price": 4.0, "shares": -20000}]},
    )
    assert bad.status_code == 422, bad.text

    # confirm
    conf = api_client.post(f"/api/v1/exec-orders/{uid}/confirm", json={"ack_discipline": True})
    assert conf.status_code == 200, conf.text
    assert conf.json()["status"] == "confirmed"
    assert conf.json()["confirmed_by"] == "system"
    assert conf.json()["confirmed_at"]

    # 重复 confirm → 422（draft→confirmed 之外）
    again = api_client.post(f"/api/v1/exec-orders/{uid}/confirm", json={"ack_discipline": True})
    assert again.status_code == 422

    # 部分成交：各登记一笔（少于委托量）→ partial=True
    filled = api_client.post(
        f"/api/v1/exec-orders/{uid}/fill",
        json={"fills": [
            {"secid": "510300.SH", "price": 4.0, "shares": -10000},
            {"secid": "159915.SZ", "price": 2.0, "shares": -40000},
        ]},
    )
    assert filled.status_code == 200, filled.text
    out = filled.json()
    assert out["status"] == "executed"
    assert out["fills"]["n"] == 2
    assert out["fills"]["partial"] is True
    assert out["filled_at"]

    # 审计三条齐全：create / confirm / fill
    with SessionLocal() as db:
        actions = [
            a
            for (a,) in db.execute(
                select(AuditLog.action)
                .where(AuditLog.entity == "exec_order", AuditLog.entity_id == uid)
                .order_by(AuditLog.id)
            ).all()
        ]
    assert actions == ["create", "confirm", "fill"]

    # executed 后不可再 cancel
    assert api_client.post(
        f"/api/v1/exec-orders/{uid}/cancel", json={}
    ).status_code == 422


@pytest.mark.parametrize(
    ("fills", "reason"),
    [
        ([{"secid": "NO_SUCH.SH", "price": 4.0, "shares": -100}], "越界 secid"),
        ([{"secid": "510300.SH", "price": 0, "shares": -100}], "价格≤0"),
        ([{"secid": "510300.SH", "price": 4.0, "shares": -150}], "非整手"),
        ([{"secid": "510300.SH", "price": 4.0, "shares": 100}], "方向不符（委托为卖出）"),
    ],
)
def test_fill_invalid_422(api_client, cleanup, monkeypatch, fills, reason) -> None:
    body = _confirmed_order(api_client, cleanup, monkeypatch)
    uid = body["exec_uid"]
    api_client.post(f"/api/v1/exec-orders/{uid}/confirm", json={"ack_discipline": True})

    resp = api_client.post(f"/api/v1/exec-orders/{uid}/fill", json={"fills": fills})
    assert resp.status_code == 422, f"{reason}: {resp.text}"
    assert resp.json()["code"] == ErrorCode.VALIDATION_ERROR.value


def test_cancel_paths(api_client, cleanup, monkeypatch) -> None:
    # draft → cancelled
    b1 = _confirmed_order(api_client, cleanup, monkeypatch)
    r1 = api_client.post(f"/api/v1/exec-orders/{b1['exec_uid']}/cancel", json={"reason": "误生成"})
    assert r1.status_code == 200, r1.text
    assert r1.json()["status"] == "cancelled"

    # confirmed → cancelled
    b2 = _confirmed_order(api_client, cleanup, monkeypatch)
    api_client.post(f"/api/v1/exec-orders/{b2['exec_uid']}/confirm", json={"ack_discipline": True})
    r2 = api_client.post(f"/api/v1/exec-orders/{b2['exec_uid']}/cancel", json={})
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "cancelled"

    # 已 cancelled 再取消 → 422
    assert api_client.post(
        f"/api/v1/exec-orders/{b1['exec_uid']}/cancel", json={}
    ).status_code == 422


# ── T4.4 历史与审计 ─────────────────────────────────────────────


def test_rebalance_history_and_audit(api_client, cleanup, monkeypatch) -> None:
    body = _confirmed_order(api_client, cleanup, monkeypatch)
    uid = body["exec_uid"]
    api_client.post(f"/api/v1/exec-orders/{uid}/confirm", json={"ack_discipline": True})
    api_client.post(
        f"/api/v1/exec-orders/{uid}/fill",
        json={"fills": [{"secid": "510300.SH", "price": 4.0, "shares": -20000}]},
    )

    pid = cleanup["pids"][-1]
    hist = api_client.get(f"/api/v1/portfolios/{pid}/rebalance-history")
    assert hist.status_code == 200, hist.text
    data = hist.json()
    kinds = [it["kind"] for it in data["items"]]
    assert kinds[0] == "plan"           # 最早为 plan
    assert "exec_order" in kinds
    assert "fills" in kinds
    assert kinds.index("exec_order") < kinds.index("fills")
    assert data["count"] == len(data["items"])

    # 不存在组合 → 404
    assert api_client.get(
        "/api/v1/portfolios/test_ex_nope/rebalance-history"
    ).status_code == 404

    # 审计过滤：entity=exec_order & entity_id=uid → create/confirm/fill
    aud = api_client.get(f"/api/v1/audit?entity=exec_order&entity_id={uid}")
    assert aud.status_code == 200, aud.text
    acts = [i["action"] for i in aud.json()["items"]]
    assert acts == ["fill", "confirm", "create"]  # 时间倒序
    assert aud.json()["count"] == 3

    # 组合列表端点
    lst = api_client.get(f"/api/v1/portfolios/{pid}/exec-orders")
    assert lst.status_code == 200
    assert [i["exec_uid"] for i in lst.json()] == [uid]

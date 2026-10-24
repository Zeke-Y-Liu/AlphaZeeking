"""核心基准链路与锚点（RG-2，冒烟成员）：定义「核心功能的最小可回归基准」。

三层防护：
1. ``test_baseline_fixture_frozen``——冻结 fixture 与生成器 bitwise 一致（数据面漂移即红）；
2. ``test_core_api_flow``——核心 API 冒烟链路（注册→建组合→详情→更新→审计），
   与真实 PG 交互、离线打桩（不触外网）；
3. ``test_backtest_anchor``——等权组合回测对冻结锚点逐位对账（计算内核不漂移）。

依赖：真实 PG（pg_ready skip 语义同其它端点测试）；回测锚点仅依赖 applib 纯函数。
"""
from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text

from app.db.session import SessionLocal, session_scope
from app.models import AuditLog, Security

sys.path.insert(0, str(Path(__file__).resolve().parent / "baseline"))

BASELINE = Path(__file__).resolve().parent / "baseline"
WS = BASELINE / "ws"
ANCHOR = BASELINE / "expected" / "anchor_backtest.json"


# ── fixtures（同库其它端点测试的约定）─────────────────────────────


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
def api_client(pg_ready, _no_auto_sync) -> Iterator[TestClient]:
    from app.main import create_app

    with TestClient(create_app()) as client:
        yield client


@pytest.fixture
def cleanup(pg_ready) -> Iterator[list[str]]:
    secids: list[str] = []
    yield secids
    with session_scope() as db:
        from app.models import Portfolio, Position

        # FK 顺序：先组合 positions/portfolio（可能引用这些标的），再 securities/审计
        pf_ids = list(db.execute(select(Portfolio.id).where(
            Portfolio.pid.in_(secids))).scalars())
        if pf_ids:
            db.execute(delete(Position).where(Position.portfolio_id.in_(pf_ids)))
            db.execute(delete(Portfolio).where(Portfolio.id.in_(pf_ids)))
        for s in secids:
            db.execute(delete(AuditLog).where(AuditLog.entity == "security",
                                              AuditLog.entity_id == s))
            db.execute(delete(AuditLog).where(AuditLog.entity == "portfolio",
                                              AuditLog.entity_id == s))
        if secids:
            db.execute(delete(Security).where(Security.secid.in_(secids)))


# ── 1. fixture 冻结咬合 ────────────────────────────────────────────


@pytest.mark.smoke
def test_baseline_fixture_frozen() -> None:
    """冻结基准数据 == 生成器即时产物（bitwise）：任何一方漂移立即红。"""
    import make_baseline as MB

    fresh = MB.build_bars()
    frozen = json.loads((WS / "market" / "daily_bars.json").read_text(encoding="utf-8"))
    assert frozen == fresh, "冻结基准数据与生成器不一致（改公式后须重跑 make_baseline.py 审阅冻结）"
    assert set(frozen) == {"600901.SH", "600902.SH", "511900.SH"}
    assert all(len(v) == 300 for v in frozen.values())


# ── 2. 核心 API 冒烟链路 ───────────────────────────────────────────


@pytest.mark.smoke
def test_core_api_flow(api_client, cleanup, monkeypatch) -> None:
    """注册 → 建组合 → 详情 → 更新 → 审计：核心功能最小闭环（离线打桩，不触外网）。"""
    from app.api.v1 import securities as sec_mod

    monkeypatch.setattr(sec_mod, "_probe_reachable",
                        lambda sina, market, is_fund: [{"dt": "2026-10-23", "open": 1,
                                                        "high": 1, "low": 1, "close": 1,
                                                        "volume": 1}])
    monkeypatch.setattr(sec_mod, "_realtime_names", lambda secids: {})

    # 注册基准甲（新注册 → 自动同步经 conftest 桩 runner 记录，可断言）
    r = api_client.post("/api/v1/securities/register", json={"code": "600901"})
    assert r.status_code == 200, r.text
    assert r.json()["secid"] == "600901.SH"
    cleanup.append("600901.SH")

    # 幂等重注册不触发新同步（M11 语义顺带回归）
    r = api_client.post("/api/v1/securities/register", json={"code": "600901"})
    assert r.status_code == 200
    assert api_client.get("/api/v1/securities/search", params={"q": "600901"}).json()[
        0]["secid"] == "600901.SH"

    # 建组合（600901/600902/511900 须全部注册）
    for code in ("600902", "511900"):
        r = api_client.post("/api/v1/securities/register", json={"code": code})
        assert r.status_code == 200, r.text
        cleanup.append(r.json()["secid"])

    pid = "pf_rg_baseline"
    with session_scope() as db:
        from app.models import Portfolio, Position

        db.execute(delete(Position).where(Position.portfolio_id.in_(
            select(Portfolio.id).where(Portfolio.pid == pid))))
        db.execute(delete(Portfolio).where(Portfolio.pid == pid))
    cleanup.append(pid)

    r = api_client.post("/api/v1/portfolios", json={
        "pid": pid, "name": "基准链路组合",
        "positions": [{"secid": "600901.SH", "weight": 33.34},
                      {"secid": "600902.SH", "weight": 33.33},
                      {"secid": "511900.SH", "weight": 33.33}]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["position_count"] == 3

    # 详情 + 更新 + 审计
    r = api_client.get(f"/api/v1/portfolios/{pid}")
    assert r.status_code == 200 and r.json()["pid"] == pid
    r = api_client.put(f"/api/v1/portfolios/{pid}", json={"name": "基准链路组合v2"})
    assert r.status_code == 200 and r.json()["name"] == "基准链路组合v2"
    with SessionLocal() as db:
        n = db.execute(select(AuditLog.id).where(AuditLog.entity == "portfolio",
                                                 AuditLog.entity_id == pid)).all()
        assert n, "组合操作应落审计"


# ── 3. 回测锚点对账 ────────────────────────────────────────────────


@pytest.mark.smoke
def test_backtest_anchor() -> None:
    """等权组合回测与冻结锚点逐位一致（纯函数：同输入必同输出）。"""
    from applib import backtest as BT

    bars = json.loads((WS / "market" / "daily_bars.json").read_text(encoding="utf-8"))
    pf = {"id": "rg_baseline", "name": "基准 · 甲乙+ETF 等权", "base_ccy": "CNY",
          "positions": [{"secid": "600901.SH", "name": "基准甲", "market": "A", "weight": 33.34},
                        {"secid": "600902.SH", "name": "基准乙", "market": "A", "weight": 33.33},
                        {"secid": "511900.SH", "name": "基准ETF", "market": "A", "weight": 33.33}]}
    out = BT.run_backtest(bars, pf, benchmark_secid="600901.SH")
    anchor = json.loads(ANCHOR.read_text(encoding="utf-8"))

    sm, asm = out["strategy_metrics"], anchor["strategy_metrics"]
    for key, frozen in asm.items():
        got = sm[key]
        assert got == frozen, f"锚点漂移 {key}: {got!r} != {frozen!r}（计算内核行为变化）"
    assert out["n_days"] == anchor["n_days"]
    assert out["n_rebalances"] == anchor["n_rebalances"]
    assert out["benchmark"]["secid"] == anchor["benchmark"]["secid"]
    for key, frozen in anchor["benchmark"]["metrics"].items():
        assert out["benchmark"]["metrics"][key] == frozen, f"基准指标漂移 {key}"

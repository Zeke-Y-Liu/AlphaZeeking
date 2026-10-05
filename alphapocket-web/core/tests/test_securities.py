"""T1.3 标的注册与检索测试。

- 不修改 ``conftest.py``（并行任务在改它），本文件自带 fixture；
- PG 不可达时 ``pytest.skip``；用例确定性、不依赖外网（探测函数 monkeypatch）；
- 真实网络冒烟用例需 ``AP_NET_TESTS=1`` 显式开启，默认 skip；
- 每个用例自行清理本次新增的 securities / audit_log 行，避免污染 PG 与并行任务。
"""
from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text

from app.api.v1 import securities as sec_mod
from app.db.session import SessionLocal, session_scope
from app.models import AuditLog, Security

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
def api_client(pg_ready) -> Iterator[TestClient]:
    from app.main import create_app

    with TestClient(create_app()) as client:
        yield client


@pytest.fixture
def cleanup(pg_ready) -> Iterator[list[str]]:
    """收集本用例落库的 secid，结束后删除对应 securities 与 audit_log 行。"""
    secids: list[str] = []
    yield secids
    if secids:
        with session_scope() as db:
            db.execute(
                delete(AuditLog).where(
                    AuditLog.entity == "security", AuditLog.entity_id.in_(secids)
                )
            )
            db.execute(delete(Security).where(Security.secid.in_(secids)))


# ── helpers ─────────────────────────────────────────────────────


def _fake_bars(n: int = 10) -> list[dict]:
    return [
        {"date": f"2026-09-{d:02d}", "open": 1.0, "high": 1.1,
         "low": 0.9, "close": 1.0, "volume": 1000}
        for d in range(1, n + 1)
    ]


def _force_clean(secid: str, cleanup: list[str]) -> None:
    """清理可能残留的同 secid 行（含审计），并登记为本次用例所有。"""
    with session_scope() as db:
        db.execute(
            delete(AuditLog).where(
                AuditLog.entity == "security", AuditLog.entity_id == secid
            )
        )
        db.execute(delete(Security).where(Security.secid == secid))
    cleanup.append(secid)


def _get(secid: str) -> Security | None:
    with SessionLocal() as db:
        return db.get(Security, secid)


def _count_securities() -> int:
    with SessionLocal() as db:
        return int(db.scalar(select(func.count()).select_from(Security)) or 0)


def _count_audit(secid: str) -> int:
    with SessionLocal() as db:
        return int(
            db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.entity == "security", AuditLog.entity_id == secid)
            )
            or 0
        )


# ── 纯函数：代码规范化与市场前缀（无需 PG）─────────────────────


@pytest.mark.parametrize(
    ("raw", "secid", "sina", "is_fund"),
    [
        ("600519", "600519.SH", "sh600519", False),
        ("sh600519", "600519.SH", "sh600519", False),
        ("600519.SH", "600519.SH", "sh600519", False),
        ("002014", "002014.SZ", "sz002014", False),
        ("sz002014", "002014.SZ", "sz002014", False),
        ("300750", "300750.SZ", "sz300750", False),
        ("688981", "688981.SH", "sh688981", False),
        ("510300", "510300.SH", "sh510300", True),
        ("159915", "159915.SZ", "sz159915", True),
    ],
)
def test_resolve_secid_rules(raw, secid, sina, is_fund) -> None:
    """规范化 + 冻结前缀规则（§9.2 T1.3）。"""
    assert sec_mod._resolve_secid(raw) == (
        secid.split(".")[0], secid, sina, is_fund
    )


@pytest.mark.parametrize("bad", ["abc", "12345", "1234567", "60051x"])
def test_normalize_code_rejects_malformed(bad: str) -> None:
    from app.core.errors import AppError, ErrorCode

    with pytest.raises(AppError) as excinfo:
        sec_mod._normalize_code(bad)
    assert excinfo.value.code == ErrorCode.VALIDATION_ERROR.value


def test_market_prefix_rejects_unknown() -> None:
    from app.core.errors import AppError, ErrorCode

    with pytest.raises(AppError) as excinfo:
        sec_mod._resolve_secid("999999")
    assert excinfo.value.code == ErrorCode.VALIDATION_ERROR.value


# ── 端点用例 ────────────────────────────────────────────────────


def test_register_new_stock(api_client, pg_ready, cleanup, monkeypatch) -> None:
    """注册 600519（股票）→ STOCK / 600519.SH / 缺省 settle&cost / 写审计。"""
    monkeypatch.setattr(sec_mod, "_probe_reachable", lambda sina, is_fund: _fake_bars())
    _force_clean("600519.SH", cleanup)

    resp = api_client.post("/api/v1/securities/register", json={"code": "600519"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["secid"] == "600519.SH"
    assert body["code"] == "600519"
    assert body["asset_type"] == "STOCK"
    assert body["sina_symbol"] == "sh600519"
    assert body["settle"] == "A股 T+1"
    assert body["cost"] == "印花税卖出0.05%+佣金"
    assert body["market"] == "A"
    assert body["source"] == "user"
    # 名称无本地来源 → 按约定回退为 code（不伪造）
    assert body["name"] == "600519"
    assert _count_audit("600519.SH") >= 1


def test_register_existing_is_idempotent(api_client, pg_ready, monkeypatch) -> None:
    """注册 510300（seed 已存在）→ 幂等返回既有记录、无新增行、不触发探测。"""
    existing = _get("510300.SH")
    if existing is None:
        pytest.skip("seed 未灌入 510300.SH，跳过幂等用例")

    def _boom(*_args, **_kwargs):
        raise AssertionError("既有标的注册不应触发可达性探测")

    monkeypatch.setattr(sec_mod, "_probe_reachable", _boom)
    before = _count_securities()

    resp = api_client.post("/api/v1/securities/register", json={"code": "510300"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["secid"] == "510300.SH"
    assert body["name"] == existing.name
    assert body["asset_type"] == existing.asset_type
    assert _count_securities() == before, "幂等命中不应新增 securities 行"


def test_register_new_fund_defaults_etf(api_client, pg_ready, cleanup, monkeypatch) -> None:
    """注册 159915（1 开头 → sz 基金）→ 默认 ETF / 159915.SZ。"""
    monkeypatch.setattr(sec_mod, "_probe_reachable", lambda sina, is_fund: _fake_bars())
    _force_clean("159915.SZ", cleanup)

    resp = api_client.post("/api/v1/securities/register", json={"code": "159915"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["secid"] == "159915.SZ"
    assert body["sina_symbol"] == "sz159915"
    assert body["asset_type"] == "ETF"
    assert body["settle"] == "T+1"
    assert body["cost"] == "ETF 免印花税·佣金约万2.5"


def test_register_explicit_asset_type_and_ftype(api_client, pg_ready, cleanup, monkeypatch) -> None:
    """显式 asset_type=LOF 与 ftype 落库（入参优先于默认）。"""
    monkeypatch.setattr(sec_mod, "_probe_reachable", lambda sina, is_fund: _fake_bars())
    _force_clean("161725.SZ", cleanup)

    resp = api_client.post(
        "/api/v1/securities/register",
        json={"code": "sz161725", "asset_type": "lof", "ftype": "lof", "name": "白酒LOF"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["secid"] == "161725.SZ"
    assert body["asset_type"] == "LOF"
    assert body["ftype"] == "lof"
    assert body["name"] == "白酒LOF"
    assert body["cost"] == "LOF 免印花税·佣金约万2.5"


@pytest.mark.parametrize("bad", ["abc", "12345", "999999", ""])
def test_register_invalid_code_422(api_client, pg_ready, bad) -> None:
    """非法/无法识别代码 → 422 统一错误体。"""
    resp = api_client.post("/api/v1/securities/register", json={"code": bad})
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert set(body) == {"code", "message", "detail"}


def test_register_probe_failure_422(api_client, pg_ready, cleanup, monkeypatch) -> None:
    """格式合法但探测不可达 → 422 SECURITY_NOT_FOUND 且不落库。"""
    monkeypatch.setattr(sec_mod, "_probe_reachable", lambda sina, is_fund: [])
    _force_clean("600000.SH", cleanup)

    resp = api_client.post("/api/v1/securities/register", json={"code": "600000"})
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "SECURITY_NOT_FOUND"
    assert _get("600000.SH") is None


def test_search_chinese_prefix_hits_seed(api_client, pg_ready) -> None:
    """search 中文名称前缀命中 seed 标的。"""
    if _get("601058.SH") is None:
        pytest.skip("seed 未灌入 601058.SH，跳过中文前缀检索用例")

    resp = api_client.get("/api/v1/securities/search", params={"q": "赛轮"})
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert isinstance(rows, list) and rows
    assert any(row["secid"] == "601058.SH" for row in rows)


def test_search_code_prefix_and_empty(api_client, pg_ready) -> None:
    """代码前缀命中；空 q 返回列表且不超过 limit。"""
    code_resp = api_client.get("/api/v1/securities/search", params={"q": "600"})
    assert code_resp.status_code == 200
    rows = code_resp.json()
    if _get("600660.SH") is not None:
        assert any(row["secid"] == "600660.SH" for row in rows)

    empty_resp = api_client.get("/api/v1/securities/search", params={"q": "", "limit": 20})
    assert empty_resp.status_code == 200
    recent = empty_resp.json()
    assert isinstance(recent, list)
    assert len(recent) <= 20


# ── 真实网络冒烟（默认 skip；AP_NET_TESTS=1 显式开启）───────────


@pytest.mark.skipif(os.environ.get("AP_NET_TESTS") != "1", reason="需 AP_NET_TESTS=1 且外网可达")
def test_probe_reachable_real_network() -> None:
    pytest.importorskip("akshare")
    bars = sec_mod._probe_reachable("sh600519", False)
    assert isinstance(bars, list) and bars

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
    ("raw", "secid", "sina", "is_fund", "market"),
    [
        # A股（冻结前缀规则，§9.2 T1.3）
        ("600519", "600519.SH", "sh600519", False, "A"),
        ("sh600519", "600519.SH", "sh600519", False, "A"),
        ("600519.SH", "600519.SH", "sh600519", False, "A"),
        ("002014", "002014.SZ", "sz002014", False, "A"),
        ("sz002014", "002014.SZ", "sz002014", False, "A"),
        ("300750", "300750.SZ", "sz300750", False, "A"),
        ("688981", "688981.SH", "sh688981", False, "A"),
        ("510300", "510300.SH", "sh510300", True, "A"),
        ("159915", "159915.SZ", "sz159915", True, "A"),
        # V4-β 港股（自动判定 + 短代码补零至 5 位）
        ("00700", "00700.HK", "hk00700", False, "HK"),
        ("700", "00700.HK", "hk00700", False, "HK"),
        ("hk00700", "00700.HK", "hk00700", False, "HK"),
        ("00700.HK", "00700.HK", "hk00700", False, "HK"),
        # V4-β 美股（自动判定，1-6 位字母）
        ("AAPL", "AAPL.US", "gb_aapl", False, "US"),
        ("aapl", "AAPL.US", "gb_aapl", False, "US"),
        ("AAPL.US", "AAPL.US", "gb_aapl", False, "US"),
    ],
)
def test_resolve_secid_rules(raw, secid, sina, is_fund, market) -> None:
    """自动判定 + 规范化 + 冻结前缀规则（§9.2 T1.3 / V4-β 多市场）。"""
    assert sec_mod._resolve_secid(raw) == (
        secid.split(".")[0], secid, sina, is_fund, market
    )


@pytest.mark.parametrize(
    ("raw", "market", "secid", "sina"),
    [
        ("12345", "HK", "12345.HK", "hk12345"),
        ("700", "HK", "00700.HK", "hk00700"),
        ("AAPL", "US", "AAPL.US", "gb_aapl"),
        ("600519", "A", "600519.SH", "sh600519"),
    ],
)
def test_resolve_secid_explicit_market(raw, market, secid, sina) -> None:
    """显式 market 入参按对应市场规范化。"""
    code, got_secid, got_sina, _fund, got_mkt = sec_mod._resolve_secid(raw, market)
    assert (code, got_secid, got_sina, got_mkt) == (secid.split(".")[0], secid, sina, market)


@pytest.mark.parametrize("bad", ["abc", "12345", "1234567", "60051x"])
def test_normalize_code_rejects_malformed(bad: str) -> None:
    """A 股代码规范化拒绝非法形态（V4-β：abc/12345 转由 US/HK 市场解释）。"""
    from app.core.errors import AppError, ErrorCode

    with pytest.raises(AppError) as excinfo:
        sec_mod._normalize_a_code(bad)
    assert excinfo.value.code == ErrorCode.VALIDATION_ERROR.value


def test_normalize_us_rejects_dotted() -> None:
    """美股暂不支持带点多类别代码（BRK.B）→ 422。"""
    from app.core.errors import AppError, ErrorCode

    with pytest.raises(AppError) as excinfo:
        sec_mod._normalize_us_code("BRK.B")
    assert excinfo.value.code == ErrorCode.VALIDATION_ERROR.value


def test_market_prefix_rejects_unknown() -> None:
    from app.core.errors import AppError, ErrorCode

    with pytest.raises(AppError) as excinfo:
        sec_mod._resolve_secid("999999", "A")
    assert excinfo.value.code == ErrorCode.VALIDATION_ERROR.value


# ── 端点用例 ────────────────────────────────────────────────────


def _patch_offline(monkeypatch, ggt_codes: set[str] | None = None) -> None:
    """打桩去网络：探测返回伪 K 线；实时名称/港股通名单确定性返回。"""
    monkeypatch.setattr(
        sec_mod, "_probe_reachable",
        lambda sina, market, is_fund: _fake_bars(),
    )
    monkeypatch.setattr(sec_mod, "_realtime_names", lambda secids: {})
    if ggt_codes is not None:
        monkeypatch.setattr(sec_mod, "_fetch_ggt_codes", lambda: ggt_codes)


def test_register_new_stock(api_client, pg_ready, cleanup, monkeypatch) -> None:
    """注册 600941（股票）→ STOCK / 600941.SH / 缺省 settle&cost / 写审计。

    刻意避开 seed/运行态标的（同 test_register_us_stock 注：force_clean+cleanup
    硬删行，共库缺陷下会误删运行库数据；此前用 600519 会删掉用户真实注册的行）。
    """
    _patch_offline(monkeypatch)
    _force_clean("600941.SH", cleanup)

    resp = api_client.post("/api/v1/securities/register", json={"code": "600941"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["secid"] == "600941.SH"
    assert body["code"] == "600941"
    assert body["asset_type"] == "STOCK"
    assert body["sina_symbol"] == "sh600941"
    assert body["settle"] == "A股 T+1"
    assert body["cost"] == "印花税卖出0.05%+佣金"
    assert body["market"] == "A"
    assert body["currency"] == "CNY"
    assert body["hk_connect"] is None
    assert body["source"] == "user"
    # 名称无本地来源 → 按约定回退为 code（不伪造）
    assert body["name"] == "600941"
    assert _count_audit("600941.SH") >= 1


def test_register_existing_is_idempotent(api_client, pg_ready, monkeypatch,
                                         _no_auto_sync) -> None:
    """注册 510300（seed 已存在）→ 幂等返回既有记录、无新增行、不触发探测与自动同步。"""
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
    assert _no_auto_sync == [], "幂等命中不应触发自动同步"


# ── 自动数据同步触发（M11：新注册 → 尽力而为增量同步）─────────


def test_register_new_triggers_bg_sync(api_client, pg_ready, cleanup, monkeypatch,
                                       _no_auto_sync) -> None:
    """新注册 → 恰好提交一次增量同步 job（conftest 桩 runner 记录，不触外网）。"""
    _patch_offline(monkeypatch)
    _force_clean("601398.SH", cleanup)

    resp = api_client.post("/api/v1/securities/register", json={"code": "601398"})

    assert resp.status_code == 200, resp.text
    assert len(_no_auto_sync) == 1, "新注册应触发一次自动同步（幂等注册则不触发）"
    job = _no_auto_sync[0]
    assert job["type"] == "data_sync"
    assert job["params"] == {"mode": "incremental", "secid": None}
    assert job["lock"] == "sync"


def test_register_survives_bg_sync_failure(api_client, pg_ready, cleanup, monkeypatch,
                                           _no_auto_sync) -> None:
    """触发链路异常（runner 未初始化等）→ 静默吞掉，注册响应不受影响（红线）。"""
    _patch_offline(monkeypatch)
    _force_clean("601988.SH", cleanup)

    import app.main as main_mod

    def _raise(*_args, **_kwargs):
        raise RuntimeError("job runner 未初始化（lifespan 未启动）")

    monkeypatch.setattr(main_mod, "get_job_runner_sync", _raise)

    resp = api_client.post("/api/v1/securities/register", json={"code": "601988"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["secid"] == "601988.SH"
    assert _get("601988.SH") is not None, "同步失败不应回滚已注册标的"


def test_register_new_fund_defaults_etf(api_client, pg_ready, cleanup, monkeypatch) -> None:
    """注册 159915（1 开头 → sz 基金）→ 默认 ETF / 159915.SZ。"""
    _patch_offline(monkeypatch)
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
    _patch_offline(monkeypatch)
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


def test_register_hk_ggt_stock(api_client, pg_ready, cleanup, monkeypatch) -> None:
    """注册 03690（美团，港股通名单内）→ HK / HKD / hk_connect=True / 港股缺省费用。"""
    _patch_offline(monkeypatch, ggt_codes={"00700", "03690"})
    _force_clean("03690.HK", cleanup)

    resp = api_client.post("/api/v1/securities/register", json={"code": "3690"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["secid"] == "03690.HK"
    assert body["sina_symbol"] == "hk03690"
    assert body["market"] == "HK"
    assert body["asset_type"] == "STOCK"
    assert body["currency"] == "HKD"
    assert body["hk_connect"] is True
    assert body["settle"].startswith("港股")
    assert _count_audit("03690.HK") >= 1


def test_register_hk_non_ggt_stock(api_client, pg_ready, cleanup, monkeypatch) -> None:
    """注册 09618（不在名单）→ hk_connect=False（明确标记非港股通）。"""
    _patch_offline(monkeypatch, ggt_codes=set())        # 名单为空（降级路径）
    _force_clean("09618.HK", cleanup)

    resp = api_client.post("/api/v1/securities/register", json={"code": "9618"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["secid"] == "09618.HK"
    assert body["market"] == "HK"
    assert body["hk_connect"] is False
    assert body["currency"] == "HKD"


def test_register_us_stock(api_client, pg_ready, cleanup, monkeypatch) -> None:
    """注册 TSLA（美股）→ US / USD / hk_connect=None / 美股缺省费用。

    刻意避开 seed 标的（NVDA.US 等）：_force_clean + cleanup 会硬删该 secid 的
    securities/audit 行——测试与运行共库的既有隔离缺陷下会误删运行库数据
    （此前用 nvda 每跑一轮套件就删掉运行库的 NVDA.US 行，连带数据同步
    _prune_orphans 清掉其 bars）。
    """
    _patch_offline(monkeypatch)
    _force_clean("TSLA.US", cleanup)

    resp = api_client.post("/api/v1/securities/register", json={"code": "tsla"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["secid"] == "TSLA.US"
    assert body["sina_symbol"] == "gb_tsla"
    assert body["market"] == "US"
    assert body["asset_type"] == "STOCK"
    assert body["currency"] == "USD"
    assert body["hk_connect"] is None
    assert body["settle"] == "美股 T+0"
    assert _count_audit("TSLA.US") >= 1


def test_register_hk_rejects_non_stock(api_client, pg_ready, monkeypatch) -> None:
    """港股/美股不支持 asset_type=ETF → 422（用非 seed 标的，避开幂等命中）。"""
    _patch_offline(monkeypatch)
    resp = api_client.post(
        "/api/v1/securities/register", json={"code": "0001", "asset_type": "ETF"}
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("bad", ["60051x", "", "1234567", "BRK.B"])
def test_register_invalid_code_422(api_client, pg_ready, bad) -> None:
    """非法/无法识别代码 → 422 统一错误体（abc/12345 现为合法 US/HK 代码）。"""
    resp = api_client.post("/api/v1/securities/register", json={"code": bad})
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert set(body) == {"code", "message", "detail"}


def test_register_probe_failure_422(api_client, pg_ready, cleanup, monkeypatch) -> None:
    """格式合法但探测不可达 → 422 SECURITY_NOT_FOUND 且不落库。"""
    monkeypatch.setattr(
        sec_mod, "_probe_reachable", lambda sina, market, is_fund: []
    )
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
    bars = sec_mod._probe_reachable("sh600519", "A", False)
    assert isinstance(bars, list) and bars


@pytest.mark.skipif(os.environ.get("AP_NET_TESTS") != "1", reason="需 AP_NET_TESTS=1 且外网可达")
@pytest.mark.parametrize(
    ("sina", "market", "min_bars"),
    [("hk00700", "HK", 1), ("AAPL", "US", 1)],
)
def test_probe_reachable_hk_us_real_network(sina, market, min_bars) -> None:
    """港股/美股探测真实网络冒烟（新浪接口可达性 + 数据非空）。"""
    pytest.importorskip("akshare")
    bars = sec_mod._probe_reachable(sina, market, False)
    assert isinstance(bars, list) and len(bars) >= min_bars


def test_search_market_filter(api_client, pg_ready) -> None:
    """search 按市场过滤（V4-β）：market=HK 只回港股。"""
    if _get("00700.HK") is None:
        pytest.skip("seed 未灌入 00700.HK，跳过市场过滤用例")

    resp = api_client.get("/api/v1/securities/search", params={"market": "HK"})
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert rows, "market=HK 应至少命中 seed 港股"
    assert all(row["market"] == "HK" for row in rows)
    assert any(row["secid"] == "00700.HK" for row in rows)
    assert all(row.get("hk_connect") is not None for row in rows)

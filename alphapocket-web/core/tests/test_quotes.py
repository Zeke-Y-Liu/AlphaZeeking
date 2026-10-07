"""V4-β 实时行情端点测试（/market/quotes、/market/ggt）。

- 不依赖外网：monkeypatch applib 的 ``fetch_realtime_quotes``/``fetch_fx``/
  ``fetch_ggt_components``；
- PG 不可达时跳过（元数据富化读 securities）；
- Redis 不可达 fail-open（缓存路径用 fakeredis 语义打桩）。
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.v1 import quotes as quotes_mod


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
def api_client(pg_ready) -> Iterator[TestClient]:
    from app.main import create_app

    with TestClient(create_app()) as client:
        yield client


_FAKE_QUOTES = {
    "00700.HK": {"name": "腾讯控股", "price": 428.2, "open": 426.4, "prev_close": 423.0,
                 "high": 429.2, "low": 425.6, "chg": 5.2, "pct": 1.229, "volume": 10030081.0,
                 "amount": 4286181540.0, "time": "2026/10/06 16:08", "currency": "HKD",
                 "source": "sina"},
    "AAPL.US": {"name": "苹果", "price": 333.41, "open": 332.28, "prev_close": 332.89,
                "high": 334.38, "low": 330.62, "chg": 0.52, "pct": 0.16, "volume": 7080722.0,
                "amount": 35237898.0, "time": "2026-10-06 22:48:26", "currency": "USD",
                "source": "sina"},
    "601058.SH": {"name": "赛轮集团", "price": 13.24, "open": 13.13, "prev_close": 13.11,
                  "high": 13.3, "low": 13.05, "chg": 0.13, "pct": 0.99, "volume": 1234500.0,
                  "amount": 16340000.0, "time": "2026-10-06 15:00:00", "currency": "CNY",
                  "source": "sina"},
}
_FAKE_FX = {"HKD_CNY": 0.8543, "USD_CNY": 6.705, "as_of": "2026-10-06",
            "time": "22:47:59", "source": "sina"}


@pytest.fixture
def offline_quotes(monkeypatch):
    """打桩：实时行情/汇率确定性返回；缓存 fail-open（Redis 直连错误）。"""
    monkeypatch.setattr(
        "applib.datasource.fetch_realtime_quotes", lambda secids: dict(_FAKE_QUOTES)
    )
    monkeypatch.setattr("applib.datasource.fetch_fx", lambda: dict(_FAKE_FX))

    class _BrokenRedis:
        def get(self, *_a, **_k):
            raise ConnectionError("redis down")

        def set(self, *_a, **_k):
            raise ConnectionError("redis down")

    monkeypatch.setattr(quotes_mod, "get_redis", lambda: _BrokenRedis())


def _item(body: dict, secid: str) -> dict:
    return next(q for q in body["quotes"] if q["secid"] == secid)


def test_quotes_mixed_markets(api_client, offline_quotes) -> None:
    """三市场混合批量：行情/币种/人民币折算/汇率随附。"""
    resp = api_client.get(
        "/api/v1/market/quotes",
        params={"secids": "00700.HK,AAPL.US,601058.SH"},
    )
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    assert body["cached"] is False
    assert body["fx"]["HKD_CNY"] == pytest.approx(0.8543)
    assert body["fx"]["USD_CNY"] == pytest.approx(6.705)

    hk = _item(body, "00700.HK")
    assert hk["market"] == "HK"
    assert hk["currency"] == "HKD"
    assert hk["price"] == pytest.approx(428.2)
    assert hk["price_cny"] == pytest.approx(428.2 * 0.8543, abs=1e-3)
    assert hk["source"] == "sina"
    assert hk["market_label"] == "港股"

    us = _item(body, "AAPL.US")
    assert us["market"] == "US"
    assert us["price_cny"] == pytest.approx(333.41 * 6.705, abs=1e-2)

    a = _item(body, "601058.SH")
    assert a["market"] == "A"
    assert a["price_cny"] == pytest.approx(13.24)      # A股原值


def test_quotes_meta_enriched_from_pg(api_client, offline_quotes) -> None:
    """seed 标的：hk_connect/asset_type 从 PG 富化（00700 港股通=True）。"""
    resp = api_client.get("/api/v1/market/quotes", params={"secids": "00700.HK"})
    assert resp.status_code == 200, resp.text
    hk = _item(resp.json(), "00700.HK")
    assert hk["hk_connect"] is True
    assert hk["asset_type"] == "STOCK"


def test_quotes_unavailable_flagged(api_client, offline_quotes) -> None:
    """双源失败的标的：带 error 字段，不影响其余条目。"""
    resp = api_client.get(
        "/api/v1/market/quotes", params={"secids": "09999.HK,00700.HK"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    bad = _item(body, "09999.HK")
    assert bad["error"] == "unavailable"
    assert _item(body, "00700.HK")["price"] == pytest.approx(428.2)


def test_quotes_validation(api_client, offline_quotes) -> None:
    """空 secids / 未知后缀 / 超 50 只 → 422。"""
    assert api_client.get("/api/v1/market/quotes", params={"secids": ""}).status_code == 422
    assert (
        api_client.get("/api/v1/market/quotes", params={"secids": "FOO.XX"}).status_code == 422
    )
    too_many = ",".join(f"S{i:05d}.HK" for i in range(51))
    assert (
        api_client.get("/api/v1/market/quotes", params={"secids": too_many}).status_code == 422
    )


def test_ggt_endpoint(api_client, monkeypatch) -> None:
    """港股通名单端点：雪球源 → 代码列表 + count。"""
    monkeypatch.setattr(
        "applib.datasource.fetch_ggt_components",
        lambda: (["00005", "00700", "00388"], "xueqiu"),
    )
    resp = api_client.get("/api/v1/market/ggt")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 3
    assert body["source"] == "xueqiu"
    assert "00700" in body["codes"]

"""T2.4 产物服务测试（``GET /runs/{run_uid}/artifacts/{file}``）。

- 用 ``create_app()`` + ``TestClient``，鉴权方式与其它 core 测试一致（本端点无 auth 依赖）；
- **MinIO 不可达 → 全部用 monkeypatch 替换 ``app.services.artifacts.get_object`` 返回伪造流**，
  不连云、不连外网；PG 不可达时整体 ``pytest.skip``（与 test_review.py 一致）；
- 覆盖：正常 200 与响应头、扩展名 content-type 映射、对象缺失 404、路径穿越 404。
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from minio.error import S3Error
from sqlalchemy import text

from app.core.config import get_settings

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


# ── fakes / helpers ─────────────────────────────────────────────


class _FakeStat:
    """伪造 MinIO stat（仅需 ``size``）。"""

    def __init__(self, size: int) -> None:
        self.size = size
        self.content_type = "application/octet-stream"


class _FakeResponse:
    """伪造 MinIO 取流响应：``stream(chunk_size)`` 分块、``close``/``release_conn`` 记账。"""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.closed = False
        self.released = False

    def stream(self, chunk_size: int) -> Iterator[bytes]:
        for i in range(0, len(self._data), chunk_size):
            yield self._data[i : i + chunk_size]

    def close(self) -> None:
        self.closed = True

    def release_conn(self) -> None:
        self.released = True


def _patch_ok(monkeypatch, data: bytes, calls: list[str]) -> None:
    """把 ``get_object`` 替换为返回伪造流的实现，并记录被调用的 key。"""

    def fake_get_object(key: str) -> tuple[Any, Any]:
        calls.append(key)
        return _FakeResponse(data), _FakeStat(len(data))

    monkeypatch.setattr("app.services.artifacts.get_object", fake_get_object)


def _patch_missing(monkeypatch, calls: list[str]) -> None:
    """把 ``get_object`` 替换为抛 S3 ``NoSuchKey``（→ 404）。"""

    def fake_get_object(key: str) -> tuple[Any, Any]:
        calls.append(key)
        raise S3Error(None, "NoSuchKey", "not found", key, "req-id", "host-id")

    monkeypatch.setattr("app.services.artifacts.get_object", fake_get_object)


def _patch_forbid(monkeypatch, calls: list[str]) -> None:
    """把 ``get_object`` 替换为一旦被调用即失败（用于穿越用例断言未被调用）。"""

    def fake_get_object(key: str) -> tuple[Any, Any]:
        calls.append(key)
        raise AssertionError(f"不应以逃逸 key 调用取流：{key}")

    monkeypatch.setattr("app.services.artifacts.get_object", fake_get_object)


# ── 用例 ────────────────────────────────────────────────────────


def test_artifact_html_ok(api_client, monkeypatch) -> None:
    """用例 1：正常 html → 200 + text/html + 必需响应头 + body 等于伪造字节。"""
    data = b"<html><body>report</body></html>"
    calls: list[str] = []
    _patch_ok(monkeypatch, data, calls)

    resp = api_client.get("/api/v1/runs/run_x/artifacts/report.html")

    assert resp.status_code == 200, resp.text
    assert "text/html" in resp.headers["content-type"]
    assert resp.headers["x-frame-options"] == "SAMEORIGIN"
    assert "report.html" in resp.headers["content-disposition"]
    assert resp.headers["content-disposition"].startswith("inline")
    assert resp.headers["content-length"] == str(len(data))
    assert resp.content == data
    assert calls == ["runs/run_x/report.html"]


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("report.md", "text/markdown"),
        ("review.json", "application/json"),
        ("exec_order.csv", "text/csv"),
        ("blob.bin", "application/octet-stream"),
    ],
)
def test_artifact_content_type_mapping(api_client, monkeypatch, filename, expected) -> None:
    """用例 2：扩展名（小写）→ content-type；未知扩展名 → octet-stream 照常 200。"""
    data = b"payload"
    calls: list[str] = []
    _patch_ok(monkeypatch, data, calls)

    resp = api_client.get(f"/api/v1/runs/run_x/artifacts/{filename}")

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith(expected)
    assert resp.content == data
    assert calls == [f"runs/run_x/{filename}"]


def test_artifact_missing_404(api_client, monkeypatch) -> None:
    """用例 3：对象不存在（S3 NoSuchKey）→ 404 NOT_FOUND（统一错误体）。"""
    calls: list[str] = []
    _patch_missing(monkeypatch, calls)

    resp = api_client.get("/api/v1/runs/run_x/artifacts/report.html")

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "NOT_FOUND"
    assert calls == ["runs/run_x/report.html"]


@pytest.mark.parametrize(
    "url",
    [
        # 编码斜杠穿越（Starlette 解码后含 / 与 ..）
        "/api/v1/runs/run_x/artifacts/..%2F..%2Fetc%2Fpasswd",
        # 纯 .. 段
        "/api/v1/runs/run_x/artifacts/..",
        # 反斜杠穿越（Windows 风格）
        "/api/v1/runs/run_x/artifacts/a%5Cb.txt",
        # run_uid 段含 ..
        "/api/v1/runs/..%2E/artifacts/report.html",
    ],
)
def test_artifact_path_traversal_404(api_client, monkeypatch, url) -> None:
    """用例 4：路径段含 /、\\、.. 或为空 → 404，且不以逃逸 key 调用取流。"""
    calls: list[str] = []
    _patch_forbid(monkeypatch, calls)

    resp = api_client.get(url)

    assert resp.status_code == 404, resp.text
    assert calls == []


def test_artifact_run_uid_normal_chars_ok(api_client, monkeypatch) -> None:
    """用例 5：run_uid 含下划线/数字/字母正常 → 200。"""
    data = b"# report"
    calls: list[str] = []
    _patch_ok(monkeypatch, data, calls)

    run_uid = "run_2026_10_05_abc123"
    resp = api_client.get(f"/api/v1/runs/{run_uid}/artifacts/report.md")

    assert resp.status_code == 200, resp.text
    assert calls == [f"runs/{run_uid}/report.md"]

"""/healthz 存活 + 依赖探测自测（T1.1）。

探测为纯 TCP，结果随环境而异；此处校验「永远 200、status 与依赖一致性」，
并通过 monkeypatch 确定性地覆盖降级分支。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


def test_healthz_returns_200_with_three_deps(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in {"ok", "degraded"}
    assert set(body["deps"]) >= {"pg", "redis", "minio"}
    for name in ("pg", "redis", "minio"):
        assert "ok" in body["deps"][name]
        assert isinstance(body["deps"][name]["ok"], bool)


def test_healthz_status_consistent_with_dependency_reachability(client: TestClient) -> None:
    # 依赖全部可达时 status=ok，否则 degraded；一致性不受环境（是否起 pg/redis/minio）影响
    body = client.get("/healthz").json()
    all_ok = all(item["ok"] for item in body["deps"].values())
    assert body["status"] == ("ok" if all_ok else "degraded")


def test_healthz_degraded_when_dependencies_down(monkeypatch) -> None:
    # 确定性覆盖降级分支：依赖探测全部不可达时必须 200 + degraded（绝不 5xx）
    def _fake_probe(_settings):
        return {name: {"ok": False, "detail": "down"} for name in ("pg", "redis", "minio")}

    monkeypatch.setattr("app.main.probe_dependencies", _fake_probe)
    app = create_app()
    with TestClient(app) as test_client:
        resp = test_client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "degraded"

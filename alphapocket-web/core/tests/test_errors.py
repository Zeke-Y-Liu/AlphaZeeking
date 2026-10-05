"""统一错误体自测（T1.1，对齐 §6.3）。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.errors import AppError, ErrorCode
from app.main import create_app


def test_404_returns_unified_error_body(client: TestClient) -> None:
    resp = client.get("/nope")
    assert resp.status_code == 404
    body = resp.json()
    assert set(body) == {"code", "message", "detail"}
    assert body["code"] == ErrorCode.NOT_FOUND.value
    assert isinstance(body["message"], str)


def _int_echo(x: int) -> dict[str, int]:
    return {"x": x}


def test_422_returns_unified_error_body() -> None:
    app = create_app()
    app.get("/_echo")(_int_echo)
    with TestClient(app) as test_client:
        resp = test_client.get("/_echo", params={"x": "not-an-int"})
    assert resp.status_code == 422
    body = resp.json()
    assert set(body) == {"code", "message", "detail"}
    assert body["code"] == ErrorCode.VALIDATION_ERROR.value
    assert isinstance(body["detail"], list)


def _raise_weight_error() -> dict[str, float]:
    raise AppError(
        ErrorCode.WEIGHT_NOT_NORMALIZED,
        "权重和不在 [99.5, 100.5]",
        detail={"sum": 88.0, "suggestion": "等权归一"},
    )


def test_app_error_maps_code_and_status() -> None:
    app = create_app()
    app.get("/_boom")(_raise_weight_error)
    with TestClient(app) as test_client:
        resp = test_client.get("/_boom")
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == ErrorCode.WEIGHT_NOT_NORMALIZED.value
    assert body["message"] == "权重和不在 [99.5, 100.5]"
    assert body["detail"] == {"sum": 88.0, "suggestion": "等权归一"}


def test_verify_gate_failed_maps_to_409() -> None:
    app = create_app()

    def _gate() -> None:
        raise AppError(ErrorCode.VERIFY_GATE_FAILED, "数据验证未全绿")

    app.get("/_gate")(_gate)
    with TestClient(app) as test_client:
        resp = test_client.get("/_gate")
    assert resp.status_code == 409
    assert resp.json()["code"] == ErrorCode.VERIFY_GATE_FAILED.value

"""结构化日志与 trace_id 中间件自测（T1.1）。"""
from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from app.core.logging import configure_logging

HEADER = "X-Request-Id"


def test_response_contains_request_id(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.headers.get(HEADER)
    assert len(resp.headers[HEADER]) > 0


def test_custom_request_id_is_passed_through(client: TestClient) -> None:
    resp = client.get("/healthz", headers={HEADER: "trace-abc-123"})
    assert resp.headers[HEADER] == "trace-abc-123"


def test_configure_logging_is_idempotent() -> None:
    root = logging.getLogger()
    configure_logging("INFO")
    count_after_first = len(root.handlers)
    configure_logging("DEBUG")
    configure_logging("WARNING")
    assert len(root.handlers) == count_after_first

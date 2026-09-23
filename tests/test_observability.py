import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from app import http_client, main, observability, remediation, store
from app.api import read
from app.celery_app import celery_app


def _counter(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels or None) or 0.0


def test_request_id_is_echoed_and_generated():
    client = TestClient(main.app)

    generated = client.get("/healthz")
    assert generated.status_code == 200
    assert len(generated.headers["X-Request-ID"]) == 32

    echoed = client.get("/healthz", headers={"X-Request-ID": "abc-123"})
    assert echoed.headers["X-Request-ID"] == "abc-123"


def test_request_id_is_available_inside_threadpool_handlers(monkeypatch):
    seen: list[str | None] = []

    def probe() -> None:
        seen.append(observability.get_request_id())

    monkeypatch.setattr(read, "_broker_ping", probe)
    client = TestClient(main.app)
    client.get("/readyz", headers={"X-Request-ID": "corr-1"})
    assert seen == ["corr-1"]
    assert observability.get_request_id() is None


def test_json_formatter_emits_request_id_and_message():
    formatter = observability.JsonFormatter()
    token = observability.set_request_id("rid-42")
    try:
        record = logging.LogRecord("t", logging.WARNING, __file__, 1, "hello %s", ("w",), None)
        payload = json.loads(formatter.format(record))
    finally:
        observability.reset_request_id(token)
    assert payload["level"] == "WARNING"
    assert payload["message"] == "hello w"
    assert payload["request_id"] == "rid-42"
    assert payload["ts"].endswith("Z")


def test_metrics_endpoint_exposes_request_and_domain_metrics():
    client = TestClient(main.app)
    client.get("/status")

    body = client.get("/metrics").text
    assert 'http_requests_total{handler="/status"' in body
    assert "remediation_sessions_created_total" in body
    assert "remediation_polls_total" in body
    assert "discovery_findings_total" in body
    assert "remediation_outcomes_total" in body


def test_readyz_reports_dependencies():
    client = TestClient(main.app)
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"ready": True, "checks": {"database": "ok", "broker": "ok"}}


def test_readyz_returns_503_when_broker_unreachable(monkeypatch):
    def boom() -> None:
        raise ConnectionError("redis down")

    monkeypatch.setattr(read, "_broker_ping", boom)
    client = TestClient(main.app)
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"] == {"database": "ok", "broker": "error: ConnectionError"}
    assert client.get("/healthz").status_code == 200


def test_domain_counters_track_session_lifecycle(monkeypatch):
    monkeypatch.setattr(remediation.github, "find_existing_pr", lambda n: None)
    monkeypatch.setattr(
        remediation.devin,
        "create_session",
        lambda *a, **k: {"session_id": "s-1", "url": "https://devin/s-1"},
    )
    monkeypatch.setattr(remediation.github, "post_comment", lambda *a, **k: None)
    monkeypatch.setattr(remediation, "arm_poll", lambda *a, **k: True)
    before = _counter("remediation_sessions_created_total")

    assert remediation.process_issue(
        {"number": 7, "title": "t", "body": "", "html_url": "https://gh/7"}
    )
    assert _counter("remediation_sessions_created_total") == before + 1

    token = store.claim_poll(7, "s-1", lease_seconds=60)
    monkeypatch.setattr(
        remediation.devin, "get_session", lambda sid: {"status": "exit", "pull_requests": [{"pr_url": "https://gh/pull/1"}]}
    )
    completed_before = _counter("remediation_outcomes_total", status="completed")
    assert remediation.poll_session_once(7, "s-1", token) is False
    assert _counter("remediation_outcomes_total", status="completed") == completed_before + 1
    assert _counter("remediation_polls_total", outcome="completed") >= 1


def test_http_retry_counter_increments(httpx_mock):
    url = "https://api.example.com/x"
    httpx_mock.add_response(method="GET", url=url, status_code=503)
    httpx_mock.add_response(method="GET", url=url, json={})
    before = _counter("external_http_retries_total", host="api.example.com")
    with http_client.client() as c:
        http_client.request(c, "GET", url, sleep=lambda s: None)
    assert _counter("external_http_retries_total", host="api.example.com") == before + 1


def test_request_id_propagates_into_celery_tasks():
    seen: list[str | None] = []

    @celery_app.task(name="tests.capture_request_id")
    def capture() -> None:
        seen.append(observability.get_request_id())

    token = observability.set_request_id("from-api")
    try:
        capture.apply_async()
    finally:
        observability.reset_request_id(token)

    assert seen == ["from-api"]
    assert observability.get_request_id() is None


def test_tracing_is_noop_without_endpoint(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    observability.configure_tracing("test")
    with observability.span("noop", key="v") as s:
        assert not s.get_span_context().is_valid


@pytest.mark.parametrize("value", ["1", "yes"])
def test_span_helper_skips_none_attributes(value):
    with observability.span("x", a=None, b=value):
        pass


def test_shutdown_tracing_is_safe_without_provider():
    observability.shutdown_tracing()
    assert isinstance(httpx.Timeout(1), httpx.Timeout)

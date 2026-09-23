"""
Broker/DB-backed integration tests. Skipped unless INTEGRATION_REDIS_URL is set
(CI runs them against Postgres + Redis service containers with
TEST_DATABASE_URL and INTEGRATION_REDIS_URL exported).
"""

import os
import threading

import pytest
from celery.contrib.testing.worker import start_worker
from fastapi.testclient import TestClient
from kombu import pools

from app import main, observability, store
from app.celery_app import celery_app

REDIS_URL = os.getenv("INTEGRATION_REDIS_URL")

pytestmark = pytest.mark.skipif(not REDIS_URL, reason="INTEGRATION_REDIS_URL not set")


@pytest.fixture
def real_broker():
    """Point the Celery app at the real Redis broker for one test."""
    previous = {
        key: celery_app.conf[key] for key in ("broker_url", "result_backend", "task_always_eager")
    }
    celery_app.conf.broker_url = REDIS_URL
    celery_app.conf.result_backend = REDIS_URL
    celery_app.conf.task_always_eager = False
    _reset_pool(celery_app)
    try:
        yield celery_app
    finally:
        _reset_pool(celery_app)
        for key, value in previous.items():
            celery_app.conf[key] = value


def _reset_pool(app) -> None:
    """Drop cached broker connections so the next call honours the new URL.

    kombu keeps a process-global pool registry keyed by connection URL, so
    closing the app's pool alone would hand a closed pool to the next test
    when the integration URL equals the default broker URL (as in CI)."""
    pools.reset()
    app._after_fork()


def test_readyz_reports_real_database_and_broker(real_broker):
    client = TestClient(main.app)
    response = client.get("/readyz")
    assert response.status_code == 200, response.text
    assert response.json() == {"ready": True, "checks": {"database": "ok", "broker": "ok"}}


def test_readyz_fails_when_broker_is_down(real_broker):
    real_broker.conf.broker_url = "redis://127.0.0.1:1/0"
    _reset_pool(real_broker)
    client = TestClient(main.app)
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["database"] == "ok"
    assert response.json()["checks"]["broker"].startswith("error:")


def test_request_id_travels_through_the_real_broker(real_broker):
    seen: list[str | None] = []
    done = threading.Event()

    @real_broker.task(name="tests.integration.capture_request_id")
    def capture(issue_number: int) -> None:
        store.upsert(issue_number, title="via broker", issue_url="u", status="completed")
        seen.append(observability.get_request_id())
        done.set()

    with start_worker(real_broker, queues=["ingest"], perform_ping_check=False, loglevel="warning"):
        token = observability.set_request_id("corr-broker")
        try:
            capture.apply_async(args=[99], queue="ingest")
        finally:
            observability.reset_request_id(token)
        assert done.wait(timeout=20), "task was not consumed from the broker"

    assert seen == ["corr-broker"]
    assert store.get_status(99) == "completed"

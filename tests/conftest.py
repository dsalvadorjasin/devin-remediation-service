import os
import sys
from pathlib import Path

os.environ["GITHUB_TOKEN"] = "test-token"
os.environ["GITHUB_REPO"] = "test-org/test-repo"
os.environ["DEVIN_API_KEY"] = "test-devin-key"
os.environ["DEVIN_ORG_ID"] = "test-org-id"
os.environ["SCAN_INTERVAL_MINUTES"] = "5"
# Tests run against an in-memory SQLite database unless TEST_DATABASE_URL points
# at an ephemeral Postgres instance.
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "sqlite://")
# Celery tasks run synchronously in-process; no broker needed.
os.environ["CELERY_TASK_ALWAYS_EAGER"] = "1"
os.environ["ORCHESTRATOR"] = "celery"
# Retries still happen in tests, but without waiting between attempts.
os.environ["HTTP_BACKOFF_BASE_SECONDS"] = "0"
os.environ["HTTP_BACKOFF_CAP_SECONDS"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app import db, store
from app.orchestrator.celery_orchestrator import CeleryOrchestrator

db.init_db()


@pytest.fixture(autouse=True)
def clear_store():
    store.clear()
    yield
    store.clear()


@pytest.fixture(autouse=True)
def scheduled_polls(monkeypatch):
    """Record schedule_poll calls instead of running them. In eager mode a
    real re-queue would execute the poll (and hit the network) immediately."""
    calls: list[tuple[int, str, int]] = []

    def fake_schedule_poll(self, issue_number, session_id, poll_token, delay_seconds=60):
        calls.append((issue_number, session_id, delay_seconds))

    monkeypatch.setattr(CeleryOrchestrator, "schedule_poll", fake_schedule_poll)
    return calls

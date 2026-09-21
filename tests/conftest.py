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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app import db, store

db.init_db()


@pytest.fixture(autouse=True)
def clear_store():
    store.clear()
    yield
    store.clear()

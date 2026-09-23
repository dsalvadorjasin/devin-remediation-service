"""Regression tests for the deferred Phase 1-5 bug: duplicate Devin sessions
from overlapping remediation tasks, and stranded rows after a worker dies
between the `running` write and saving the session id."""

from datetime import timedelta

from sqlalchemy import update

from app import devin, github, remediation, store
from app.db import SessionLocal, Task, utcnow

ISSUE = {
    "number": 11,
    "title": "Fix bug",
    "body": "Details",
    "html_url": "https://example.com/issues/11",
}


def _set_lease(issue_number: int, seconds_from_now: int | None) -> None:
    value = None if seconds_from_now is None else utcnow() + timedelta(seconds=seconds_from_now)
    with SessionLocal() as session:
        session.execute(
            update(Task)
            .where(Task.repository == "test-org/test-repo", Task.issue_number == issue_number)
            .values(poll_lease_until=value)
        )
        session.commit()


def test_concurrent_process_issue_creates_exactly_one_session(monkeypatch, scheduled_polls):
    """Two tasks for the same issue both pass the read-only guards (neither
    sees a row yet). The first `find_existing_pr` call re-enters
    `process_issue` to model the second task finishing in between; the outer
    task must then lose the atomic claim instead of creating a second session."""
    sessions: list[int] = []
    monkeypatch.setattr(github, "post_comment", lambda *a, **k: None)

    def fake_create_session(number, title, body):
        sessions.append(number)
        return {"session_id": f"sess-{len(sessions)}", "url": "https://example.com/s"}

    monkeypatch.setattr(devin, "create_session", fake_create_session)

    reentered = {"done": False}

    def find_existing_pr(number):
        if not reentered["done"]:
            reentered["done"] = True
            assert remediation.process_issue(ISSUE) is True
        return None

    monkeypatch.setattr(github, "find_existing_pr", find_existing_pr)

    assert remediation.process_issue(ISSUE) is False

    assert sessions == [11]
    entry = store.get(11)
    assert entry["status"] == "running"
    assert entry["session_id"] == "sess-1"
    assert scheduled_polls == [(11, "sess-1", 60)]


def test_claim_remediation_is_granted_once_per_issue():
    assert store.claim_remediation(11, "t", "u", lease_seconds=600) is True
    assert store.claim_remediation(11, "t", "u", lease_seconds=600) is False
    entry = store.get(11)
    assert entry["status"] == "running"
    assert entry["session_id"] is None
    assert entry["poll_lease_until"] is not None


def test_claim_remediation_never_reclaims_row_with_session():
    store.upsert(11, title="t", issue_url="u", status="running", session_id="sess-1")
    _set_lease(11, None)
    assert store.claim_remediation(11, "t", "u", lease_seconds=600) is False
    assert store.get(11)["session_id"] == "sess-1"


def test_claim_remediation_reclaims_terminal_rows_and_clears_old_association():
    store.upsert(11, title="t", issue_url="u", status="completed", session_id="old", pr_url="p")
    assert store.claim_remediation(11, "t2", "u", lease_seconds=600) is True
    entry = store.get(11)
    assert entry["status"] == "running"
    assert entry["title"] == "t2"
    assert entry["session_id"] is None
    assert entry["pr_url"] is None


def test_stranded_claim_is_reclaimed_only_after_lease_expiry(monkeypatch, scheduled_polls):
    """Worker died after the claim but before `session_id` was saved. While
    the creation lease is live a redelivered task must skip; once it expires
    the issue is picked up again and a session is created."""
    monkeypatch.setattr(github, "find_existing_pr", lambda number: None)
    monkeypatch.setattr(github, "post_comment", lambda *a, **k: None)
    created: list[int] = []
    monkeypatch.setattr(
        devin,
        "create_session",
        lambda n, t, b: created.append(n) or {"session_id": "sess-new", "url": "https://x"},
    )

    assert store.claim_remediation(11, "t", "u", lease_seconds=600) is True

    assert remediation.process_issue(ISSUE) is False
    assert created == []

    _set_lease(11, -1)
    assert remediation.process_issue(ISSUE) is True
    assert created == [11]
    entry = store.get(11)
    assert entry["session_id"] == "sess-new"
    assert entry["poll_token"] is not None
    assert scheduled_polls == [(11, "sess-new", 60)]


def test_stranded_claim_is_not_rearmed_as_a_poll(monkeypatch, scheduled_polls):
    """A claimed row without a session is not a lost poll chain."""
    monkeypatch.setattr(github, "get_labeled_issues", lambda: [])
    assert store.claim_remediation(11, "t", "u", lease_seconds=600) is True
    _set_lease(11, -1)
    assert remediation.scan_and_process() == {"scanned": 0, "polls_rearmed": 0}
    assert scheduled_polls == []


def test_failed_session_creation_releases_claim(monkeypatch, scheduled_polls):
    monkeypatch.setattr(github, "find_existing_pr", lambda number: None)

    def boom(*a, **k):
        raise RuntimeError("devin down")

    monkeypatch.setattr(devin, "create_session", boom)
    assert remediation.process_issue(ISSUE) is False
    entry = store.get(11)
    assert entry["status"] == "failed"
    assert entry["poll_lease_until"] is None
    assert remediation.process_issue(ISSUE, force_retry=True) is False
    assert store.get(11)["status"] == "failed"


def test_session_response_without_id_is_a_failure(monkeypatch, scheduled_polls):
    """A 'successful' Devin response with no session id must not leave a
    session-less running row that a later scan would reclaim into a second
    session; it is treated as a creation failure."""
    monkeypatch.setattr(github, "find_existing_pr", lambda number: None)
    monkeypatch.setattr(github, "post_comment", lambda *a, **k: None)
    calls: list[int] = []

    def no_id(number, title, body):
        calls.append(number)
        return {"url": "https://app.devin.ai/sessions/s-42"}

    monkeypatch.setattr(devin, "create_session", no_id)
    assert remediation.process_issue(ISSUE) is False
    entry = store.get(11)
    assert entry["status"] == "failed"
    assert entry["session_id"] is None
    assert entry["poll_lease_until"] is None
    assert scheduled_polls == []
    # Reconciliation scans do not retry a failed row without force_retry.
    assert remediation.process_issue(ISSUE) is False
    assert calls == [11]

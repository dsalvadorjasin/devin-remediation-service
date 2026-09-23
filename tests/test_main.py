from fastapi.testclient import TestClient

from app import devin, github, main, remediation, store, tasks
from app.orchestrator.celery_orchestrator import CeleryOrchestrator


def test_dashboard_returns_html():
    client = TestClient(main.app)

    response = client.get("/")

    assert response.status_code == 200
    assert "<!DOCTYPE html>" in response.text
    assert "Devin" in response.text


def test_status_returns_store_entries_as_json():
    store.upsert(12, title="Issue 12", issue_url="https://example.com/issues/12")
    store.upsert(
        3, title="Issue 3", issue_url="https://example.com/issues/3",
        session_id="sess-3",
    )
    assert store.claim_poll(3, "sess-3", lease_seconds=180) is not None
    client = TestClient(main.app)

    response = client.get("/status")

    assert response.status_code == 200
    assert [entry["issue_number"] for entry in response.json()] == [3, 12]
    for entry in response.json():
        assert set(entry) == {
            "issue_number", "title", "issue_url", "session_id", "session_url",
            "status", "pr_url", "created_at", "updated_at",
        }
        assert "poll_token" not in entry
        assert "poll_lease_until" not in entry


def test_status_for_issue_excludes_poll_lease():
    store.upsert(
        42, title="Issue 42", issue_url="https://example.com/issues/42",
        session_id="sess-42",
    )
    assert store.claim_poll(42, "sess-42", lease_seconds=180) is not None
    client = TestClient(main.app)

    response = client.get("/status/42")

    assert response.status_code == 200
    assert set(response.json()) == {
        "issue_number", "title", "issue_url", "session_id", "session_url",
        "status", "pr_url", "created_at", "updated_at",
    }
    assert "poll_token" not in response.json()
    assert "poll_lease_until" not in response.json()
    assert store.get(42)["poll_token"] is not None
    assert store.get(42)["poll_lease_until"] is not None


def test_manual_scan_enqueues_scan_through_orchestrator(monkeypatch):
    calls = []

    def fake_scan_and_process(force_retry=False):
        calls.append(force_retry)
        return {"scanned": 2}

    monkeypatch.setattr(remediation, "scan_and_process", fake_scan_and_process)
    client = TestClient(main.app)

    monkeypatch.setenv("INGEST_TOKEN", "t0k")
    assert client.post("/scan?force_retry=true").status_code == 401
    response = client.post("/scan?force_retry=true", headers={"X-Ingest-Token": "t0k"})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "enqueued": True, "force_retry": True}
    assert calls == [True]


def test_scan_and_process_enqueues_each_issue(monkeypatch):
    issues = [
        {"number": 1, "title": "A", "body": "", "html_url": "https://example.com/issues/1"},
        {"number": 2, "title": "B", "body": "", "html_url": "https://example.com/issues/2"},
    ]
    monkeypatch.setattr(github, "get_labeled_issues", lambda: issues)
    processed = []
    monkeypatch.setattr(
        remediation, "process_issue", lambda issue, force_retry=False: processed.append(issue["number"])
    )

    result = remediation.scan_and_process()

    assert result == {"scanned": 2, "polls_rearmed": 0}
    assert processed == [1, 2]


def test_scan_rearms_poll_for_running_session_without_lease(monkeypatch, scheduled_polls):
    """A session persisted as running with no live poll (e.g. created before a
    worker restart) gets a fresh poll chain from the reconciliation scan."""
    store.upsert(42, title="Fix bug", issue_url="u", session_id="sess-42", status="running")
    monkeypatch.setattr(github, "get_labeled_issues", lambda: [])

    result = remediation.scan_and_process()

    assert result == {"scanned": 0, "polls_rearmed": 1}
    assert scheduled_polls == [(42, "sess-42", 0)]
    entry = store.get(42)
    assert entry["poll_token"] is not None
    assert entry["poll_lease_until"] is not None


def test_scan_does_not_rearm_poll_with_live_lease(monkeypatch, scheduled_polls):
    store.upsert(43, title="Fix bug", issue_url="u", session_id="sess-43", status="running")
    assert remediation.arm_poll(43, "sess-43") is True
    scheduled_polls.clear()
    monkeypatch.setattr(github, "get_labeled_issues", lambda: [])

    result = remediation.scan_and_process()
    result_again = remediation.scan_and_process()

    assert result == result_again == {"scanned": 0, "polls_rearmed": 0}
    assert scheduled_polls == []


def test_scan_ignores_running_session_without_session_id(monkeypatch, scheduled_polls):
    store.upsert(44, title="Fix bug", issue_url="u", status="running")
    monkeypatch.setattr(github, "get_labeled_issues", lambda: [])

    assert remediation.scan_and_process() == {"scanned": 0, "polls_rearmed": 0}
    assert scheduled_polls == []


def test_scan_and_process_reports_github_error(monkeypatch):
    def boom():
        raise RuntimeError("github down")

    monkeypatch.setattr(github, "get_labeled_issues", boom)

    assert remediation.scan_and_process() == {"error": "github down"}


def test_process_issue_creates_session_and_comments(monkeypatch, scheduled_polls):
    issue = {
        "number": 1,
        "title": "Fix bug",
        "body": "Details",
        "html_url": "https://example.com/issues/1",
    }
    session_calls = []
    comment_calls = []

    monkeypatch.setattr(github, "find_existing_pr", lambda number: None)

    def fake_create_session(number, title, body):
        session_calls.append((number, title, body))
        return {"session_id": "sess-1", "url": "https://example.com/sessions/1"}

    def fake_post_comment(number, body):
        comment_calls.append((number, body))

    monkeypatch.setattr(devin, "create_session", fake_create_session)
    monkeypatch.setattr(github, "post_comment", fake_post_comment)

    main.process_issue(issue)

    assert scheduled_polls == [(1, "sess-1", 60)]
    assert session_calls == [(1, "Fix bug", "Details")]
    assert comment_calls == [
        (
            1,
            "🤖 A Devin session has been started to address this issue.\n\nSession: https://example.com/sessions/1",
        )
    ]
    entry = store.get(1)
    assert entry["status"] == "running"
    assert entry["session_id"] == "sess-1"
    assert entry["session_url"] == "https://example.com/sessions/1"


def test_process_issue_marks_existing_pr_completed_and_skips_session(monkeypatch):
    issue = {
        "number": 2,
        "title": "Fix bug",
        "body": "Details",
        "html_url": "https://example.com/issues/2",
    }
    monkeypatch.setattr(
        github, "find_existing_pr", lambda number: "https://example.com/pr/2"
    )

    create_session_calls = []
    monkeypatch.setattr(
        devin,
        "create_session",
        lambda *args, **kwargs: create_session_calls.append((args, kwargs)),
    )

    main.process_issue(issue)

    assert create_session_calls == []
    entry = store.get(2)
    assert entry["status"] == "completed"
    assert entry["pr_url"] == "https://example.com/pr/2"


def test_process_issue_skips_when_running(monkeypatch):
    issue = {
        "number": 3,
        "title": "Fix bug",
        "body": "Details",
        "html_url": "https://example.com/issues/3",
    }
    store.upsert(
        3,
        title="Fix bug",
        issue_url="https://example.com/issues/3",
        status="running",
        session_id="sess-3",
    )
    monkeypatch.setattr(github, "find_existing_pr", lambda number: None)
    create_session_calls = []
    monkeypatch.setattr(
        devin,
        "create_session",
        lambda *args, **kwargs: create_session_calls.append((args, kwargs)),
    )

    main.process_issue(issue)

    assert create_session_calls == []
    assert store.get_status(3) == "running"


def test_process_issue_skips_failed_without_force_retry(monkeypatch):
    issue = {
        "number": 4,
        "title": "Fix bug",
        "body": "Details",
        "html_url": "https://example.com/issues/4",
    }
    store.upsert(4, title="Fix bug", issue_url="https://example.com/issues/4", status="failed")
    monkeypatch.setattr(github, "find_existing_pr", lambda number: None)
    create_session_calls = []
    monkeypatch.setattr(
        devin,
        "create_session",
        lambda *args, **kwargs: create_session_calls.append((args, kwargs)),
    )

    main.process_issue(issue, force_retry=False)

    assert create_session_calls == []


def test_process_issue_retries_failed_when_force_retry_true(monkeypatch):
    issue = {
        "number": 5,
        "title": "Fix bug",
        "body": "Details",
        "html_url": "https://example.com/issues/5",
    }
    store.upsert(5, title="Fix bug", issue_url="https://example.com/issues/5", status="failed")
    monkeypatch.setattr(github, "find_existing_pr", lambda number: None)
    monkeypatch.setattr(devin, "create_session", lambda *args: {"session_id": "sess-5", "url": "https://example.com/sessions/5"})
    monkeypatch.setattr(github, "post_comment", lambda *args, **kwargs: None)

    main.process_issue(issue, force_retry=True)

    assert store.get(5)["status"] == "running"
    assert store.get(5)["session_id"] == "sess-5"
    assert store.get(5)["session_url"] == "https://example.com/sessions/5"


def _own(issue_number, session_id):
    """Give the test's poll chain the lease, as arm_poll does in production."""
    return store.claim_poll(issue_number, session_id, lease_seconds=180)


def test_poll_session_marks_completed_from_devin_exit(monkeypatch, scheduled_polls):
    store.upsert(
        10,
        title="Fix bug",
        issue_url="https://example.com/issues/10",
        session_id="sess-10",
        session_url="https://example.com/sessions/10",
        status="running",
    )
    monkeypatch.setattr(
        devin,
        "get_session",
        lambda session_id: {
            "status": "exit",
            "status_detail": None,
            "pull_requests": [{"pr_url": "https://example.com/pr/10"}],
        },
    )
    monkeypatch.setattr(github, "find_existing_pr", lambda issue_number: None)

    still_running = tasks.poll_session_task.apply(
        kwargs={"issue_number": 10, "session_id": "sess-10", "poll_token": _own(10, "sess-10")}
    ).get()

    assert still_running is False
    entry = store.get(10)
    assert entry["status"] == "completed"
    assert entry["pr_url"] == "https://example.com/pr/10"
    assert scheduled_polls == []


def test_poll_session_marks_completed_from_github_fallback(monkeypatch, scheduled_polls):
    store.upsert(
        11,
        title="Fix bug",
        issue_url="https://example.com/issues/11",
        session_id="sess-11",
        session_url="https://example.com/sessions/11",
        status="running",
    )
    monkeypatch.setattr(
        devin,
        "get_session",
        lambda session_id: {"status": "running", "status_detail": None, "pull_requests": []},
    )
    monkeypatch.setattr(
        github, "find_existing_pr", lambda issue_number: "https://example.com/pr/11"
    )

    still_running = tasks.poll_session_task.apply(
        kwargs={"issue_number": 11, "session_id": "sess-11", "poll_token": _own(11, "sess-11")}
    ).get()

    assert still_running is False
    entry = store.get(11)
    assert entry["status"] == "completed"
    assert entry["pr_url"] == "https://example.com/pr/11"


def test_poll_session_requeues_itself_while_running(monkeypatch, scheduled_polls):
    store.upsert(
        12,
        title="Fix bug",
        issue_url="https://example.com/issues/12",
        session_id="sess-12",
        status="running",
    )
    monkeypatch.setattr(
        devin,
        "get_session",
        lambda session_id: {"status": "running", "status_detail": None, "pull_requests": []},
    )
    monkeypatch.setattr(github, "find_existing_pr", lambda issue_number: None)

    still_running = tasks.poll_session_task.apply(
        kwargs={"issue_number": 12, "session_id": "sess-12", "poll_token": _own(12, "sess-12")}
    ).get()

    assert still_running is True
    assert store.get_status(12) == "running"
    assert scheduled_polls == [(12, "sess-12", 60)]


def test_poll_session_with_stale_token_stops_without_polling(monkeypatch, scheduled_polls):
    store.upsert(15, title="Fix bug", issue_url="u", session_id="sess-15", status="running")
    assert remediation.arm_poll(15, "sess-15") is True
    scheduled_polls.clear()
    called = []
    monkeypatch.setattr(devin, "get_session", lambda session_id: called.append(session_id))

    still_running = tasks.poll_session_task.apply(
        kwargs={"issue_number": 15, "session_id": "sess-15", "poll_token": "stale"}
    ).get()

    assert still_running is False
    assert called == []
    assert scheduled_polls == []


def test_poll_session_with_current_token_renews_lease(monkeypatch, scheduled_polls):
    store.upsert(16, title="Fix bug", issue_url="u", session_id="sess-16", status="running")
    assert remediation.arm_poll(16, "sess-16") is True
    scheduled_polls.clear()
    token = store.get(16)["poll_token"]
    lease_before = store.get(16)["poll_lease_until"]
    monkeypatch.setattr(
        devin,
        "get_session",
        lambda session_id: {"status": "running", "status_detail": None, "pull_requests": []},
    )
    monkeypatch.setattr(github, "find_existing_pr", lambda issue_number: None)

    still_running = tasks.poll_session_task.apply(
        kwargs={"issue_number": 16, "session_id": "sess-16", "poll_token": token}
    ).get()

    assert still_running is True
    assert scheduled_polls == [(16, "sess-16", 60)]
    assert store.get(16)["poll_token"] == token
    assert store.get(16)["poll_lease_until"] >= lease_before


def test_poll_session_releases_lease_on_completion(monkeypatch, scheduled_polls):
    store.upsert(17, title="Fix bug", issue_url="u", session_id="sess-17", status="running")
    assert remediation.arm_poll(17, "sess-17") is True
    token = store.get(17)["poll_token"]
    monkeypatch.setattr(
        devin,
        "get_session",
        lambda session_id: {"status": "finished", "status_detail": None, "pull_requests": []},
    )
    monkeypatch.setattr(github, "find_existing_pr", lambda issue_number: "https://example.com/pr/17")

    tasks.poll_session_task.apply(
        kwargs={"issue_number": 17, "session_id": "sess-17", "poll_token": token}
    ).get()

    entry = store.get(17)
    assert entry["status"] == "completed"
    assert entry["poll_token"] is None
    assert entry["poll_lease_until"] is None


def test_poll_session_marks_failed_on_error(monkeypatch, scheduled_polls):
    store.upsert(13, title="Fix bug", issue_url="https://example.com/issues/13", session_id="sess-13", status="running")
    monkeypatch.setattr(
        devin, "get_session", lambda session_id: {"status": "error", "pull_requests": []}
    )
    monkeypatch.setattr(github, "find_existing_pr", lambda issue_number: None)

    assert tasks.poll_session_task.apply(
        kwargs={"issue_number": 13, "session_id": "sess-13", "poll_token": _own(13, "sess-13")}
    ).get() is False
    assert store.get_status(13) == "failed"
    assert scheduled_polls == []


def test_poll_session_stops_when_superseded(monkeypatch, scheduled_polls):
    store.upsert(14, title="Fix bug", issue_url="https://example.com/issues/14", session_id="sess-new", status="running")
    called = []
    monkeypatch.setattr(devin, "get_session", lambda session_id: called.append(session_id))

    assert tasks.poll_session_task.apply(
        kwargs={"issue_number": 14, "session_id": "sess-old", "poll_token": _own(14, "sess-new")}
    ).get() is False
    assert called == []


def test_terminal_poll_release_leaves_newer_owner(monkeypatch, scheduled_polls):
    """A chain finishing after its lease was re-claimed must not clear the new owner."""
    store.upsert(18, title="Fix bug", issue_url="u", session_id="sess-18", status="running")
    old = _own(18, "sess-18")
    new = store.claim_poll(18, "sess-18", lease_seconds=180, only_if_lost=False)
    assert new is not None and new != old
    monkeypatch.setattr(devin, "get_session", lambda session_id: {"status": "exit", "pull_requests": []})
    monkeypatch.setattr(github, "find_existing_pr", lambda issue_number: None)

    assert remediation.poll_session_once(18, "sess-18", old) is False
    assert store.get(18)["poll_token"] == new
    assert remediation.poll_session_once(18, "sess-18", new) is False
    assert store.get(18)["status"] == "completed"
    assert store.get(18)["poll_token"] is None


def test_arm_poll_releases_lease_when_scheduling_fails(monkeypatch):
    """A broker failure must not leave a live lease with no chain behind it."""
    store.upsert(19, title="Fix bug", issue_url="u", session_id="sess-19", status="running")

    def boom(self, *args, **kwargs):
        raise RuntimeError("broker down")

    monkeypatch.setattr(CeleryOrchestrator, "schedule_poll", boom)

    assert remediation.arm_poll(19, "sess-19") is False
    entry = store.get(19)
    assert entry["poll_token"] is None
    assert entry["poll_lease_until"] is None
    assert [e["issue_number"] for e in store.get_unpolled_running()] == [19]


def test_release_poll_with_token_leaves_newer_owner(monkeypatch, scheduled_polls):
    store.upsert(20, title="Fix bug", issue_url="u", session_id="sess-20", status="running")
    old = store.claim_poll(20, "sess-20", lease_seconds=180)
    new = store.claim_poll(20, "sess-20", lease_seconds=180)

    store.release_poll(20, poll_token=old)

    assert store.get(20)["poll_token"] == new

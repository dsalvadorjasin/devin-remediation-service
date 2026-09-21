from fastapi.testclient import TestClient

from app import devin, github, main, remediation, store, tasks


def test_dashboard_returns_html():
    client = TestClient(main.app)

    response = client.get("/")

    assert response.status_code == 200
    assert "<!DOCTYPE html>" in response.text
    assert "Devin" in response.text


def test_status_returns_store_entries_as_json():
    store.upsert(12, title="Issue 12", issue_url="https://example.com/issues/12")
    store.upsert(3, title="Issue 3", issue_url="https://example.com/issues/3")
    client = TestClient(main.app)

    response = client.get("/status")

    assert response.status_code == 200
    assert [entry["issue_number"] for entry in response.json()] == [3, 12]


def test_manual_scan_enqueues_scan_through_orchestrator(monkeypatch):
    calls = []

    def fake_scan_and_process(force_retry=False):
        calls.append(force_retry)
        return {"scanned": 2}

    monkeypatch.setattr(remediation, "scan_and_process", fake_scan_and_process)
    client = TestClient(main.app)

    response = client.post("/scan?force_retry=true")

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

    assert result == {"scanned": 2}
    assert processed == [1, 2]


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
    store.upsert(3, title="Fix bug", issue_url="https://example.com/issues/3", status="running")
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

    still_running = tasks.poll_session_task.apply(kwargs={"issue_number": 10, "session_id": "sess-10"}).get()

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

    still_running = tasks.poll_session_task.apply(kwargs={"issue_number": 11, "session_id": "sess-11"}).get()

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

    still_running = tasks.poll_session_task.apply(kwargs={"issue_number": 12, "session_id": "sess-12"}).get()

    assert still_running is True
    assert store.get_status(12) == "running"
    assert scheduled_polls == [(12, "sess-12", 60)]


def test_poll_session_marks_failed_on_error(monkeypatch, scheduled_polls):
    store.upsert(13, title="Fix bug", issue_url="https://example.com/issues/13", session_id="sess-13", status="running")
    monkeypatch.setattr(
        devin, "get_session", lambda session_id: {"status": "error", "pull_requests": []}
    )
    monkeypatch.setattr(github, "find_existing_pr", lambda issue_number: None)

    assert tasks.poll_session_task.apply(kwargs={"issue_number": 13, "session_id": "sess-13"}).get() is False
    assert store.get_status(13) == "failed"
    assert scheduled_polls == []


def test_poll_session_stops_when_superseded(monkeypatch, scheduled_polls):
    store.upsert(14, title="Fix bug", issue_url="https://example.com/issues/14", session_id="sess-new", status="running")
    called = []
    monkeypatch.setattr(devin, "get_session", lambda session_id: called.append(session_id))

    assert tasks.poll_session_task.apply(kwargs={"issue_number": 14, "session_id": "sess-old"}).get() is False
    assert called == []

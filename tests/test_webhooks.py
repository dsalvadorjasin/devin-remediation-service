import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app import github, main, remediation, store, webhooks
from app.orchestrator.celery_orchestrator import CeleryOrchestrator

SECRET = "test-webhook-secret"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post(client, event: str, payload: dict, secret: str = SECRET, signature: str | None = None):
    body = json.dumps(payload).encode()
    headers = {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": "d-1",
        "X-Hub-Signature-256": signature if signature is not None else _sign(body, secret),
        "Content-Type": "application/json",
    }
    return client.post("/webhooks/github", content=body, headers=headers)


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)


@pytest.fixture
def enqueued(monkeypatch):
    calls = []

    def fake(self, issue, force_retry=False):
        calls.append((issue["number"], force_retry))

    monkeypatch.setattr(CeleryOrchestrator, "enqueue_remediation", fake)
    return calls


def _issue(number=7, labels=(github.LABEL,), state="open"):
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": "details",
        "html_url": f"https://github.com/o/r/issues/{number}",
        "state": state,
        "labels": [{"name": n} for n in labels],
    }


def test_verify_signature_roundtrip():
    body = b'{"a":1}'
    assert webhooks.verify_signature(SECRET, body, _sign(body))
    assert not webhooks.verify_signature(SECRET, body, _sign(body, "other"))
    assert not webhooks.verify_signature(SECRET, body, None)
    assert not webhooks.verify_signature(SECRET, body, "sha1=abc")


def test_webhook_503_when_secret_unset(monkeypatch):
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    client = TestClient(main.app)
    assert _post(client, "ping", {}).status_code == 503


def test_webhook_rejects_bad_signature(secret):
    client = TestClient(main.app)
    resp = _post(client, "ping", {"zen": "x"}, signature="sha256=deadbeef")
    assert resp.status_code == 401


def test_webhook_ping(secret):
    client = TestClient(main.app)
    resp = _post(client, "ping", {"zen": "x"})
    assert resp.status_code == 200
    assert resp.json()["action"] == "pong"


def test_issue_labeled_enqueues_remediation(secret, enqueued):
    client = TestClient(main.app)
    payload = {"action": "labeled", "label": {"name": github.LABEL}, "issue": _issue(7)}
    resp = _post(client, "issues", payload)
    assert resp.status_code == 200
    assert resp.json()["action"] == "enqueued"
    assert enqueued == [(7, False)]


def test_issue_labeled_with_other_label_ignored(secret, enqueued):
    client = TestClient(main.app)
    payload = {"action": "labeled", "label": {"name": "bug"}, "issue": _issue(7, labels=("bug", github.LABEL))}
    assert _post(client, "issues", payload).json()["action"] == "ignored"
    assert enqueued == []


def test_issue_opened_without_label_ignored(secret, enqueued):
    client = TestClient(main.app)
    payload = {"action": "opened", "issue": _issue(7, labels=())}
    assert _post(client, "issues", payload).json()["action"] == "ignored"
    assert enqueued == []


def test_issue_edited_updates_tracked_title(secret, enqueued):
    store.upsert(7, title="old", issue_url="u", status="running")
    client = TestClient(main.app)
    issue = _issue(7)
    issue["title"] = "new title"
    resp = _post(client, "issues", {"action": "edited", "issue": issue})
    assert resp.json()["action"] == "updated"
    assert store.get(7)["title"] == "new title"


def test_pr_opened_marks_referenced_issue_completed(secret, enqueued):
    store.upsert(7, title="t", issue_url="u", session_id="s", status="running")
    store.upsert(8, title="t", issue_url="u", session_id="s", status="running")
    client = TestClient(main.app)
    payload = {
        "action": "opened",
        "pull_request": {
            "html_url": "https://github.com/o/r/pull/99",
            "state": "open",
            "title": "Fix #7: thing",
            "body": "",
        },
    }
    resp = _post(client, "pull_request", payload)
    assert resp.json()["issues"] == [7]
    assert store.get(7)["status"] == "completed"
    assert store.get(7)["pr_url"] == "https://github.com/o/r/pull/99"
    assert store.get(8)["status"] == "running"


def test_pr_closed_unmerged_requeues_issue(secret, enqueued, monkeypatch):
    store.upsert(7, title="t", issue_url="u", status="completed", pr_url="https://github.com/o/r/pull/99")
    monkeypatch.setattr(github, "get_issue", lambda n: _issue(n))
    client = TestClient(main.app)
    payload = {
        "action": "closed",
        "pull_request": {
            "html_url": "https://github.com/o/r/pull/99",
            "state": "closed",
            "merged": False,
            "title": "Fix #7",
            "body": None,
        },
    }
    resp = _post(client, "pull_request", payload)
    assert resp.json()["issues"] == [7]
    assert enqueued == [(7, True)]


def test_pr_closed_unmerged_requeues_by_stored_pr_url(secret, enqueued, monkeypatch):
    """The closing PR's text no longer mentions #7; the stored pr_url still links them."""
    store.upsert(7, title="t", issue_url="u", status="completed", pr_url="https://github.com/o/r/pull/99")
    store.upsert(8, title="t", issue_url="u", status="completed", pr_url="https://github.com/o/r/pull/100")
    monkeypatch.setattr(github, "get_issue", lambda n: _issue(n))
    client = TestClient(main.app)
    payload = {
        "action": "closed",
        "pull_request": {
            "html_url": "https://github.com/o/r/pull/99",
            "state": "closed",
            "merged": False,
            "title": "Refactor parser",
            "body": "",
        },
    }
    resp = _post(client, "pull_request", payload)
    assert resp.json()["issues"] == [7]
    assert enqueued == [(7, True)]
    assert store.get_status(8) == "completed"


def test_webhook_rejects_oversized_body(secret, enqueued, monkeypatch):
    monkeypatch.setattr(webhooks, "MAX_BODY_BYTES", 64)
    client = TestClient(main.app)
    payload = {"action": "opened", "issue": _issue(), "padding": "x" * 200}
    assert _post(client, "issues", payload).status_code == 413
    assert enqueued == []


def test_webhook_requires_content_length(secret):
    client = TestClient(main.app)
    body = b"{}"
    headers = {
        "X-GitHub-Event": "ping",
        "X-Hub-Signature-256": _sign(body),
        "Content-Type": "application/json",
    }
    resp = client.post("/webhooks/github", content=iter([body]), headers=headers)
    assert resp.status_code == 411


def test_pr_merged_does_not_requeue(secret, enqueued):
    store.upsert(7, title="t", issue_url="u", status="completed", pr_url="https://github.com/o/r/pull/99")
    client = TestClient(main.app)
    payload = {
        "action": "closed",
        "pull_request": {"html_url": "https://github.com/o/r/pull/99", "state": "closed", "merged": True, "title": "Fix #7", "body": ""},
    }
    assert _post(client, "pull_request", payload).json()["issues"] == []
    assert enqueued == []


def test_referenced_issues_parsing():
    assert webhooks.referenced_issues({"title": "Fix #12 and #3", "body": "see org/repo#5 and #12"}) == {12, 3}


def test_reconciliation_scan_still_works_without_webhook(monkeypatch, enqueued):
    """Webhook disabled: the periodic scan must still discover labelled issues."""
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr(github, "get_labeled_issues", lambda: [_issue(1), _issue(2)])
    assert remediation.scan_and_process() == {"scanned": 2, "polls_rearmed": 0}
    assert enqueued == [(1, False), (2, False)]


def test_create_and_delete_webhook(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://api.github.com/repos/test-org/test-repo/hooks",
        json={"id": 42, "config": {"url": "https://x/webhooks/github"}, "events": ["issues", "pull_request"]},
    )
    hook = github.create_webhook("https://x/webhooks/github", "s3cret")
    assert hook["id"] == 42
    sent = json.loads(httpx_mock.get_requests()[0].content)
    assert sent["config"]["secret"] == "s3cret"
    assert sent["events"] == ["issues", "pull_request"]

    httpx_mock.add_response(method="DELETE", url="https://api.github.com/repos/test-org/test-repo/hooks/42", status_code=204)
    github.delete_webhook(42)


def test_webhook_script_delete_dedupes_ids(monkeypatch):
    import importlib

    script = importlib.import_module("scripts.webhook")
    monkeypatch.setattr(github, "list_webhooks", lambda: [{"id": 42, "config": {"url": "https://x/webhooks/github"}}])
    deleted = []
    monkeypatch.setattr(github, "delete_webhook", deleted.append)
    assert script.main(["delete", "42", "--all-service"]) == 0
    assert deleted == [42]


def test_pr_closed_replay_does_not_requeue_running_issue(secret, enqueued, monkeypatch):
    """A redelivered close event must not clobber the replacement session."""
    store.upsert(7, title="t", issue_url="u", status="completed", pr_url="https://github.com/o/r/pull/99")
    monkeypatch.setattr(github, "get_issue", lambda n: _issue(n))
    client = TestClient(main.app)
    payload = {
        "action": "closed",
        "pull_request": {"html_url": "https://github.com/o/r/pull/99", "state": "closed", "merged": False, "title": "Fix #7", "body": ""},
    }
    assert _post(client, "pull_request", payload).json()["issues"] == [7]
    assert not store.get(7)["pr_url"]
    store.upsert(7, status="running")
    assert _post(client, "pull_request", payload).json()["issues"] == []
    assert enqueued == [(7, True)]
    assert store.get_status(7) == "running"


def test_pr_closed_requeues_failed_issue_with_pr_url(secret, enqueued, monkeypatch):
    """A session that errored after opening a PR is still `failed` + pr_url;
    closing that PR must hand it back too."""
    store.upsert(7, title="t", issue_url="u", status="failed", pr_url="https://github.com/o/r/pull/99")
    monkeypatch.setattr(github, "get_issue", lambda n: _issue(n))
    payload = {
        "action": "closed",
        "pull_request": {"html_url": "https://github.com/o/r/pull/99", "state": "closed", "merged": False, "title": "", "body": ""},
    }
    assert _post(TestClient(main.app), "pull_request", payload).json()["issues"] == [7]
    assert enqueued == [(7, True)]
    assert not store.get(7)["pr_url"]


def test_pr_closed_restores_pr_url_when_publish_fails(secret, enqueued, monkeypatch):
    store.upsert(7, title="t", issue_url="u", status="completed", pr_url="https://github.com/o/r/pull/99")
    monkeypatch.setattr(github, "get_issue", lambda n: _issue(n))

    def boom(*a, **k):
        raise RuntimeError("broker down")

    monkeypatch.setattr(CeleryOrchestrator, "enqueue_remediation", boom)
    payload = {
        "action": "closed",
        "pull_request": {"html_url": "https://github.com/o/r/pull/99", "state": "closed", "merged": False, "title": "", "body": ""},
    }
    with pytest.raises(RuntimeError):
        _post(TestClient(main.app), "pull_request", payload)
    assert store.get(7)["pr_url"] == "https://github.com/o/r/pull/99"
    assert store.get_status(7) == "failed"


def test_detach_closed_pr_is_conditional():
    store.upsert(7, title="t", issue_url="u", status="running", pr_url="https://x/1")
    assert store.detach_closed_pr(7, "https://x/1") is False
    store.upsert(7, status="completed")
    assert store.detach_closed_pr(7, "https://x/other") is False
    assert store.detach_closed_pr(7, "https://x/1") is True
    assert store.detach_closed_pr(7, "https://x/1") is False
    assert store.get(7)["pr_url"] == "" and store.get_status(7) == "failed"

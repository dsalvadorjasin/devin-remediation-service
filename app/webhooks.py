"""
GitHub webhook ingestion.

Webhooks give the service immediacy: as soon as an issue is labelled
`devin-remediate` (or a PR referencing a tracked issue changes state) GitHub
pushes an event here and we enqueue work through the orchestrator right away.

They are NOT the source of truth. Deliveries can be missed (tunnel down,
deploy in progress, GitHub retry exhausted), so the periodic Celery Beat scan
from Phase 2 keeps running as a reconciliation pass that re-lists labelled
issues and reconciles the store against GitHub's live state. Everything this
module does is therefore idempotent and safe to replay.
"""

import hashlib
import hmac
import logging
import os
import re

from app import github, store
from app.orchestrator import get_orchestrator

log = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-Hub-Signature-256"
EVENT_HEADER = "X-GitHub-Event"
DELIVERY_HEADER = "X-GitHub-Delivery"
# GitHub caps webhook payloads at 25 MB.
MAX_BODY_BYTES = 25 * 1024 * 1024

_ISSUE_REF = re.compile(r"(?<![A-Za-z0-9/])#(\d+)\b")


def webhook_secret() -> str | None:
    return os.getenv("GITHUB_WEBHOOK_SECRET") or None


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Constant-time check of GitHub's `sha256=<hex>` HMAC header."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header[len("sha256=") :])


def _issue_summary(issue: dict) -> dict:
    return {
        "number": issue["number"],
        "title": issue.get("title", ""),
        "body": issue.get("body") or "",
        "html_url": issue.get("html_url", ""),
    }


def _has_label(issue: dict) -> bool:
    return any(lbl.get("name") == github.LABEL for lbl in issue.get("labels", []))


def referenced_issues(pr: dict) -> set[int]:
    text = f"{pr.get('title') or ''}\n{pr.get('body') or ''}"
    return {int(n) for n in _ISSUE_REF.findall(text)}


def handle_event(event: str, payload: dict) -> dict:
    """Normalize a GitHub event into store updates / orchestrator calls.
    Returns a small dict describing what was done (useful for logs and tests)."""
    if event == "ping":
        return {"event": event, "action": "pong"}
    if event == "issues":
        return _handle_issues(payload)
    if event == "pull_request":
        return _handle_pull_request(payload)
    return {"event": event, "action": "ignored"}


def _handle_issues(payload: dict) -> dict:
    action = payload.get("action")
    issue = payload.get("issue") or {}
    if "pull_request" in issue or "number" not in issue:
        return {"event": "issues", "action": "ignored"}
    number = issue["number"]

    if action in ("labeled", "opened", "reopened") and _has_label(issue):
        if action == "labeled" and (payload.get("label") or {}).get("name") != github.LABEL:
            return {"event": "issues", "action": "ignored", "issue": number}
        log.info("Webhook: issue #%d labelled %s — enqueueing remediation", number, github.LABEL)
        get_orchestrator().enqueue_remediation(_issue_summary(issue))
        return {"event": "issues", "action": "enqueued", "issue": number}

    if action == "edited" and store.get(number):
        store.upsert(number, title=issue.get("title"), issue_url=issue.get("html_url"))
        return {"event": "issues", "action": "updated", "issue": number}

    return {"event": "issues", "action": "ignored", "issue": number}


def _handle_pull_request(payload: dict) -> dict:
    action = payload.get("action")
    pr = payload.get("pull_request") or {}
    pr_url = pr.get("html_url")
    touched: list[int] = []

    if action in ("opened", "reopened", "ready_for_review") and pr.get("state") == "open":
        for number in sorted(referenced_issues(pr)):
            if store.get(number):
                store.upsert(number, status="completed", pr_url=pr_url)
                touched.append(number)
    elif action == "closed" and not pr.get("merged") and pr_url:
        # PR abandoned: hand every issue that recorded this PR back to the
        # guards in process_issue, which re-check GitHub and create a fresh
        # session if needed. Keyed on the stored pr_url, not the current PR
        # text, so edits to the PR title/body cannot hide the closure.
        linked = sorted(
            e["issue_number"]
            for e in store.get_all()
            if e.get("pr_url") == pr_url and e.get("status") == "completed"
        )
        for number in linked:
            try:
                issue = github.get_issue(number)
            except Exception as exc:
                log.warning("Could not fetch issue #%d after PR close: %s", number, exc)
                continue
            if _has_label(issue) and issue.get("state") == "open":
                store.upsert(number, status="failed", pr_url="")
                get_orchestrator().enqueue_remediation(_issue_summary(issue), force_retry=True)
                touched.append(number)

    return {"event": "pull_request", "action": action, "issues": touched}

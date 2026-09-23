"""
Core remediation logic, independent of any scheduler.

- `scan_and_process` lists labelled issues and hands each one to the
  orchestrator (used by the startup scan, the /scan route and the periodic
  reconciliation scan).
- `process_issue` applies the dedup guards and creates a Devin session.
- `poll_session_once` reconciles one running session with Devin/GitHub and
  reports whether polling should continue.

These functions are plain synchronous Python so that any Orchestrator
implementation (Celery today, Temporal later) can wrap them.
"""

import logging

from app import devin, github, store
from app.observability import (
    CLAIM_CONFLICTS,
    POLLS,
    REMEDIATION_OUTCOMES,
    SESSION_CREATE_FAILURES,
    SESSIONS_CREATED,
    span,
)
from app.orchestrator import get_orchestrator
from app.orchestrator.base import POLL_INTERVAL_SECONDS, POLL_LEASE_GRACE_SECONDS

log = logging.getLogger(__name__)

# How long a remediation claim (row `running` with no session yet) stays
# reserved before the reconciliation scan may hand the issue to a new task.
# Must comfortably exceed a Devin create_session round trip including retries.
CREATE_LEASE_SECONDS = 10 * 60


def _lease_seconds(delay_seconds: int) -> int:
    return delay_seconds + POLL_LEASE_GRACE_SECONDS


def arm_poll(
    issue_number: int,
    session_id: str,
    delay_seconds: int = POLL_INTERVAL_SECONDS,
    only_if_lost: bool = False,
) -> bool:
    """Claim poll ownership for a running session in the store, then schedule
    the first poll of the chain. Returns False if the claim was not granted
    (another chain already holds a live lease)."""
    token = store.claim_poll(
        issue_number, session_id, _lease_seconds(delay_seconds), only_if_lost=only_if_lost
    )
    if token is None:
        return False
    return _publish_poll(issue_number, session_id, delay_seconds, token)


def continue_poll(issue_number: int, session_id: str, poll_token: str, delay_seconds: int) -> None:
    """Renew the lease held by an existing chain and schedule its next poll."""
    if not store.renew_poll(issue_number, poll_token, _lease_seconds(delay_seconds)):
        return
    _publish_poll(issue_number, session_id, delay_seconds, poll_token)


def _publish_poll(issue_number: int, session_id: str, delay_seconds: int, poll_token: str) -> bool:
    """Schedule the next hop of a chain; on broker failure give the lease back
    so the next reconciliation scan can re-arm immediately."""
    try:
        get_orchestrator().schedule_poll(issue_number, session_id, poll_token, delay_seconds)
    except Exception as exc:
        log.error("Could not schedule poll for issue #%d: %s", issue_number, exc)
        store.release_poll(issue_number, poll_token=poll_token)
        return False
    return True


def process_issue(issue: dict, force_retry: bool = False) -> bool:
    """
    Decide whether to create/retry a Devin session for a single GitHub issue.
    - open PR found on GitHub → mark completed, skip (checked every scan so a
      closed PR causes the issue to be re-queued on the next scan).
    - running → always skip (never duplicate).
    - failed → skip unless force_retry=True.
    - not in store, or completed with no open PR → create session.

    The final gate is ``store.claim_remediation``, a single atomic statement,
    so overlapping tasks for the same issue (replica startup scans, Beat,
    /scan, webhook redeliveries) can never both reach ``create_session``.
    A stranded claim (worker died before saving the session) expires after
    CREATE_LEASE_SECONDS and is then re-claimable.

    Returns True when a session was created (and a poll scheduled).
    """
    # for valid keys in response, check GitHub API docs:
    # https://docs.github.com/en/rest/issues/issues?apiVersion=2026-03-10#list-repository-issues
    number = issue["number"]
    title = issue["title"]
    body = issue.get("body") or ""
    issue_url = issue["html_url"]

    current_status = store.get_status(number)

    # 1. Check GitHub for an open PR — covers all states including previously completed
    # issues whose PR was subsequently closed.
    pr_url = github.find_existing_pr(number)
    if pr_url:
        log.info("Issue #%d already has open PR %s — marking completed", number, pr_url)
        store.upsert(number, title=title, issue_url=issue_url, status="completed", pr_url=pr_url)
        return False

    # 2. Currently running → never spawn a duplicate. Enforced by the atomic
    # claim below, which only lets a stranded claim (no session, expired
    # creation lease) through.

    # 3. Failed → only retry when explicitly requested
    if current_status == "failed" and not force_retry:
        return False

    # 4. Atomically claim the issue, then create a Devin session and post a
    # comment on the issue with the session URL.
    if not store.claim_remediation(number, title, issue_url, CREATE_LEASE_SECONDS):
        log.info("Issue #%d already claimed by another remediation task — skipping", number)
        CLAIM_CONFLICTS.inc()
        return False
    log.info("Creating Devin session for issue #%d: %s", number, title)
    try:
        with span("devin.create_session", issue_number=number):
            result = devin.create_session(number, title, body)
        SESSIONS_CREATED.inc()
        session_id = result.get("session_id") or result.get("id")
        session_url = result.get("url") or result.get("session_url")
        store.upsert(number, session_id=session_id, session_url=session_url, status="running")
        log.info("Session %s created for issue #%d", session_id, number)
        try:
            github.post_comment(
                number,
                f"🤖 A Devin session has been started to address this issue.\n\nSession: {session_url}",
            )
        except Exception as comment_exc:
            log.warning("Could not post comment on issue #%d: %s", number, comment_exc)
    except Exception as exc:
        log.error("Failed to create Devin session for issue #%d: %s", number, exc)
        SESSION_CREATE_FAILURES.inc()
        REMEDIATION_OUTCOMES.labels(status="failed").inc()
        store.upsert(number, status="failed")
        store.release_poll(number)
        return False

    if session_id:
        arm_poll(number, session_id)
    return True


def scan_and_process(force_retry: bool = False) -> dict:
    """Fetch labelled issues from GitHub and enqueue each one for processing
    through the orchestrator. Also re-arms polling for any session that is
    marked running but has no poll in flight (e.g. after a worker restart)."""
    log.info("Starting issue scan (force_retry=%s)", force_retry)
    try:
        with span("github.get_labeled_issues"):
            issues = github.get_labeled_issues()
    except Exception as exc:
        log.error("Failed to fetch issues from GitHub: %s", exc)
        return {"error": str(exc)}

    log.info("Found %d labeled issue(s)", len(issues))
    orchestrator = get_orchestrator()
    for issue in issues:
        orchestrator.enqueue_remediation(issue, force_retry=force_retry)

    rearmed = 0
    for entry in store.get_unpolled_running():
        if arm_poll(entry["issue_number"], entry["session_id"], delay_seconds=0, only_if_lost=True):
            rearmed += 1
            log.info(
                "Re-armed lost poll for issue #%d (session %s)",
                entry["issue_number"],
                entry["session_id"],
            )

    return {"scanned": len(issues), "polls_rearmed": rearmed}


def poll_session_once(issue_number: int, session_id: str, poll_token: str) -> bool:
    """
    Fetch the latest state of one Devin session, map it to the internal status
    model, and update the store. If Devin's response doesn't include a PR URL
    yet, fall back to searching GitHub directly. A PR that exists while Devin
    is still "running" counts as completed.

    ``poll_token`` is the calling chain's lease token; a superseded chain stops.

    Returns True if the session is still running and should be polled again.
    """
    entry = store.get(issue_number)
    if not entry or entry["status"] != "running" or entry["session_id"] != session_id:
        # Superseded (retried, completed elsewhere, or cleared) — stop polling.
        POLLS.labels(outcome="superseded").inc()
        return False
    if entry["poll_token"] != poll_token:
        # Lease re-claimed by another chain — stop.
        POLLS.labels(outcome="superseded").inc()
        return False
    try:
        with span("devin.get_session", issue_number=issue_number, session_id=session_id):
            data = devin.get_session(session_id)
    except Exception as exc:
        log.warning("Could not poll session %s: %s", session_id, exc)
        POLLS.labels(outcome="error").inc()
        return True

    raw_status = data.get("status")
    status_detail = data.get("status_detail")
    new_status = devin.map_devin_status(raw_status, status_detail)
    pr_url = devin.extract_pr_url(data)
    log.info(
        "Poll session %s (issue #%d): devin_status=%r detail=%r → %s",
        session_id,
        issue_number,
        raw_status,
        status_detail,
        new_status,
    )
    if not pr_url:
        try:
            pr_url = github.find_existing_pr(issue_number)
        except Exception as exc:
            log.warning("GitHub PR lookup failed for issue #%d: %s", issue_number, exc)
    if pr_url and new_status == "running":
        new_status = "completed"
    store.upsert(issue_number, status=new_status, pr_url=pr_url)
    POLLS.labels(outcome=new_status).inc()
    if new_status != "running":
        REMEDIATION_OUTCOMES.labels(status=new_status).inc()
        store.release_poll(issue_number, poll_token=poll_token)
        return False
    return True

"""
Celery task definitions. Each task is a thin wrapper over app.remediation so
the business logic stays scheduler-agnostic.
"""

import logging

from app import remediation
from app.celery_app import celery_app
from app.orchestrator.base import POLL_INTERVAL_SECONDS

log = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.scan_task")
def scan_task(force_retry: bool = False) -> dict:
    """Periodic/reconciliation scan: list labelled issues and fan out one
    remediate_issue_task per issue."""
    return remediation.scan_and_process(force_retry=force_retry)


@celery_app.task(name="app.tasks.remediate_issue_task")
def remediate_issue_task(issue: dict, force_retry: bool = False) -> bool:
    """Apply the dedup guards for one issue and create a Devin session."""
    return remediation.process_issue(issue, force_retry=force_retry)


@celery_app.task(name="app.tasks.discovery_task")
def discovery_task(dry_run: bool = False) -> dict:
    """Update the target repo checkout, run every discovery source, and file
    new findings as `devin-remediate` issues. Newly created issues are picked
    up by an immediate scan (or the webhook, if registered)."""
    from app.discovery import SemgrepDiscoverySource, ingest_findings

    findings = SemgrepDiscoverySource().discover()
    result = ingest_findings(findings, dry_run=dry_run)
    if result["created"] and not dry_run:
        from app.orchestrator import get_orchestrator

        get_orchestrator().enqueue_scan(force_retry=False)
    return result


@celery_app.task(name="app.tasks.poll_session_task")
def poll_session_task(issue_number: int, session_id: str, poll_token: str) -> bool:
    """Poll a running session once; re-queue itself while it is still running.

    Kept as a self-rescheduling task (rather than a loop) so a Temporal
    orchestrator can replace it with a durable timer later.
    """
    still_running = remediation.poll_session_once(issue_number, session_id, poll_token)
    if still_running:
        remediation.continue_poll(issue_number, session_id, poll_token, POLL_INTERVAL_SECONDS)
    return still_running

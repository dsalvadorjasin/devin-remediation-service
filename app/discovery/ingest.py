"""
Turn findings into `devin-remediate` issues, once each.

Dedup key is the finding fingerprint embedded in the issue body as an HTML
comment; re-running discovery against the same code is a no-op.
"""

import logging
import os

from app import github
from app.discovery.base import Finding
from app.observability import DISCOVERY_FINDINGS, DISCOVERY_ISSUES_CREATED

log = logging.getLogger(__name__)


def max_new_issues() -> int | None:
    return int(os.getenv("SEMGREP_MAX_FINDINGS", "0")) or None


def ingest_findings(
    findings: list[Finding], dry_run: bool = False, max_new: int | None = None
) -> dict:
    """File one issue per new fingerprint. `max_new` caps issues created per
    call (default `SEMGREP_MAX_FINDINGS`) and is applied after dedup, so every
    run makes progress through the backlog instead of re-checking the same
    leading findings."""
    if max_new is None:
        max_new = max_new_issues()
    for f in findings:
        DISCOVERY_FINDINGS.labels(source=f.source).inc()
    existing = github.find_issues_by_fingerprint([f.fingerprint for f in findings])
    created: list[dict] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for f in findings:
        if f.fingerprint in seen or f.fingerprint in existing:
            skipped.append(f.fingerprint)
            continue
        if max_new is not None and len(created) >= max_new:
            break
        seen.add(f.fingerprint)
        if dry_run:
            created.append({"fingerprint": f.fingerprint, "title": f.title})
            continue
        issue = github.create_issue(f.title, f.issue_body())
        log.info("Created issue #%s for %s", issue.get("number"), f.title)
        DISCOVERY_ISSUES_CREATED.inc()
        created.append(
            {"fingerprint": f.fingerprint, "number": issue.get("number"), "html_url": issue.get("html_url")}
        )
    return {"findings": len(findings), "created": created, "skipped": len(skipped)}

"""
Turn findings into `devin-remediate` issues, once each.

Dedup key is the finding fingerprint embedded in the issue body as an HTML
comment; re-running discovery against the same code is a no-op.
"""

import logging

from app import github
from app.discovery.base import Finding

log = logging.getLogger(__name__)


def ingest_findings(findings: list[Finding], dry_run: bool = False) -> dict:
    existing = github.find_issues_by_fingerprint([f.fingerprint for f in findings])
    created: list[dict] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for f in findings:
        if f.fingerprint in seen or f.fingerprint in existing:
            skipped.append(f.fingerprint)
            continue
        seen.add(f.fingerprint)
        if dry_run:
            created.append({"fingerprint": f.fingerprint, "title": f.title})
            continue
        issue = github.create_issue(f.title, f.issue_body())
        log.info("Created issue #%s for %s", issue.get("number"), f.title)
        created.append(
            {"fingerprint": f.fingerprint, "number": issue.get("number"), "html_url": issue.get("html_url")}
        )
    return {"findings": len(findings), "created": created, "skipped": len(skipped)}

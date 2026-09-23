"""
Devin API v3 client for the remediation service.

Provides the operations used by main.py to delegate issue fixes to Devin:
- Creating a session from a structured prompt built around a GitHub issue.
- Fetching the current state of a running session.
- Mapping Devin's session status values to the internal status model (running, completed, failed).
- Extracting a PR URL from a session response.

Requests use app.http_client: shared timeouts, retries for GET (polling) and
for 429 on session creation; a timed-out create is never blindly resent.
"""

import os

from app import http_client

DEVIN_API = "https://api.devin.ai/v3"

PROMPT_TEMPLATE = """You are working on the GitHub repository {repo}.

Please fix issue #{issue_number}: "{title}".

Issue description:
{body}

Instructions:
- Clone the repository and fix the issue described above.
- Open a pull request with your fix.
- The PR title should reference the issue, e.g. "Fix #{issue_number}: {title}".
- The PR body must include the line "Closes #{issue_number}" so that GitHub automatically closes the issue when the PR is merged.
- Make sure all existing tests pass and all pre-commit hooks pass.
"""


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ['DEVIN_API_KEY']}",
        "Content-Type": "application/json",
    }


def _org_id() -> str:
    return os.environ["DEVIN_ORG_ID"]


def create_session(issue_number: int, title: str, body: str) -> dict:
    """
    Create a Devin session to fix the given issue.
    Returns the full response dict (includes session_id and url).
    """
    repo = os.environ["GITHUB_REPO"]
    prompt = PROMPT_TEMPLATE.format(
        repo=repo,
        issue_number=issue_number,
        title=title,
        body=body or "No additional description provided.",
    )
    payload = {"prompt": prompt}
    with http_client.client() as client:
        resp = http_client.request(
            client,
            "POST",
            f"{DEVIN_API}/organizations/{_org_id()}/sessions",
            headers=_headers(),
            json=payload,
        )
    return resp.json()


def get_session(session_id: str) -> dict:
    """
    Fetch the current state of a Devin session.
    Returns the full response dict.
    """
    with http_client.client() as client:
        resp = http_client.request(
            client,
            "GET",
            f"{DEVIN_API}/organizations/{_org_id()}/sessions/{session_id}",
            headers=_headers(),
        )
    return resp.json()


def map_devin_status(devin_status: str | None, status_detail: str | None = None) -> str:
    """Map Devin v3 session status to our internal status: running, completed, or failed.

    Documented status values: new, claimed, running, exit, error, suspended, resuming.
    When status is "running", status_detail can be "finished" — meaning the task is
    done but the session hasn't exited yet.
    """
    if devin_status is None:
        return "running"
    status_lower = devin_status.lower()
    if status_lower == "exit":
        return "completed"
    if status_lower in ("error", "suspended"):
        return "failed"
    if status_lower == "running" and status_detail == "finished":
        return "completed"
    return "running"


def extract_pr_url(session_data: dict) -> str | None:
    """Extract the first PR URL from a session response's pull_requests list."""
    prs = session_data.get("pull_requests") or []
    if prs:
        return prs[0].get("pr_url")
    return None

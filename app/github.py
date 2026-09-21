"""
GitHub REST API client for the Devin remediation service.

Provides three operations used by main.py to drive the automation loop:
- Fetching open issues tagged with the remediation label.
- Detecting whether an open PR already exists for a given issue.
- Posting status comments on issues when a Devin session is started.
"""

import os

import httpx

GITHUB_API = "https://api.github.com"
LABEL = "devin-remediate"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _repo() -> str:
    return os.environ["GITHUB_REPO"]


def get_labeled_issues() -> list[dict]:
    """Return all open issues tagged with LABEL."""
    # See GitHub API docs:
    # https://docs.github.com/en/rest/issues/issues?apiVersion=2026-03-10#list-repository-issues
    url = f"{GITHUB_API}/repos/{_repo()}/issues"
    params = {"labels": LABEL, "state": "open", "per_page": 100}
    with httpx.Client() as client:
        resp = client.get(url, headers=_headers(), params=params)
        resp.raise_for_status()
    issues = resp.json()
    # Exclude pull requests (GitHub returns PRs as issues too)
    return [i for i in issues if "pull_request" not in i]


def find_existing_pr(issue_number: int) -> str | None:
    """
    Return the HTML URL of any open PR that references this issue, or None.

    Two checks are performed in order:
    1. Issue timeline cross-referenced events — covers PRs that use GitHub's
       closing keywords (Fixes/Closes/Resolves #N) in their body.
    2. Full open-PR scan — covers PRs that mention #N in their title or body
       (e.g. the format Devin is instructed to use: "Fix #N: Title"),
       but did not produce a GitHub cross-reference event.
    """
    owner, repo = _repo().split("/", 1)

    # --- check 1: timeline cross-references ---
    timeline_url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/{issue_number}/timeline"
    with httpx.Client() as client:
        resp = client.get(timeline_url, headers=_headers(), params={"per_page": 100})
        resp.raise_for_status()
    for event in resp.json():
        if event.get("event") != "cross-referenced":
            continue
        source_issue = event.get("source", {}).get("issue", {})
        if source_issue.get("pull_request") and source_issue.get("state") == "open":
            return source_issue["html_url"]

    # --- check 2: scan open PRs for title/body mention of #issue_number ---
    prs_url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls"
    needle = f"#{issue_number}"
    with httpx.Client() as client:
        resp = client.get(
            prs_url, headers=_headers(), params={"state": "open", "per_page": 100}
        )
        resp.raise_for_status()
    for pr in resp.json():
        title = pr.get("title", "")
        body = pr.get("body") or ""
        if needle in title or needle in body:
            return pr["html_url"]

    return None


def post_comment(issue_number: int, body: str) -> None:
    """Post a comment on a GitHub issue."""
    url = f"{GITHUB_API}/repos/{_repo()}/issues/{issue_number}/comments"
    with httpx.Client() as client:
        resp = client.post(url, headers=_headers(), json={"body": body})
        resp.raise_for_status()


def get_issue(issue_number: int) -> dict:
    url = f"{GITHUB_API}/repos/{_repo()}/issues/{issue_number}"
    with httpx.Client() as client:
        resp = client.get(url, headers=_headers())
        resp.raise_for_status()
    return resp.json()


def add_labels(issue_number: int, labels: list[str]) -> None:
    url = f"{GITHUB_API}/repos/{_repo()}/issues/{issue_number}/labels"
    with httpx.Client() as client:
        resp = client.post(url, headers=_headers(), json={"labels": labels})
        resp.raise_for_status()


def create_issue(title: str, body: str, labels: list[str] | None = None) -> dict:
    """Create an issue (Issues: write). Defaults to the remediation label."""
    url = f"{GITHUB_API}/repos/{_repo()}/issues"
    payload = {"title": title, "body": body, "labels": labels if labels is not None else [LABEL]}
    with httpx.Client() as client:
        resp = client.post(url, headers=_headers(), json=payload)
        resp.raise_for_status()
    return resp.json()


def find_issues_by_fingerprint(fingerprints: list[str], marker: str = "semgrep-fingerprint") -> dict[str, dict]:
    """Map fingerprint -> open issue whose body contains
    `<!-- {marker}: {fingerprint} -->`. One paginated listing of open labelled
    issues, then a local scan, so a batch of N findings costs O(pages) calls."""
    wanted = set(fingerprints)
    found: dict[str, dict] = {}
    if not wanted:
        return found
    url = f"{GITHUB_API}/repos/{_repo()}/issues"
    page = 1
    with httpx.Client() as client:
        while True:
            resp = client.get(
                url,
                headers=_headers(),
                params={"labels": LABEL, "state": "open", "per_page": 100, "page": page},
            )
            resp.raise_for_status()
            issues = resp.json()
            for issue in issues:
                if "pull_request" in issue:
                    continue
                body = issue.get("body") or ""
                for fp in wanted:
                    if f"<!-- {marker}: {fp} -->" in body:
                        found[fp] = issue
            if len(issues) < 100:
                break
            page += 1
    return found


# --- webhooks (Webhooks: read/write) ---------------------------------------


def list_webhooks() -> list[dict]:
    url = f"{GITHUB_API}/repos/{_repo()}/hooks"
    with httpx.Client() as client:
        resp = client.get(url, headers=_headers(), params={"per_page": 100})
        resp.raise_for_status()
    return resp.json()


def create_webhook(payload_url: str, secret: str, events: list[str] | None = None) -> dict:
    """Register a repository webhook pointing at `payload_url`.
    https://docs.github.com/en/rest/repos/webhooks#create-a-repository-webhook
    """
    url = f"{GITHUB_API}/repos/{_repo()}/hooks"
    body = {
        "name": "web",
        "active": True,
        "events": events or ["issues", "pull_request"],
        "config": {
            "url": payload_url,
            "content_type": "json",
            "secret": secret,
            "insecure_ssl": "0",
        },
    }
    with httpx.Client() as client:
        resp = client.post(url, headers=_headers(), json=body)
        resp.raise_for_status()
    return resp.json()


def delete_webhook(hook_id: int) -> None:
    url = f"{GITHUB_API}/repos/{_repo()}/hooks/{hook_id}"
    with httpx.Client() as client:
        resp = client.delete(url, headers=_headers())
        resp.raise_for_status()

"""
FastAPI application and entry point for the Devin remediation service.

On startup, runs an immediate GitHub issue scan and launches two background loops:
one that re-scans for new labeled issues on a configurable interval, and one that
polls running Devin sessions every 60 seconds to update their status and PR URL.

Exposes three routes:
- GET  /        — HTML dashboard showing the current state of all tracked issues.
- GET  /status  — JSON list of all tracked issues (polled by the dashboard).
- POST /scan    — Manually trigger a scan; pass force_retry=true to retry failed sessions.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

load_dotenv()

from app import db, devin, github, store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL_MINUTES", "5")) * 60
TEMPLATES_DIR = Path(__file__).parent / "templates"


# ---------------------------------------------------------------------------
# Core scan logic
# ---------------------------------------------------------------------------


def _process_issue(issue: dict, force_retry: bool = False) -> None:
    """
    Decide whether to create/retry a Devin session for a single GitHub issue.
    - open PR found on GitHub → mark completed, skip (checked every scan so a
      closed PR causes the issue to be re-queued on the next scan).
    - running → always skip (never duplicate).
    - failed → skip unless force_retry=True.
    - not in store, or completed with no open PR → create session.
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
        store.upsert(
            number, title=title, issue_url=issue_url, status="completed", pr_url=pr_url
        )
        return

    # 2. Currently running → never spawn a duplicate
    if current_status == "running":
        return

    # 3. Failed → only retry when explicitly requested
    if current_status == "failed" and not force_retry:
        return

    # 5. Create a new Devin session, posting a comment on the issue with the session URL.
    log.info("Creating Devin session for issue #%d: %s", number, title)
    store.upsert(number, title=title, issue_url=issue_url, status="running")
    try:
        result = devin.create_session(number, title, body)
        session_id = result.get("session_id") or result.get("id")
        session_url = result.get("url") or result.get("session_url")
        store.upsert(
            number, session_id=session_id, session_url=session_url, status="running"
        )
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
        store.upsert(number, status="failed")


def scan_and_process(force_retry: bool = False) -> dict:
    """Fetch labeled issues from GitHub, and process each one
    according to its current state in the store and whether
    force_retry is requested.
    """
    log.info("Starting issue scan (force_retry=%s)", force_retry)
    try:
        issues = github.get_labeled_issues()
    except Exception as exc:
        log.error("Failed to fetch issues from GitHub: %s", exc)
        return {"error": str(exc)}

    log.info("Found %d labeled issue(s)", len(issues))
    for issue in issues:
        _process_issue(issue, force_retry=force_retry)

    return {"scanned": len(issues)}


# ---------------------------------------------------------------------------
# Session status polling
# ---------------------------------------------------------------------------


async def _poll_running_sessions() -> None:
    """
    Background loop that polls Devin every 60 seconds for all sessions currently
    marked as running in the store. For each one, fetches the latest session state,
    maps it to the internal status model, and updates the store. If Devin's response
    doesn't include a PR URL yet, falls back to searching GitHub directly.
    """
    while True:
        await asyncio.sleep(60)
        running = [
            e for e in store.get_all() if e["status"] == "running" and e["session_id"]
        ]
        for entry in running:
            try:
                data = devin.get_session(entry["session_id"])
                raw_status = data.get("status")
                status_detail = data.get("status_detail")
                new_status = devin.map_devin_status(raw_status, status_detail)
                pr_url = devin.extract_pr_url(data)
                log.info(
                    "Poll session %s (issue #%d): devin_status=%r detail=%r → %s",
                    entry["session_id"],
                    entry["issue_number"],
                    raw_status,
                    status_detail,
                    new_status,
                )
                # If Devin hasn't surfaced a PR URL yet, fall back to GitHub search
                if not pr_url:
                    pr_url = github.find_existing_pr(entry["issue_number"])
                # A PR exists but Devin hasn't exited yet — the work is done
                if pr_url and new_status == "running":
                    new_status = "completed"
                store.upsert(entry["issue_number"], status=new_status, pr_url=pr_url)
            except Exception as exc:
                log.warning("Could not poll session %s: %s", entry["session_id"], exc)


# ---------------------------------------------------------------------------
# Background periodic scan
# ---------------------------------------------------------------------------


async def _periodic_scan() -> None:
    """
    Background loop that triggers an issue scan every SCAN_INTERVAL seconds.
    This ensures that new issues are picked up and processed even if no one manually
    triggers a scan via the /scan endpoint.
    """
    while True:
        await asyncio.sleep(SCAN_INTERVAL)
        log.info("Periodic scan triggered")
        scan_and_process(force_retry=False)


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    log.info("Startup scan...")
    scan_and_process(force_retry=False)
    asyncio.create_task(_periodic_scan())
    asyncio.create_task(_poll_running_sessions())
    yield


app = FastAPI(title="Devin Superset Remediation", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html = (TEMPLATES_DIR / "index.html").read_text()
    return HTMLResponse(content=html)


@app.get("/status")
async def status():
    return JSONResponse(content=store.get_all())


@app.post("/scan")
async def manual_scan(force_retry: bool = False):
    """
    Manually trigger a scan.
    - New issues (no existing PR) → create session.
    - failed/pending → retry if force_retry=True.
    - completed → always skip.
    """
    result = scan_and_process(force_retry=force_retry)
    return JSONResponse(content={"ok": True, **result})

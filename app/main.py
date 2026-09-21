"""
FastAPI application and entry point for the Devin remediation service.

The API process only enqueues work: on startup it asks the orchestrator for an
initial scan, and every route that triggers work goes through the orchestrator
as well. The actual scanning, Devin session creation and status polling run in
Celery workers (see app/tasks.py), with the periodic reconciliation scan
driven by Celery Beat (see app/celery_app.py).

Exposes five routes:
- GET  /        — HTML dashboard showing the current state of all tracked issues.
- GET  /status  — JSON list of all tracked issues (polled by the dashboard).
- POST /scan    — Manually trigger a scan; pass force_retry=true to retry failed sessions.
- POST /webhooks/github — GitHub webhook receiver (HMAC-verified with GITHUB_WEBHOOK_SECRET).
- POST /ingest/semgrep  — Accept a SARIF document (or trigger discovery) and file new findings as issues.
"""

import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse

load_dotenv()

from app import db, store, webhooks
from app.discovery import ingest_findings, parse_sarif
from app.orchestrator import get_orchestrator
from app.remediation import process_issue, scan_and_process  # noqa: F401 (re-exported)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    log.info("Enqueueing startup scan...")
    try:
        get_orchestrator().enqueue_scan(force_retry=False)
    except Exception as exc:
        log.error("Could not enqueue startup scan: %s", exc)
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
    Enqueue a scan through the orchestrator.
    - New issues (no existing PR) → create session.
    - failed → retry if force_retry=True.
    - running → always skip.
    """
    get_orchestrator().enqueue_scan(force_retry=force_retry)
    return JSONResponse(content={"ok": True, "enqueued": True, "force_retry": force_retry})


@app.post("/webhooks/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str = Header(default=""),
    x_github_delivery: str = Header(default=""),
    content_length: int | None = Header(default=None),
):
    secret = webhooks.webhook_secret()
    if not secret:
        raise HTTPException(status_code=503, detail="GITHUB_WEBHOOK_SECRET not configured")
    if content_length is None:
        raise HTTPException(status_code=411, detail="Content-Length required")
    if content_length > webhooks.MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    body = await request.body()
    if len(body) > webhooks.MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    if not webhooks.verify_signature(secret, body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="invalid signature")
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON")
    result = await run_in_threadpool(webhooks.handle_event, x_github_event, payload)
    log.info("Webhook %s delivery=%s -> %s", x_github_event, x_github_delivery, result)
    return JSONResponse(content={"ok": True, "delivery": x_github_delivery, **result})


def _check_ingest_token(authorization: str | None, x_ingest_token: str | None) -> None:
    expected = os.getenv("INGEST_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="INGEST_TOKEN not configured")
    presented = x_ingest_token
    if not presented and authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()
    if not presented or not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="invalid ingest token")


@app.post("/ingest/semgrep")
async def ingest_semgrep(
    request: Request,
    dry_run: bool = False,
    authorization: str | None = Header(default=None),
    x_ingest_token: str | None = Header(default=None),
):
    """
    Two modes:
    - Body is a SARIF document (e.g. from the semgrep GitHub Action) -> parse it
      here and create a `devin-remediate` issue for every new fingerprint.
    - Empty body -> enqueue a full discovery run (clone + semgrep) on a worker.
    """
    _check_ingest_token(authorization, x_ingest_token)
    body = await request.body()
    if not body.strip():
        get_orchestrator().enqueue_discovery(dry_run=dry_run)
        return JSONResponse(content={"ok": True, "enqueued": True, "dry_run": dry_run})
    try:
        sarif = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="body must be SARIF JSON")
    findings = parse_sarif(sarif)
    result = ingest_findings(findings, dry_run=dry_run)
    if result["created"] and not dry_run:
        get_orchestrator().enqueue_scan(force_retry=False)
    return JSONResponse(content={"ok": True, "dry_run": dry_run, **result})

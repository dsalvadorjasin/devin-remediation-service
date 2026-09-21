"""
FastAPI application and entry point for the Devin remediation service.

The API process only enqueues work: on startup it asks the orchestrator for an
initial scan, and every route that triggers work goes through the orchestrator
as well. The actual scanning, Devin session creation and status polling run in
Celery workers (see app/tasks.py), with the periodic reconciliation scan
driven by Celery Beat (see app/celery_app.py).

Exposes four routes:
- GET  /        — HTML dashboard showing the current state of all tracked issues.
- GET  /status  — JSON list of all tracked issues (polled by the dashboard).
- POST /scan    — Manually trigger a scan; pass force_retry=true to retry failed sessions.
- POST /webhooks/github — GitHub webhook receiver (HMAC-verified with GITHUB_WEBHOOK_SECRET).
"""

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

load_dotenv()

from app import db, store, webhooks
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
):
    secret = webhooks.webhook_secret()
    if not secret:
        raise HTTPException(status_code=503, detail="GITHUB_WEBHOOK_SECRET not configured")
    body = await request.body()
    if not webhooks.verify_signature(secret, body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="invalid signature")
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON")
    result = webhooks.handle_event(x_github_event, payload)
    log.info("Webhook %s delivery=%s -> %s", x_github_event, x_github_delivery, result)
    return JSONResponse(content={"ok": True, "delivery": x_github_delivery, **result})

"""
Ingest layer: everything that puts work into the system. Handlers only
validate/authenticate and hand off to the Orchestrator (or, for pushed SARIF,
file issues inline); no Devin calls happen in this process.
"""

import hmac
import json
import logging
import os

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app import webhooks
from app.discovery import MAX_SARIF_BYTES, ingest_findings, parse_sarif
from app.orchestrator import get_orchestrator

log = logging.getLogger(__name__)

router = APIRouter(tags=["ingest"])


@router.post("/scan")
async def manual_scan(
    force_retry: bool = False,
    authorization: str | None = Header(default=None),
    x_ingest_token: str | None = Header(default=None),
):
    """
    Enqueue a scan through the orchestrator. Requires INGEST_TOKEN (same
    scheme as /ingest/semgrep) since the API is reachable through the Ingress.
    - New issues (no existing PR) → create session.
    - failed → retry if force_retry=True.
    - running → always skip.
    """
    _check_ingest_token(authorization, x_ingest_token)
    get_orchestrator().enqueue_scan(force_retry=force_retry)
    return JSONResponse(content={"ok": True, "enqueued": True, "force_retry": force_retry})


@router.post("/webhooks/github")
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


def _ingest_sarif_body(body: bytes, dry_run: bool) -> dict | None:
    try:
        sarif = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(sarif, dict):
        return None
    return ingest_findings(parse_sarif(sarif), dry_run=dry_run)


@router.post("/ingest/semgrep")
async def ingest_semgrep(
    request: Request,
    dry_run: bool = False,
    authorization: str | None = Header(default=None),
    x_ingest_token: str | None = Header(default=None),
    content_length: int | None = Header(default=None),
):
    """
    Two modes:
    - Body is a SARIF document (e.g. from the semgrep GitHub Action) -> parse it
      here and create a `devin-remediate` issue for every new fingerprint.
    - Empty body -> enqueue a full discovery run (clone + semgrep) on a worker.
    """
    _check_ingest_token(authorization, x_ingest_token)
    if content_length is None:
        raise HTTPException(status_code=411, detail="Content-Length required")
    if content_length > MAX_SARIF_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    body = await request.body()
    if len(body) > MAX_SARIF_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    if not body.strip():
        get_orchestrator().enqueue_discovery(dry_run=dry_run)
        return JSONResponse(content={"ok": True, "enqueued": True, "dry_run": dry_run})
    result = await run_in_threadpool(_ingest_sarif_body, body, dry_run)
    if result is None:
        raise HTTPException(status_code=400, detail="body must be SARIF JSON")
    if result["created"] and not dry_run:
        get_orchestrator().enqueue_scan(force_retry=False)
    return JSONResponse(content={"ok": True, "dry_run": dry_run, **result})

"""
Read layer: the dashboard and the JSON views it polls. Pure reads from the
store; a future SPA (Phase 6) attaches here without touching ingest.
"""

import logging
from pathlib import Path

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text

from app import store
from app.celery_app import celery_app
from app.db import SessionLocal

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
TASK_VIEW_FIELDS = (
    "issue_number",
    "title",
    "issue_url",
    "session_id",
    "session_url",
    "status",
    "pr_url",
    "created_at",
    "updated_at",
)

router = APIRouter(tags=["read"])
log = logging.getLogger(__name__)


def to_task_view(entry: dict) -> dict:
    return {field: entry[field] for field in TASK_VIEW_FIELDS}


@router.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(content=(TEMPLATES_DIR / "index.html").read_text())


@router.get("/status")
async def status():
    return JSONResponse(content=[to_task_view(entry) for entry in store.get_all()])


@router.get("/status/{issue_number}")
async def status_for_issue(issue_number: int):
    entry = store.get(issue_number)
    if entry is None:
        return JSONResponse(status_code=404, content={"detail": "not tracked"})
    return JSONResponse(content=to_task_view(entry))


def _db_ping() -> None:
    with SessionLocal() as session:
        session.execute(text("SELECT 1"))


def _broker_ping() -> None:
    if celery_app.conf.task_always_eager:
        return
    with celery_app.connection_for_write() as conn:
        conn.ensure_connection(max_retries=1, timeout=2)


@router.get("/healthz")
async def healthz():
    """Liveness: the process is up and can reach its database."""
    await run_in_threadpool(_db_ping)
    return {"ok": True}


@router.get("/readyz")
async def readyz():
    """Readiness: every dependency needed to accept work is reachable."""
    checks: dict[str, str] = {}
    for name, probe in (("database", _db_ping), ("broker", _broker_ping)):
        try:
            await run_in_threadpool(probe)
            checks[name] = "ok"
        except Exception as exc:
            log.warning("readiness probe %s failed: %s", name, exc)
            checks[name] = f"error: {type(exc).__name__}"
    ready = all(value == "ok" for value in checks.values())
    return JSONResponse(
        status_code=200 if ready else 503, content={"ready": ready, "checks": checks}
    )

"""
Read layer: the dashboard and the JSON views it polls. Pure reads from the
store; a future SPA (Phase 6) attaches here without touching ingest.
"""

from pathlib import Path

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text

from app import store
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


@router.get("/healthz")
async def healthz():
    await run_in_threadpool(_db_ping)
    return {"ok": True}

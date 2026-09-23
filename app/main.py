"""
FastAPI entry point for the ingest/API service.

This process never talks to Devin. It serves the read layer (dashboard,
/status, /healthz — see app/api/read.py) and the ingest layer (/scan,
/webhooks/github, /ingest/semgrep — see app/api/ingest.py), and every piece of
work it accepts is handed to the Orchestrator. Actual scanning, discovery,
Devin session creation and status polling run in the worker services
(app/tasks.py), routed by Celery queue:

    ingest queue -> ingest-worker : scan_task, discovery_task
    devin  queue -> devin-worker  : remediate_issue_task, poll_session_task

The periodic reconciliation scan and Semgrep discovery are driven by Celery
Beat (app/celery_app.py).
"""

import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

from app import db
from app.api import ingest_router, read_router
from app.orchestrator import get_orchestrator
from app.remediation import process_issue, scan_and_process  # noqa: F401 (re-exported)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    log.info("Enqueueing startup scan...")
    try:
        get_orchestrator().enqueue_scan(force_retry=False)
    except Exception as exc:
        log.error("Could not enqueue startup scan: %s", exc)
    yield


def configure_cors(api: FastAPI) -> None:
    origins = [
        origin.strip()
        for origin in os.getenv("CORS_ALLOW_ORIGINS", "").split(",")
        if origin.strip()
    ]
    if origins:
        api.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "HEAD", "OPTIONS"],
        )


app = FastAPI(title="Devin Superset Remediation", lifespan=lifespan)
configure_cors(app)
app.include_router(read_router)
app.include_router(ingest_router)

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
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator

load_dotenv()

from app import db, observability  # noqa: E402
from app.api import ingest_router, read_router  # noqa: E402
from app.orchestrator import get_orchestrator  # noqa: E402
from app.remediation import process_issue, scan_and_process  # noqa: E402, F401 (re-exported)

observability.configure_logging()
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    observability.configure_tracing(os.getenv("SERVICE_NAME", "remediation-api"))
    db.init_db()
    log.info("Enqueueing startup scan...")
    try:
        get_orchestrator().enqueue_scan(force_retry=False)
    except Exception as exc:
        log.error("Could not enqueue startup scan: %s", exc)
    yield
    # Graceful shutdown: uvicorn has stopped accepting connections and awaited
    # in-flight requests by the time we get here; flush spans and drop pools.
    log.info("Shutting down: flushing traces and closing DB/broker connections")
    observability.shutdown_tracing()
    db.engine.dispose()
    try:
        from app.celery_app import celery_app

        celery_app.close()
    except Exception as exc:  # pragma: no cover - best effort
        log.warning("Error closing Celery connections: %s", exc)


async def request_id_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    request_id = (
        request.headers.get(observability.REQUEST_ID_HEADER) or observability.new_request_id()
    )
    token = observability.set_request_id(request_id)
    try:
        with observability.span(
            f"{request.method} {request.url.path}",
            **{"http.method": request.method, "http.target": request.url.path},
        ):
            response = await call_next(request)
    finally:
        observability.reset_request_id(token)
    response.headers[observability.REQUEST_ID_HEADER] = request_id
    return response


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
app.middleware("http")(request_id_middleware)
app.include_router(read_router)
app.include_router(ingest_router)
Instrumentator(
    excluded_handlers=["/metrics", "/healthz", "/readyz"],
    should_group_status_codes=False,
).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

"""
Cross-cutting observability for the API and the Celery workers.

- Structured JSON logging (LOG_FORMAT=json|text, LOG_LEVEL) with the current
  request/correlation id attached to every record.
- Correlation ids: the API accepts/generates ``X-Request-ID`` and propagates
  it into every Celery task it publishes (task header ``request_id``); tasks
  that enqueue further tasks forward the same id, so one webhook delivery can
  be followed through scan -> remediate -> poll chain in the logs.
- Prometheus metrics: request metrics come from
  prometheus-fastapi-instrumentator (``/metrics`` on the api service); the
  domain counters below are incremented from app.remediation / app.discovery.
- OpenTelemetry tracing: a TracerProvider with an OTLP/HTTP exporter is only
  installed when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set; otherwise the API
  no-op tracer is used and ``span()`` costs nothing.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from celery import signals
from opentelemetry import trace
from prometheus_client import Counter

from app import db

REQUEST_ID_HEADER = "X-Request-ID"
_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
_configured = False


# --------------------------------------------------------------------------- #
# Correlation ids
# --------------------------------------------------------------------------- #
def new_request_id() -> str:
    return uuid.uuid4().hex


def get_request_id() -> str | None:
    return _request_id.get()


def set_request_id(value: str | None) -> contextvars.Token:
    return _request_id.set(value)


def reset_request_id(token: contextvars.Token) -> None:
    _request_id.reset(token)


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": get_request_id(),
        }
        span = trace.get_current_span().get_span_context()
        if span.is_valid:
            payload["trace_id"] = format(span.trace_id, "032x")
            payload["span_id"] = format(span.span_id, "016x")
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id() or "-"
        return True


def configure_logging() -> None:
    """Install the root handler once per process (api, worker, beat)."""
    global _configured
    if _configured:
        return
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    fmt = os.getenv("LOG_FORMAT", "json").lower()
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RequestIdFilter())
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s")
        )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    _configured = True


# --------------------------------------------------------------------------- #
# Metrics (domain counters; HTTP request metrics come from the instrumentator)
# --------------------------------------------------------------------------- #
SESSIONS_CREATED = Counter(
    "remediation_sessions_created_total", "Devin sessions created for issues."
)
SESSION_CREATE_FAILURES = Counter(
    "remediation_session_create_failures_total", "Devin session creations that raised."
)
POLLS = Counter(
    "remediation_polls_total",
    "Devin session status polls, by outcome.",
    ["outcome"],  # running | completed | failed | error | superseded
)
DISCOVERY_FINDINGS = Counter(
    "discovery_findings_total", "Findings produced by discovery sources.", ["source"]
)
DISCOVERY_ISSUES_CREATED = Counter(
    "discovery_issues_created_total", "Issues filed from discovery findings."
)
REMEDIATION_OUTCOMES = Counter(
    "remediation_outcomes_total",
    "Terminal remediation outcomes.",
    ["status"],  # completed | failed
)
CLAIM_CONFLICTS = Counter(
    "remediation_claim_conflicts_total",
    "process_issue calls that lost the atomic claim to another task.",
)
EXTERNAL_RETRIES = Counter("external_http_retries_total", "Retried outbound HTTP calls.", ["host"])


# --------------------------------------------------------------------------- #
# Tracing
# --------------------------------------------------------------------------- #
def configure_tracing(service_name: str) -> None:
    """Install an OTLP exporter when a collector endpoint is configured.

    Without ``OTEL_EXPORTER_OTLP_ENDPOINT`` the global provider stays the
    no-op ``ProxyTracerProvider`` and spans are free.
    """
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint or isinstance(trace.get_tracer_provider(), _sdk_provider_type()):
        return
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(
        resource=Resource.create({SERVICE_NAME: os.getenv("OTEL_SERVICE_NAME", service_name)})
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)


def _sdk_provider_type() -> type:
    from opentelemetry.sdk.trace import TracerProvider

    return TracerProvider


def shutdown_tracing() -> None:
    provider = trace.get_tracer_provider()
    shutdown = getattr(provider, "shutdown", None)
    if callable(shutdown):
        shutdown()


tracer = trace.get_tracer("devin-remediation-service")


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[trace.Span]:
    with tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value)
        request_id = get_request_id()
        if request_id:
            current.set_attribute("request_id", request_id)
        yield current


# --------------------------------------------------------------------------- #
# Celery wiring: propagate request ids, span every task
# --------------------------------------------------------------------------- #
_task_tokens: dict[str, contextvars.Token] = {}
_task_spans: dict[str, Any] = {}


@signals.before_task_publish.connect
def _inject_request_id(headers: dict | None = None, **_: Any) -> None:
    if headers is not None and get_request_id():
        headers.setdefault("request_id", get_request_id())


@signals.task_prerun.connect
def _task_prerun(task_id: str, task: Any, **_: Any) -> None:
    request = getattr(task, "request", None)
    incoming = getattr(request, "request_id", None) if request else None
    if incoming is None and request is not None:
        incoming = (getattr(request, "headers", None) or {}).get("request_id")
    _task_tokens[task_id] = set_request_id(incoming or get_request_id() or new_request_id())
    ctx = tracer.start_as_current_span(f"celery.task {task.name}")
    current = ctx.__enter__()
    current.set_attribute("celery.task_id", task_id)
    current.set_attribute("request_id", get_request_id() or "")
    _task_spans[task_id] = ctx


@signals.task_postrun.connect
def _task_postrun(task_id: str, **_: Any) -> None:
    ctx = _task_spans.pop(task_id, None)
    if ctx is not None:
        ctx.__exit__(None, None, None)
    token = _task_tokens.pop(task_id, None)
    if token is not None:
        reset_request_id(token)


@signals.setup_logging.connect
def _celery_setup_logging(**_: Any) -> None:
    # Returning from this signal stops Celery from installing its own handlers.
    configure_logging()


@signals.worker_process_init.connect
@signals.beat_init.connect
def _worker_init(**_: Any) -> None:
    configure_logging()
    configure_tracing(os.getenv("SERVICE_NAME", "remediation-worker"))


@signals.worker_process_shutdown.connect
@signals.worker_shutdown.connect
def _worker_shutdown(**_: Any) -> None:
    shutdown_tracing()
    db.engine.dispose()

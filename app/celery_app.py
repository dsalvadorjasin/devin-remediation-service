"""
Celery application configured from the environment.

- CELERY_BROKER_URL / CELERY_RESULT_BACKEND select the Redis broker/backend.
- CELERY_TASK_ALWAYS_EAGER=1 runs tasks synchronously in-process (used by the
  test suite so no broker is needed).
- The Beat schedule holds the periodic reconciliation scan, whose interval
  comes from SCAN_INTERVAL_MINUTES.
"""

import os

from celery import Celery
from dotenv import load_dotenv

load_dotenv()


def _truthy(value: str | None) -> bool:
    return (value or "").lower() in ("1", "true", "yes", "on")


SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_MINUTES", "5")) * 60
SEMGREP_SCAN_INTERVAL_SECONDS = int(os.getenv("SEMGREP_SCAN_INTERVAL_MINUTES", "60")) * 60

celery_app = Celery(
    "remediation",
    broker=os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0"),
    backend=os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/1"),
    include=["app.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_ignore_result=True,
    task_always_eager=_truthy(os.getenv("CELERY_TASK_ALWAYS_EAGER")),
    task_eager_propagates=True,
    task_store_eager_result=False,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    broker_connection_retry_on_startup=True,
    beat_schedule={
        "reconciliation-scan": {
            "task": "app.tasks.scan_task",
            "schedule": SCAN_INTERVAL_SECONDS,
            "kwargs": {"force_retry": False},
        },
        "semgrep-discovery": {
            "task": "app.tasks.discovery_task",
            "schedule": SEMGREP_SCAN_INTERVAL_SECONDS,
            "kwargs": {"dry_run": False},
        },
    },
)

"""
Orchestration boundary.

Routes and clients never talk to Celery directly; they call the `Orchestrator`
returned by `get_orchestrator()`. The concrete implementation is selected by
the ORCHESTRATOR env var (currently only "celery"). A future
`TemporalOrchestrator` implements the same interface without touching routes
or the GitHub/Devin clients.
"""

import os
from functools import lru_cache

from app.orchestrator.base import Orchestrator

__all__ = ["Orchestrator", "get_orchestrator"]


@lru_cache(maxsize=1)
def get_orchestrator() -> Orchestrator:
    kind = os.getenv("ORCHESTRATOR", "celery").lower()
    if kind == "celery":
        from app.orchestrator.celery_orchestrator import CeleryOrchestrator

        return CeleryOrchestrator()
    raise ValueError(f"Unknown ORCHESTRATOR={kind!r} (supported: celery)")

import pytest

from app import github, orchestrator, remediation
from app.celery_app import celery_app
from app.orchestrator.base import Orchestrator
from app.orchestrator.celery_orchestrator import CeleryOrchestrator


def test_get_orchestrator_selects_celery(monkeypatch):
    orchestrator.get_orchestrator.cache_clear()
    monkeypatch.setenv("ORCHESTRATOR", "celery")
    inst = orchestrator.get_orchestrator()
    assert isinstance(inst, CeleryOrchestrator)
    assert isinstance(inst, Orchestrator)


def test_get_orchestrator_rejects_unknown(monkeypatch):
    orchestrator.get_orchestrator.cache_clear()
    monkeypatch.setenv("ORCHESTRATOR", "temporal")
    with pytest.raises(ValueError):
        orchestrator.get_orchestrator()
    orchestrator.get_orchestrator.cache_clear()


def test_enqueue_scan_runs_scan_task_eagerly(monkeypatch):
    assert celery_app.conf.task_always_eager is True
    monkeypatch.setattr(github, "get_labeled_issues", lambda: [])
    seen = []
    monkeypatch.setattr(
        remediation,
        "scan_and_process",
        lambda force_retry=False: seen.append(force_retry) or {"scanned": 0},
    )

    CeleryOrchestrator().enqueue_scan(force_retry=True)

    assert seen == [True]


def test_enqueue_remediation_runs_process_issue(monkeypatch):
    issue = {"number": 9, "title": "T", "body": "", "html_url": "u"}
    seen = []
    monkeypatch.setattr(
        remediation,
        "process_issue",
        lambda i, force_retry=False: seen.append((i["number"], force_retry)),
    )

    CeleryOrchestrator().enqueue_remediation(issue)

    assert seen == [(9, False)]


def test_beat_schedule_uses_scan_interval():
    entry = celery_app.conf.beat_schedule["reconciliation-scan"]
    assert entry["task"] == "app.tasks.scan_task"
    assert entry["schedule"] == 5 * 60

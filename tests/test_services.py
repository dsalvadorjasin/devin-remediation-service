"""Phase 5: service split — read/ingest routers and queue routing."""

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from app import main, store
from app.celery_app import celery_app

ROOT = Path(__file__).resolve().parents[1]


def test_healthz_checks_database():
    client = TestClient(main.app)
    assert client.get("/healthz").json() == {"ok": True}


def test_status_for_single_issue():
    store.upsert(3, title="t", issue_url="u", status="running")
    client = TestClient(main.app)
    assert client.get("/status/3").json()["status"] == "running"
    assert client.get("/status/4").status_code == 404


def test_routes_split_between_read_and_ingest_routers():
    tags = {r.path: set(r.tags) for r in main.app.routes if hasattr(r, "tags")}
    assert tags["/"] == tags["/status"] == tags["/healthz"] == {"read"}
    assert tags["/scan"] == tags["/webhooks/github"] == tags["/ingest/semgrep"] == {"ingest"}


def test_devin_tasks_routed_to_devin_queue():
    routes = celery_app.conf.task_routes
    assert routes["app.tasks.remediate_issue_task"]["queue"] == "devin"
    assert routes["app.tasks.poll_session_task"]["queue"] == "devin"
    assert routes["app.tasks.scan_task"]["queue"] == "ingest"
    assert routes["app.tasks.discovery_task"]["queue"] == "ingest"


def test_compose_and_k8s_define_all_services():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert {"api", "ingest-worker", "devin-worker", "beat", "redis", "postgres", "migrate"} <= set(compose["services"])
    assert "-Q devin" in compose["services"]["devin-worker"]["command"]
    assert "-Q ingest" in compose["services"]["ingest-worker"]["command"]

    kust = yaml.safe_load((ROOT / "k8s" / "kustomization.yaml").read_text())
    names = set()
    for res in kust["resources"]:
        for doc in yaml.safe_load_all((ROOT / "k8s" / res).read_text()):
            if doc and doc["kind"] in ("Deployment", "StatefulSet", "Job"):
                names.add(doc["metadata"]["name"])
    assert {
        "remediation-api",
        "remediation-ingest-worker",
        "remediation-devin-worker",
        "remediation-beat",
        "remediation-migrate",
        "redis",
        "postgres",
    } <= names

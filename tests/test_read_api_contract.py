from pathlib import Path

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import main, store

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contracts" / "read-api.openapi.yaml"


def test_read_endpoints_match_frozen_contract():
    contract = yaml.safe_load(CONTRACT_PATH.read_text())
    schemas = contract["components"]["schemas"]
    required = set(schemas["TaskView"]["required"])
    statuses = set(schemas["TaskStatus"]["enum"])

    for issue_number, status in enumerate(sorted(statuses), start=1):
        store.upsert(
            issue_number,
            title=f"Issue {issue_number}",
            issue_url=f"https://example.com/issues/{issue_number}",
            status=status,
        )

    client = TestClient(main.app)
    response = client.get("/status")

    assert response.status_code == 200
    tasks = response.json()
    assert [task["issue_number"] for task in tasks] == [1, 2, 3]
    assert {task["status"] for task in tasks} == statuses
    for task in tasks:
        assert set(task) == required
        assert task["status"] in statuses
        single = client.get(f"/status/{task['issue_number']}")
        assert single.status_code == 200
        assert single.json() == task
        assert set(single.json()) == required
        assert single.json()["status"] in statuses


def test_cors_allows_configured_origins_for_read_only_methods(monkeypatch):
    monkeypatch.setenv(
        "CORS_ALLOW_ORIGINS", " https://dashboard.example.com, http://localhost:5173 , "
    )
    api = FastAPI()
    main.configure_cors(api)
    api.include_router(main.read_router)
    client = TestClient(api)

    for origin in ("https://dashboard.example.com", "http://localhost:5173"):
        response = client.get("/status", headers={"Origin": origin})
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin

        preflight = client.options(
            "/status",
            headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == origin
        assert {"GET", "HEAD", "OPTIONS"} <= set(
            preflight.headers["access-control-allow-methods"].split(", ")
        )

    denied = client.options(
        "/status",
        headers={
            "Origin": "https://dashboard.example.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert denied.status_code == 400
    assert "https://untrusted.example.com" not in client.get(
        "/status", headers={"Origin": "https://untrusted.example.com"}
    ).headers.get("access-control-allow-origin", "")


def test_cors_is_disabled_when_origins_are_unset(monkeypatch):
    monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
    api = FastAPI()
    main.configure_cors(api)
    api.include_router(main.read_router)

    response = TestClient(api).get(
        "/status", headers={"Origin": "https://dashboard.example.com"}
    )

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers

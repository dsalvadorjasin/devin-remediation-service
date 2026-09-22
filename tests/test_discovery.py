import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import github, main
from app.discovery import FINGERPRINT_MARKER, Finding, SemgrepDiscoverySource, ingest_findings, parse_sarif
from app.orchestrator.celery_orchestrator import CeleryOrchestrator

FIXTURE = Path(__file__).parent / "fixtures" / "semgrep.sarif"
ISSUES_URL = "https://api.github.com/repos/test-org/test-repo/issues"
TOKEN = "test-ingest-token"


@pytest.fixture
def sarif() -> dict:
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def ingest_token(monkeypatch):
    monkeypatch.setenv("INGEST_TOKEN", TOKEN)


@pytest.fixture
def orchestrator_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(CeleryOrchestrator, "enqueue_scan", lambda self, force_retry=False: calls.append(("scan", force_retry)))
    monkeypatch.setattr(CeleryOrchestrator, "enqueue_discovery", lambda self, dry_run=False: calls.append(("discovery", dry_run)))
    return calls


# --- SARIF -> Finding -------------------------------------------------------


def test_parse_sarif_normalizes_findings(sarif):
    findings = parse_sarif(sarif)
    assert len(findings) == 2
    f = findings[0]
    assert f.rule_id.endswith("dangerous-subprocess-use")
    assert f.file_path == "superset/utils/shell.py"
    assert f.start_line == 42
    assert f.snippet == "    subprocess.run(cmd, shell=True)"
    assert f.severity == "HIGH"
    assert "shell=True" in f.message
    assert f.help_uri.startswith("https://semgrep.dev/r/")
    # SARIF-provided fingerprint preferred, truncated to 32 chars
    assert f.fingerprint == "abcdef0123456789abcdef0123456789"

    g = findings[1]
    assert g.severity == "MEDIUM"
    assert g.fingerprint == g.compute_fingerprint()
    assert len(g.fingerprint) == 32


def test_finding_title_and_body_contain_marker():
    f = Finding(rule_id="r.x", message="msg", severity="LOW", file_path="a/b.py", start_line=3, snippet="x = 1")
    assert f.title == "[semgrep] r.x: a/b.py:3"
    body = f.issue_body()
    assert f"<!-- {FINGERPRINT_MARKER}: {f.fingerprint} -->" in body
    assert "`a/b.py:3`" in body and "x = 1" in body and "LOW" in body


def test_fingerprint_stable_under_small_line_shift_and_whitespace():
    a = Finding(rule_id="r", message="m", severity="S", file_path="f.py", start_line=41, snippet="foo( x )")
    b = Finding(rule_id="r", message="different message", severity="S", file_path="f.py", start_line=43, snippet="foo(  x )")
    c = Finding(rule_id="r", message="m", severity="S", file_path="f.py", start_line=141, snippet="foo(x)")
    d = Finding(rule_id="other", message="m", severity="S", file_path="f.py", start_line=41, snippet="foo(x)")
    assert a.fingerprint == b.fingerprint
    assert a.fingerprint != c.fingerprint
    assert a.fingerprint != d.fingerprint


def test_discover_strips_checkout_prefix(monkeypatch, sarif, tmp_path):
    src = SemgrepDiscoverySource(repo="o/r", checkout_dir=str(tmp_path), token="")
    monkeypatch.setattr(src, "update_checkout", lambda: tmp_path)
    for res in sarif["runs"][0]["results"]:
        res["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] = f"{tmp_path}/" + res["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
    monkeypatch.setattr(src, "run_semgrep", lambda target: sarif)
    findings = src.discover()
    assert len(findings) == 2
    assert findings[0].file_path == "superset/utils/shell.py"


# --- GitHub issue creation + dedup -----------------------------------------


def test_create_issue_posts_with_label(httpx_mock):
    httpx_mock.add_response(method="POST", url=ISSUES_URL, json={"number": 5, "html_url": "https://gh/o/r/issues/5"})
    issue = github.create_issue("t", "b")
    assert issue["number"] == 5
    sent = json.loads(httpx_mock.get_requests()[0].content)
    assert sent == {"title": "t", "body": "b", "labels": [github.LABEL]}


def test_find_issues_by_fingerprint(httpx_mock):
    httpx_mock.add_response(
        method="GET",
        url=f"{ISSUES_URL}?labels={github.LABEL}&state=all&per_page=100&page=1",
        json=[
            {"number": 1, "body": f"x <!-- {FINGERPRINT_MARKER}: aaa -->"},
            {"number": 2, "body": "no marker"},
            {"number": 3, "body": f"<!-- {FINGERPRINT_MARKER}: ccc -->", "pull_request": {}},
        ],
    )
    found = github.find_issues_by_fingerprint(["aaa", "bbb", "ccc"])
    assert set(found) == {"aaa"}
    assert found["aaa"]["number"] == 1


def test_ingest_findings_dedups_existing_and_duplicates(monkeypatch, sarif):
    findings = parse_sarif(sarif) + parse_sarif(sarif)  # same two findings twice
    monkeypatch.setattr(github, "find_issues_by_fingerprint", lambda fps: {findings[0].fingerprint: {"number": 1}})
    created = []

    def fake_create(title, body, labels=None):
        created.append(title)
        return {"number": 100 + len(created), "html_url": "u"}

    monkeypatch.setattr(github, "create_issue", fake_create)
    result = ingest_findings(findings)
    assert result["findings"] == 4
    assert result["skipped"] == 3
    assert [c["number"] for c in result["created"]] == [101]
    assert created == [findings[1].title]


def test_ingest_findings_cap_applies_after_dedup(monkeypatch, sarif):
    """SEMGREP_MAX_FINDINGS caps *new* issues per run; already-filed findings
    must not eat the budget, otherwise later findings are never reached."""
    findings = parse_sarif(sarif)
    monkeypatch.setattr(github, "find_issues_by_fingerprint", lambda fps: {findings[0].fingerprint: {"number": 1}})
    created = []
    monkeypatch.setattr(github, "create_issue", lambda t, b, labels=None: created.append(t) or {"number": 1, "html_url": "u"})
    monkeypatch.setenv("SEMGREP_MAX_FINDINGS", "1")
    result = ingest_findings(findings)
    assert created == [findings[1].title]
    assert result["skipped"] == 1
    assert len(ingest_findings(findings, max_new=0)["created"]) == 0


def test_issue_body_neutralises_untrusted_markdown():
    f = Finding(
        rule_id="r`x",
        message="hi @octocat see ```",
        severity="HIGH",
        file_path="a/b.py",
        start_line=3,
        snippet="```\n@everyone pwned\n```",
    )
    body = f.issue_body()
    assert "`` r`x ``" in body
    assert "@octocat" not in body and "@\u200boctocat" in body
    assert "@everyone" in body  # inside the code block, so inert
    assert body.count("````") == 2  # snippet fence is longer than any run inside it
    assert body.rstrip().endswith(f.marker)


def test_ingest_endpoint_rejects_oversized_body(ingest_token, monkeypatch):
    from app import discovery

    monkeypatch.setattr(discovery.base, "MAX_SARIF_BYTES", 10)
    monkeypatch.setattr(main, "MAX_SARIF_BYTES", 10)
    client = TestClient(main.app)
    resp = client.post("/ingest/semgrep", content=b"{" + b" " * 20 + b"}", headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 413
    resp = client.post(
        "/ingest/semgrep", content=b"{}", headers={"Authorization": f"Bearer {TOKEN}", "Content-Length": "2"}
    )
    assert resp.status_code != 413


# --- POST /ingest/semgrep ---------------------------------------------------


def test_ingest_endpoint_requires_token(ingest_token):
    client = TestClient(main.app)
    assert client.post("/ingest/semgrep", content=b"{}").status_code == 401
    assert client.post("/ingest/semgrep", content=b"{}", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_ingest_endpoint_503_without_token_configured(monkeypatch):
    monkeypatch.delenv("INGEST_TOKEN", raising=False)
    client = TestClient(main.app)
    assert client.post("/ingest/semgrep", content=b"{}").status_code == 503


def test_ingest_endpoint_accepts_sarif_and_creates_issues(ingest_token, orchestrator_calls, httpx_mock, sarif):
    httpx_mock.add_response(method="GET", url=f"{ISSUES_URL}?labels={github.LABEL}&state=all&per_page=100&page=1", json=[])
    httpx_mock.add_response(method="POST", url=ISSUES_URL, json={"number": 11, "html_url": "u11"})
    httpx_mock.add_response(method="POST", url=ISSUES_URL, json={"number": 12, "html_url": "u12"})
    client = TestClient(main.app)

    resp = client.post("/ingest/semgrep", json=sarif, headers={"Authorization": f"Bearer {TOKEN}"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["findings"] == 2 and data["skipped"] == 0
    assert [c["number"] for c in data["created"]] == [11, 12]
    posted = [json.loads(r.content) for r in httpx_mock.get_requests() if r.method == "POST"]
    assert posted[0]["title"].startswith("[semgrep] python.lang.security.audit.dangerous-subprocess-use")
    assert posted[0]["labels"] == [github.LABEL]
    assert FINGERPRINT_MARKER in posted[0]["body"]
    assert orchestrator_calls == [("scan", False)]


def test_ingest_endpoint_dry_run_creates_nothing(ingest_token, orchestrator_calls, httpx_mock, sarif):
    httpx_mock.add_response(method="GET", url=f"{ISSUES_URL}?labels={github.LABEL}&state=all&per_page=100&page=1", json=[])
    client = TestClient(main.app)
    resp = client.post("/ingest/semgrep?dry_run=true", json=sarif, headers={"X-Ingest-Token": TOKEN})
    assert resp.status_code == 200
    assert len(resp.json()["created"]) == 2
    assert all(r.method == "GET" for r in httpx_mock.get_requests())
    assert orchestrator_calls == []


def test_ingest_endpoint_empty_body_enqueues_discovery(ingest_token, orchestrator_calls):
    client = TestClient(main.app)
    resp = client.post("/ingest/semgrep", headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "enqueued": True, "dry_run": False}
    assert orchestrator_calls == [("discovery", False)]


def test_ingest_endpoint_rejects_non_json(ingest_token):
    client = TestClient(main.app)
    resp = client.post("/ingest/semgrep", content=b"not json", headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 400


def test_discovery_task_enqueues_scan_when_issues_created(monkeypatch):
    from app import tasks
    from app.discovery import semgrep as semgrep_mod

    monkeypatch.setattr(semgrep_mod.SemgrepDiscoverySource, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(semgrep_mod.SemgrepDiscoverySource, "discover", lambda self: [])
    monkeypatch.setattr("app.discovery.ingest_findings", lambda findings, dry_run=False: {"created": [{"number": 1}], "skipped": []})
    calls = []
    monkeypatch.setattr(CeleryOrchestrator, "enqueue_scan", lambda self, force_retry=False: calls.append(force_retry))
    assert tasks.discovery_task()["created"]
    assert calls == [False]


def test_scan_env_drops_service_credentials(monkeypatch):
    for k in ("GITHUB_TOKEN", "DEVIN_API_KEY", "DATABASE_URL", "CELERY_BROKER_URL", "INGEST_TOKEN", "GITHUB_WEBHOOK_SECRET"):
        monkeypatch.setenv(k, "secret")
    monkeypatch.setenv("SEMGREP_CONFIG", "p/ci")
    monkeypatch.setenv("PATH", "/usr/bin")

    env = SemgrepDiscoverySource.scan_env()

    assert env["SEMGREP_CONFIG"] == "p/ci"
    assert env["PATH"] == "/usr/bin"
    assert not {k for k in env if k in ("GITHUB_TOKEN", "DEVIN_API_KEY", "DATABASE_URL", "CELERY_BROKER_URL", "INGEST_TOKEN", "GITHUB_WEBHOOK_SECRET")}


def test_scan_env_keeps_proxy_and_ca_settings(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:8080")
    monkeypatch.setenv("no_proxy", "localhost")
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/corp.pem")
    monkeypatch.setenv("GITHUB_TOKEN", "secret")

    env = SemgrepDiscoverySource.scan_env()

    assert env["HTTPS_PROXY"] == "http://proxy.internal:8080"
    assert env["no_proxy"] == "localhost"
    assert env["SSL_CERT_FILE"] == "/etc/ssl/corp.pem"
    assert "GITHUB_TOKEN" not in env

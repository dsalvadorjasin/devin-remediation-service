import httpx
import pytest

from app import devin, github, http_client

URL = "https://api.example.com/thing"


def _sleeps() -> tuple[list[float], object]:
    calls: list[float] = []
    return calls, calls.append


def test_client_applies_connect_and_read_timeouts(monkeypatch):
    monkeypatch.setenv("HTTP_CONNECT_TIMEOUT", "2.5")
    monkeypatch.setenv("HTTP_READ_TIMEOUT", "12")
    with http_client.client() as c:
        assert c.timeout.connect == 2.5
        assert c.timeout.read == 12
        assert c.timeout.pool == 2.5


def test_get_retries_timeouts_then_succeeds(httpx_mock):
    httpx_mock.add_exception(httpx.ReadTimeout("slow"))
    httpx_mock.add_exception(httpx.ConnectError("refused"))
    httpx_mock.add_response(method="GET", url=URL, json={"ok": True})
    sleeps, sleep = _sleeps()

    with http_client.client() as c:
        resp = http_client.request(c, "GET", URL, sleep=sleep, max_attempts=4)

    assert resp.json() == {"ok": True}
    assert len(httpx_mock.get_requests()) == 3
    assert len(sleeps) == 2


def test_get_retries_5xx_then_gives_up_with_status_error(httpx_mock):
    httpx_mock.add_response(method="GET", url=URL, status_code=503)
    sleeps, sleep = _sleeps()

    with http_client.client() as c, pytest.raises(httpx.HTTPStatusError):
        http_client.request(c, "GET", URL, sleep=sleep, max_attempts=3)

    assert len(httpx_mock.get_requests()) == 3
    assert len(sleeps) == 2


def test_get_reraises_transport_error_after_last_attempt(httpx_mock):
    httpx_mock.add_exception(httpx.ReadTimeout("slow"))
    sleeps, sleep = _sleeps()

    with http_client.client() as c, pytest.raises(httpx.ReadTimeout):
        http_client.request(c, "GET", URL, sleep=sleep, max_attempts=2)

    assert len(httpx_mock.get_requests()) == 2
    assert len(sleeps) == 1


def test_429_honours_retry_after_header(httpx_mock, monkeypatch):
    monkeypatch.setenv("HTTP_BACKOFF_CAP_SECONDS", "60")
    httpx_mock.add_response(method="GET", url=URL, status_code=429, headers={"Retry-After": "7"})
    httpx_mock.add_response(method="GET", url=URL, json={"ok": True})
    sleeps, sleep = _sleeps()

    with http_client.client() as c:
        http_client.request(c, "GET", URL, sleep=sleep)

    assert sleeps == [7.0]


def test_retry_after_is_capped(httpx_mock, monkeypatch):
    monkeypatch.setenv("HTTP_BACKOFF_CAP_SECONDS", "3")
    httpx_mock.add_response(method="GET", url=URL, status_code=429, headers={"Retry-After": "600"})
    httpx_mock.add_response(method="GET", url=URL, json={"ok": True})
    sleeps, sleep = _sleeps()

    with http_client.client() as c:
        http_client.request(c, "GET", URL, sleep=sleep)

    assert sleeps == [3.0]


def test_post_is_not_retried_on_5xx_or_timeout(httpx_mock):
    httpx_mock.add_response(method="POST", url=URL, status_code=502)
    with http_client.client() as c, pytest.raises(httpx.HTTPStatusError):
        http_client.request(c, "POST", URL, json={})
    assert len(httpx_mock.get_requests()) == 1

    httpx_mock.reset(assert_all_responses_were_requested=False)
    httpx_mock.add_exception(httpx.ReadTimeout("slow"))
    with http_client.client() as c, pytest.raises(httpx.ReadTimeout):
        http_client.request(c, "POST", URL, json={})
    assert len(httpx_mock.get_requests()) == 1


def test_post_is_retried_on_429_only(httpx_mock):
    httpx_mock.add_response(method="POST", url=URL, status_code=429)
    httpx_mock.add_response(method="POST", url=URL, status_code=201, json={"id": 1})
    sleeps, sleep = _sleeps()

    with http_client.client() as c:
        resp = http_client.request(c, "POST", URL, json={}, sleep=sleep)

    assert resp.status_code == 201
    assert len(httpx_mock.get_requests()) == 2


def test_post_opted_in_as_idempotent_retries_5xx(httpx_mock):
    httpx_mock.add_response(method="POST", url=URL, status_code=500)
    httpx_mock.add_response(method="POST", url=URL, json={"id": 1})
    with http_client.client() as c:
        resp = http_client.request(c, "POST", URL, json={}, idempotent=True, sleep=lambda s: None)
    assert resp.json() == {"id": 1}


def test_4xx_is_not_retried(httpx_mock):
    httpx_mock.add_response(method="GET", url=URL, status_code=404)
    with http_client.client() as c, pytest.raises(httpx.HTTPStatusError):
        http_client.request(c, "GET", URL, sleep=lambda s: None)
    assert len(httpx_mock.get_requests()) == 1


def test_backoff_is_exponential_with_full_jitter_and_capped():
    assert http_client.backoff_seconds(0, 0.5, 20, rand=lambda: 1.0) == 0.5
    assert http_client.backoff_seconds(3, 0.5, 20, rand=lambda: 1.0) == 4.0
    assert http_client.backoff_seconds(10, 0.5, 20, rand=lambda: 1.0) == 20
    assert http_client.backoff_seconds(3, 0.5, 20, rand=lambda: 0.25) == 1.0


def test_retry_after_parses_http_date():
    resp = httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
    assert http_client.retry_after_seconds(resp) == 0.0
    resp = httpx.Response(429, headers={"Retry-After": "garbage"})
    assert http_client.retry_after_seconds(resp) is None
    assert http_client.retry_after_seconds(httpx.Response(429)) is None


def test_github_get_labeled_issues_survives_transient_5xx(httpx_mock):
    url = "https://api.github.com/repos/test-org/test-repo/issues?labels=devin-remediate&state=open&per_page=100&page=1"
    httpx_mock.add_response(method="GET", url=url, status_code=502)
    httpx_mock.add_response(method="GET", url=url, json=[{"number": 1, "title": "x"}])

    assert [i["number"] for i in github.get_labeled_issues()] == [1]
    assert len(httpx_mock.get_requests()) == 2


def test_devin_get_session_retries_but_create_session_does_not(httpx_mock):
    base = "https://api.devin.ai/v3/organizations/test-org-id/sessions"
    httpx_mock.add_exception(httpx.ReadTimeout("slow"), method="GET", url=f"{base}/s1")
    httpx_mock.add_response(method="GET", url=f"{base}/s1", json={"status": "running"})
    assert devin.get_session("s1") == {"status": "running"}

    httpx_mock.add_exception(httpx.ReadTimeout("slow"), method="POST", url=base)
    with pytest.raises(httpx.ReadTimeout):
        devin.create_session(1, "t", "b")
    assert len([r for r in httpx_mock.get_requests() if r.method == "POST"]) == 1


def test_clients_send_requests_with_timeouts_configured(httpx_mock):
    url = "https://api.github.com/repos/test-org/test-repo/issues/1"
    httpx_mock.add_response(method="GET", url=url, json={"number": 1})
    github.get_issue(1)
    request = httpx_mock.get_requests()[0]
    assert request.extensions["timeout"]["connect"] == 5.0
    assert request.extensions["timeout"]["read"] == 30.0

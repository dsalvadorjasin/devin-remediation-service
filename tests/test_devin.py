import json

import httpx
import pytest

from app.devin import create_session, extract_pr_url, get_session, map_devin_status


@pytest.mark.parametrize(
    ("devin_status", "status_detail", "expected"),
    [
        (None, None, "running"),
        ("exit", None, "completed"),
        ("EXIT", None, "completed"),
        ("error", None, "failed"),
        ("suspended", None, "failed"),
        ("running", "finished", "completed"),
        ("running", None, "running"),
        ("new", None, "running"),
        ("claimed", None, "running"),
        ("resuming", None, "running"),
        ("unknown", None, "running"),
    ],
)
def test_map_devin_status(devin_status, status_detail, expected):
    assert map_devin_status(devin_status, status_detail) == expected


@pytest.mark.parametrize(
    ("session_data", "expected"),
    [
        ({}, None),
        ({"pull_requests": []}, None),
        ({"pull_requests": None}, None),
        ({"pull_requests": [{"pr_url": "https://example.com/pr/1"}]}, "https://example.com/pr/1"),
        (
            {
                "pull_requests": [
                    {"pr_url": "https://example.com/pr/1"},
                    {"pr_url": "https://example.com/pr/2"},
                ]
            },
            "https://example.com/pr/1",
        ),
    ],
)
def test_extract_pr_url(session_data, expected):
    assert extract_pr_url(session_data) == expected


def test_create_session_posts_prompt_and_returns_json(httpx_mock):
    url = "https://api.devin.ai/v3/organizations/test-org-id/sessions"
    httpx_mock.add_response(
        method="POST",
        url=url,
        json={"session_id": "sess-1", "url": "https://example.com/sessions/1"},
    )

    result = create_session(7, "Fix bug", "Issue body")

    assert result == {"session_id": "sess-1", "url": "https://example.com/sessions/1"}
    request = httpx_mock.get_requests()[0]
    assert request.url == url
    assert request.headers["authorization"] == "Bearer test-devin-key"
    payload = json.loads(request.content.decode())
    assert payload["prompt"] == (
        "You are working on the GitHub repository test-org/test-repo.\n\n"
        'Please fix issue #7: "Fix bug".\n\n'
        "Issue description:\nIssue body\n\n"
        "Instructions:\n"
        "- Clone the repository and fix the issue described above.\n"
        "- Open a pull request with your fix.\n"
        '- The PR title should reference the issue, e.g. "Fix #7: Fix bug".\n'
        '- The PR body must include the line "Closes #7" so that GitHub automatically closes the issue when the PR is merged.\n'
        "- Make sure all existing tests pass and all pre-commit hooks pass.\n"
    )


def test_create_session_uses_fallback_body_when_empty(httpx_mock):
    url = "https://api.devin.ai/v3/organizations/test-org-id/sessions"
    httpx_mock.add_response(method="POST", url=url, json={"session_id": "sess-2"})

    create_session(8, "Fix bug", "")

    request = httpx_mock.get_requests()[0]
    payload = json.loads(request.content.decode())
    assert "No additional description provided." in payload["prompt"]


def test_get_session_gets_session_json(httpx_mock):
    url = "https://api.devin.ai/v3/organizations/test-org-id/sessions/sess-3"
    httpx_mock.add_response(method="GET", url=url, json={"status": "running"})

    result = get_session("sess-3")

    assert result == {"status": "running"}
    request = httpx_mock.get_requests()[0]
    assert request.url == url
    assert request.headers["authorization"] == "Bearer test-devin-key"


def test_get_session_raises_for_status_on_http_error(httpx_mock):
    url = "https://api.devin.ai/v3/organizations/test-org-id/sessions/sess-4"
    httpx_mock.add_response(method="GET", url=url, status_code=500)

    with pytest.raises(httpx.HTTPStatusError):
        get_session("sess-4")

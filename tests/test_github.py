import json

from app.github import find_existing_pr, get_labeled_issues, post_comment


def test_get_labeled_issues_filters_out_pull_requests(httpx_mock):
    url = "https://api.github.com/repos/test-org/test-repo/issues?labels=devin-remediate&state=open&per_page=100&page=1"
    httpx_mock.add_response(
        method="GET",
        url=url,
        json=[
            {"number": 1, "title": "issue 1"},
            {"number": 2, "title": "issue 2", "pull_request": {}},
            {"number": 3, "title": "issue 3"},
        ],
    )

    issues = get_labeled_issues()

    assert [issue["number"] for issue in issues] == [1, 3]
    request = httpx_mock.get_requests()[0]
    assert str(request.url) == url
    assert request.headers["authorization"] == "Bearer test-token"
    assert request.headers["accept"] == "application/vnd.github+json"
    assert request.headers["x-github-api-version"] == "2022-11-28"


def test_find_existing_pr_returns_cross_referenced_open_pr(httpx_mock):
    timeline_url = "https://api.github.com/repos/test-org/test-repo/issues/42/timeline?per_page=100"
    httpx_mock.add_response(
        method="GET",
        url=timeline_url,
        json=[
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "pull_request": {"url": "https://example.com/pr/42"},
                        "state": "open",
                        "html_url": "https://example.com/pr/42",
                    }
                },
            }
        ],
    )

    assert find_existing_pr(42) == "https://example.com/pr/42"
    assert len(httpx_mock.get_requests()) == 1
    assert str(httpx_mock.get_requests()[0].url) == timeline_url


def test_find_existing_pr_ignores_closed_cross_referenced_and_falls_back_to_open_pr_scan(
    httpx_mock,
):
    timeline_url = "https://api.github.com/repos/test-org/test-repo/issues/42/timeline?per_page=100"
    pulls_url = "https://api.github.com/repos/test-org/test-repo/pulls?state=open&per_page=100"
    httpx_mock.add_response(
        method="GET",
        url=timeline_url,
        json=[
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "pull_request": {"url": "https://example.com/pr/closed"},
                        "state": "closed",
                        "html_url": "https://example.com/pr/closed",
                    }
                },
            }
        ],
    )
    httpx_mock.add_response(
        method="GET",
        url=pulls_url,
        json=[
            {"title": "Fix #42: important bug", "html_url": "https://example.com/pr/42"},
            {"title": "Unrelated", "body": "No match here", "html_url": "https://example.com/pr/99"},
        ],
    )

    assert find_existing_pr(42) == "https://example.com/pr/42"
    assert len(httpx_mock.get_requests()) == 2
    assert str(httpx_mock.get_requests()[0].url) == timeline_url
    assert str(httpx_mock.get_requests()[1].url) == pulls_url


def test_find_existing_pr_returns_none_when_no_match(httpx_mock):
    timeline_url = "https://api.github.com/repos/test-org/test-repo/issues/42/timeline?per_page=100"
    pulls_url = "https://api.github.com/repos/test-org/test-repo/pulls?state=open&per_page=100"
    httpx_mock.add_response(method="GET", url=timeline_url, json=[])
    httpx_mock.add_response(
        method="GET",
        url=pulls_url,
        json=[
            {"title": "Unrelated", "body": "No issue reference", "html_url": "https://example.com/pr/1"}
        ],
    )

    assert find_existing_pr(42) is None


def test_post_comment_sends_comment_body_and_headers(httpx_mock):
    url = "https://api.github.com/repos/test-org/test-repo/issues/7/comments"
    httpx_mock.add_response(method="POST", url=url, json={"ok": True})

    post_comment(7, "hello")

    request = httpx_mock.get_requests()[0]
    assert request.url.path == "/repos/test-org/test-repo/issues/7/comments"
    assert request.headers["authorization"] == "Bearer test-token"
    assert request.headers["accept"] == "application/vnd.github+json"
    assert request.headers["x-github-api-version"] == "2022-11-28"
    assert json.loads(request.content.decode()) == {"body": "hello"}

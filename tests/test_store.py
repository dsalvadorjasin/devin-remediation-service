from app import store


def test_upsert_inserts_with_defaults_and_updates_existing_entry():
    created = store.upsert(
        7,
        title="Fix bug",
        issue_url="https://example.com/issues/7",
        session_id="sess-1",
        session_url="https://example.com/sessions/1",
        pr_url="https://example.com/pr/1",
        status="completed",
        ignored_field="ignored",
    )

    assert created["issue_number"] == 7
    assert created["title"] == "Fix bug"
    assert created["issue_url"] == "https://example.com/issues/7"
    assert created["session_id"] == "sess-1"
    assert created["session_url"] == "https://example.com/sessions/1"
    assert created["pr_url"] == "https://example.com/pr/1"
    assert created["status"] == "completed"
    assert created["created_at"]
    assert created["updated_at"]
    assert "ignored_field" not in created

    updated = store.upsert(
        7,
        title=None,
        issue_url=None,
        session_id="sess-2",
        session_url=None,
        pr_url=None,
        status=None,
        ignored_field="still ignored",
    )

    assert updated["issue_number"] == 7
    assert updated["title"] == "Fix bug"
    assert updated["issue_url"] == "https://example.com/issues/7"
    assert updated["session_id"] == "sess-2"
    assert updated["session_url"] == "https://example.com/sessions/1"
    assert updated["pr_url"] == "https://example.com/pr/1"
    assert updated["status"] == "completed"
    assert updated["created_at"] == created["created_at"]
    assert updated["updated_at"] != created["updated_at"]
    assert "ignored_field" not in updated


def test_get_returns_copy_and_none_for_missing():
    store.upsert(1, title="A", issue_url="https://example.com/issues/1")

    entry = store.get(1)

    assert entry == store.get(1)
    assert store.get(999) is None

    entry["title"] = "mutated"
    assert store.get(1)["title"] == "A"


def test_get_all_returns_entries_sorted_by_issue_number():
    store.upsert(20, title="B", issue_url="https://example.com/issues/20")
    store.upsert(3, title="A", issue_url="https://example.com/issues/3")
    store.upsert(11, title="C", issue_url="https://example.com/issues/11")

    assert [entry["issue_number"] for entry in store.get_all()] == [3, 11, 20]


def test_get_status_returns_current_status_or_none():
    assert store.get_status(1) is None

    store.upsert(1, title="A", issue_url="https://example.com/issues/1")
    assert store.get_status(1) == "running"

    store.upsert(1, status="failed")
    assert store.get_status(1) == "failed"


def test_clear_empties_the_store():
    store.upsert(1, title="A", issue_url="https://example.com/issues/1")
    store.upsert(2, title="B", issue_url="https://example.com/issues/2")

    store.clear()

    assert store.get_all() == []


def test_repository_switch_does_not_reuse_task_state(monkeypatch):
    store.upsert(1, title="A", issue_url="https://example.com/issues/1", status="failed")

    monkeypatch.setenv("GITHUB_REPO", "other-org/other-repo")

    assert store.get_status(1) is None
    other = store.upsert(1, title="B", issue_url="https://example.com/issues/1")
    assert other["title"] == "B"
    assert other["status"] == "running"
    store.clear()

    monkeypatch.setenv("GITHUB_REPO", "test-org/test-repo")
    assert store.get_status(1) == "failed"

"""
Persistent store for tracking GitHub issues, their associated Devin sessions,
and processing statuses, backed by SQLAlchemy (PostgreSQL in production,
SQLite fallback for tests/local runs). The public function signatures are
unchanged from the original in-memory implementation.
"""

from sqlalchemy import select

from app.db import SessionLocal, Task, utcnow

# Schema per store entry (see app.db.Task.to_dict):
# {
#   "issue_number": int,
#   "title": str,
#   "issue_url": str,
#   "session_id": str | None,
#   "session_url": str | None,
#   "status": "running" | "completed" | "failed",
#   "pr_url": str | None,
#   "created_at": str (ISO),
#   "updated_at": str (ISO),
# }

_MUTABLE_FIELDS = {"title", "issue_url", "session_id", "session_url", "status", "pr_url"}


def upsert(issue_number: int, **kwargs) -> dict:
    """Insert a new entry or update an existing one for the given issue number."""
    with SessionLocal() as session:
        task = session.get(Task, issue_number)
        if task is None:
            task = Task(
                issue_number=issue_number,
                title=kwargs.get("title") or "",
                issue_url=kwargs.get("issue_url") or "",
                status="running",
            )
            session.add(task)
        for k, v in kwargs.items():
            if k in _MUTABLE_FIELDS and v is not None:
                setattr(task, k, v)
        task.updated_at = utcnow()
        session.commit()
        session.refresh(task)
        return task.to_dict()


def get(issue_number: int) -> dict | None:
    """Retrieve the entry for the given issue number."""
    with SessionLocal() as session:
        task = session.get(Task, issue_number)
        return task.to_dict() if task else None


def clear() -> None:
    """Remove all entries from the store."""
    with SessionLocal() as session:
        session.query(Task).delete()
        session.commit()


def get_all() -> list:
    """Return a list of all entries in the store, sorted by issue number."""
    with SessionLocal() as session:
        tasks = session.scalars(select(Task).order_by(Task.issue_number)).all()
        return [t.to_dict() for t in tasks]


def get_status(issue_number: int) -> str | None:
    """Return the current status of the given issue number."""
    with SessionLocal() as session:
        task = session.get(Task, issue_number)
        return task.status if task else None

"""
Persistent store for tracking GitHub issues, their associated Devin sessions,
and processing statuses, backed by SQLAlchemy (PostgreSQL in production,
SQLite fallback for tests/local runs). The public function signatures are
unchanged from the original in-memory implementation.
"""

import os
import secrets
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

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
#   "poll_token": str | None,        # owner of the active poll chain
#   "poll_lease_until": str | None,  # ISO; poll considered lost once this has passed
#   "created_at": str (ISO),
#   "updated_at": str (ISO),
# }

_MUTABLE_FIELDS = {"title", "issue_url", "session_id", "session_url", "status", "pr_url"}


def _repository() -> str:
    return os.environ["GITHUB_REPO"]


def upsert(issue_number: int, **kwargs) -> dict:
    """Insert a new entry or update an existing one for the given issue number."""
    repository = _repository()
    with SessionLocal() as session:
        task = session.get(Task, (repository, issue_number))
        if task is None:
            task = Task(
                repository=repository,
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


def claim_remediation(issue_number: int, title: str, issue_url: str, lease_seconds: int) -> bool:
    """Atomically reserve the right to create a Devin session for an issue.

    Exactly one caller wins when several remediation tasks for the same issue
    overlap (replica startup scans, Beat reconciliation, manual /scan, webhook
    redeliveries). The winner's row is left ``running`` with no ``session_id``
    and a short creation lease in ``poll_lease_until``; the caller must then
    record the session (``upsert``) or mark the row ``failed``. A row that is
    ``running`` without a session whose lease has expired is a stranded
    delivery (worker died between the claim and saving the session) and can
    be re-claimed. Rows with a live session are never re-claimed.
    """
    repository = _repository()
    now = utcnow()
    lease_until = now + timedelta(seconds=lease_seconds)
    with SessionLocal() as session:
        if _try_insert_claim(session, repository, issue_number, title, issue_url, now, lease_until):
            session.commit()
            return True
        result = session.execute(
            update(Task)
            .where(
                Task.repository == repository,
                Task.issue_number == issue_number,
                or_(
                    Task.status != "running",
                    and_(
                        Task.session_id.is_(None),
                        or_(Task.poll_lease_until.is_(None), Task.poll_lease_until < now),
                    ),
                ),
            )
            .values(
                title=title,
                issue_url=issue_url,
                status="running",
                session_id=None,
                session_url=None,
                pr_url=None,
                poll_token=None,
                poll_lease_until=lease_until,
                updated_at=now,
            )
        )
        session.commit()
        return result.rowcount == 1


def _try_insert_claim(
    session: Session,
    repository: str,
    issue_number: int,
    title: str,
    issue_url: str,
    now: datetime,
    lease_until: datetime,
) -> bool:
    values = {
        "repository": repository,
        "issue_number": issue_number,
        "title": title,
        "issue_url": issue_url,
        "status": "running",
        "poll_lease_until": lease_until,
        "created_at": now,
        "updated_at": now,
    }
    insert = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
    stmt = insert(Task).values(**values).on_conflict_do_nothing().returning(Task.issue_number)
    return session.execute(stmt).first() is not None


def get(issue_number: int) -> dict | None:
    """Retrieve the entry for the given issue number."""
    with SessionLocal() as session:
        task = session.get(Task, (_repository(), issue_number))
        return task.to_dict() if task else None


def clear() -> None:
    """Remove all entries for the configured repository from the store."""
    with SessionLocal() as session:
        session.query(Task).filter(Task.repository == _repository()).delete()
        session.commit()


def get_all() -> list:
    """Return entries for the configured repository, sorted by issue number."""
    with SessionLocal() as session:
        tasks = session.scalars(
            select(Task)
            .where(Task.repository == _repository())
            .order_by(Task.issue_number)
        ).all()
        return [t.to_dict() for t in tasks]


def get_status(issue_number: int) -> str | None:
    """Return the current status of the given issue number."""
    with SessionLocal() as session:
        task = session.get(Task, (_repository(), issue_number))
        return task.status if task else None


def claim_poll(
    issue_number: int, session_id: str, lease_seconds: int, only_if_lost: bool = False
) -> str | None:
    """Atomically take ownership of polling for a running session.

    Issues a new poll token and lease. With ``only_if_lost`` the claim only
    succeeds when no other poll chain holds a live lease, so concurrent
    reconciliation scans cannot start duplicate chains. Returns the token, or
    None if the claim was not granted.
    """
    token = secrets.token_hex(16)
    now = utcnow()
    stmt = (
        update(Task)
        .where(
            Task.repository == _repository(),
            Task.issue_number == issue_number,
            Task.session_id == session_id,
            Task.status == "running",
        )
        .values(poll_token=token, poll_lease_until=now + timedelta(seconds=lease_seconds))
    )
    if only_if_lost:
        stmt = stmt.where(or_(Task.poll_lease_until.is_(None), Task.poll_lease_until < now))
    with SessionLocal() as session:
        granted = session.execute(stmt).rowcount == 1
        session.commit()
    return token if granted else None


def renew_poll(issue_number: int, poll_token: str, lease_seconds: int) -> bool:
    """Extend the lease of the poll chain identified by ``poll_token``."""
    with SessionLocal() as session:
        result = session.execute(
            update(Task)
            .where(
                Task.repository == _repository(),
                Task.issue_number == issue_number,
                Task.poll_token == poll_token,
            )
            .values(poll_lease_until=utcnow() + timedelta(seconds=lease_seconds))
        )
        session.commit()
        return result.rowcount == 1


def release_poll(issue_number: int, poll_token: str | None = None) -> None:
    """Drop poll ownership (session reached a terminal status, or the chain
    could not be scheduled). With ``poll_token`` only that chain's lease is
    released, so a newer owner is left untouched."""
    stmt = (
        update(Task)
        .where(Task.repository == _repository(), Task.issue_number == issue_number)
        .values(poll_token=None, poll_lease_until=None)
    )
    if poll_token is not None:
        stmt = stmt.where(Task.poll_token == poll_token)
    with SessionLocal() as session:
        session.execute(stmt)
        session.commit()


def detach_closed_pr(issue_number: int, pr_url: str) -> datetime | None:
    """Atomically hand an issue back for remediation after its PR was closed
    unmerged: only a row still pointing at ``pr_url`` and not ``running`` is
    flipped to ``failed`` with the association cleared, so concurrent or
    replayed deliveries cannot clobber a replacement session. Returns the
    ``updated_at`` stamp written by this transition (the compensation token
    for ``reattach_closed_pr``), or None if no row matched."""
    stamp = utcnow()
    stmt = (
        update(Task)
        .where(
            Task.repository == _repository(),
            Task.issue_number == issue_number,
            Task.pr_url == pr_url,
            Task.status.in_(("completed", "failed")),
        )
        .values(status="failed", pr_url="", updated_at=stamp)
    )
    with SessionLocal() as session:
        result = session.execute(stmt)
        session.commit()
        return stamp if result.rowcount == 1 else None


def reattach_closed_pr(issue_number: int, pr_url: str, token: datetime) -> bool:
    """Undo ``detach_closed_pr`` when the retry could not be published. Only
    touches a row whose ``updated_at`` still equals the detach stamp, so any
    intervening write (a replacement starting, failing, or recording a new
    PR) permanently invalidates the compensation."""
    stmt = (
        update(Task)
        .where(
            Task.repository == _repository(),
            Task.issue_number == issue_number,
            Task.status == "failed",
            Task.pr_url == "",
            Task.updated_at == token,
        )
        .values(pr_url=pr_url, updated_at=utcnow())
    )
    with SessionLocal() as session:
        result = session.execute(stmt)
        session.commit()
        return result.rowcount == 1


def get_unpolled_running() -> list:
    """Running sessions whose poll lease is missing or expired, i.e. whose poll
    chain was never scheduled or has been lost (worker restart, broker error)."""
    now = utcnow()
    with SessionLocal() as session:
        tasks = session.scalars(
            select(Task)
            .where(
                Task.repository == _repository(),
                Task.status == "running",
                Task.session_id.is_not(None),
                or_(Task.poll_lease_until.is_(None), Task.poll_lease_until < now),
            )
            .order_by(Task.issue_number)
        ).all()
        return [t.to_dict() for t in tasks]

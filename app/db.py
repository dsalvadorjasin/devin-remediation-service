"""
SQLAlchemy engine, session factory and ORM models.

The database is selected via DATABASE_URL. Production uses PostgreSQL
(postgresql+psycopg://...); when the variable is unset a local SQLite file is
used so unit tests and quick local runs need no external service.
"""

import os
from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

DEFAULT_DATABASE_URL = "sqlite:///./remediation.db"


def database_url() -> str:
    return os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)


def _make_engine(url: str):
    if url.startswith("sqlite"):
        kwargs: dict = {"connect_args": {"check_same_thread": False}}
        if ":memory:" in url or url == "sqlite://":
            kwargs["poolclass"] = StaticPool
        return create_engine(url, **kwargs)
    return create_engine(url, pool_pre_ping=True)


engine = _make_engine(database_url())
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


class Task(Base):
    """One row per tracked GitHub issue and its Devin session."""

    __tablename__ = "tasks"

    repository: Mapped[str] = mapped_column(String, primary_key=True)
    issue_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String, default="")
    issue_url: Mapped[str] = mapped_column(String, default="")
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    session_url: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="running")
    pr_url: Mapped[str | None] = mapped_column(String, nullable=True)
    poll_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    poll_lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def to_dict(self) -> dict:
        return {
            "issue_number": self.issue_number,
            "title": self.title,
            "issue_url": self.issue_url,
            "session_id": self.session_id,
            "session_url": self.session_url,
            "status": self.status,
            "pr_url": self.pr_url,
            "poll_token": self.poll_token,
            "poll_lease_until": _iso(self.poll_lease_until),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def init_db() -> None:
    """Create tables directly for the SQLite fallback. Postgres schemas are
    managed by Alembic (`alembic upgrade head`), so this is a no-op there."""
    if engine.dialect.name == "sqlite":
        Base.metadata.create_all(engine)

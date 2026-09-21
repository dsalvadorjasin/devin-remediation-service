from abc import ABC, abstractmethod

POLL_INTERVAL_SECONDS = 60


class Orchestrator(ABC):
    """Abstract scheduling seam between the API/ingest layer and the workers."""

    @abstractmethod
    def enqueue_scan(self, force_retry: bool = False) -> None:
        """Run a full labelled-issue scan asynchronously."""

    @abstractmethod
    def enqueue_remediation(self, issue: dict, force_retry: bool = False) -> None:
        """Evaluate one GitHub issue (dict from the GitHub API) and create a
        Devin session for it if the dedup guards allow."""

    @abstractmethod
    def schedule_poll(
        self, issue_number: int, session_id: str, delay_seconds: int = POLL_INTERVAL_SECONDS
    ) -> None:
        """Check a running Devin session after `delay_seconds`; the poll re-schedules
        itself until the session reaches completed/failed."""

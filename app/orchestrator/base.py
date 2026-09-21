from abc import ABC, abstractmethod

POLL_INTERVAL_SECONDS = 60
# A poll chain is considered lost once its lease has been silent for this long
# past the scheduled poll; the reconciliation scan then re-arms it.
POLL_LEASE_GRACE_SECONDS = 2 * POLL_INTERVAL_SECONDS


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
        self,
        issue_number: int,
        session_id: str,
        delay_seconds: int = POLL_INTERVAL_SECONDS,
        poll_token: str | None = None,
    ) -> None:
        """Check a running Devin session after `delay_seconds`; the poll re-schedules
        itself until the session reaches completed/failed. `poll_token` identifies
        the owning poll chain (see store.claim_poll) so superseded chains stop."""

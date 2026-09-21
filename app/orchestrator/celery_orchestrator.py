from app.orchestrator.base import POLL_INTERVAL_SECONDS, Orchestrator


class CeleryOrchestrator(Orchestrator):
    """Orchestrator that dispatches work as Celery tasks (see app/tasks.py)."""

    def enqueue_scan(self, force_retry: bool = False) -> None:
        from app import tasks

        tasks.scan_task.apply_async(kwargs={"force_retry": force_retry})

    def enqueue_remediation(self, issue: dict, force_retry: bool = False) -> None:
        from app import tasks

        tasks.remediate_issue_task.apply_async(
            kwargs={"issue": issue, "force_retry": force_retry}
        )

    def schedule_poll(
        self,
        issue_number: int,
        session_id: str,
        delay_seconds: int = POLL_INTERVAL_SECONDS,
        poll_token: str | None = None,
    ) -> None:
        from app import tasks

        tasks.poll_session_task.apply_async(
            kwargs={
                "issue_number": issue_number,
                "session_id": session_id,
                "poll_token": poll_token,
            },
            countdown=delay_seconds,
        )

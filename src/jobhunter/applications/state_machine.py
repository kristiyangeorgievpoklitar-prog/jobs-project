"""Job lifecycle state machine.

Transitions are explicit and validated so a job can never silently jump from,
say, DISCOVERED to APPLIED. Every accepted transition writes an audit event.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from jobhunter.db.base import utcnow
from jobhunter.db.models import Application, ApplicationEvent, Job
from jobhunter.domain.enums import JobState
from jobhunter.logging_setup import get_logger

log = get_logger(__name__)

ALLOWED_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.DISCOVERED: frozenset(
        {JobState.CLASSIFIED, JobState.SKIPPED, JobState.BLOCKED, JobState.DISCOVERED}
    ),
    JobState.CLASSIFIED: frozenset(
        {JobState.MATCHED, JobState.SKIPPED, JobState.BLOCKED, JobState.CLASSIFIED}
    ),
    JobState.MATCHED: frozenset(
        {
            JobState.REVIEW,
            JobState.APPROVED,
            JobState.SKIPPED,
            JobState.BLOCKED,
            JobState.MATCHED,
            JobState.CLASSIFIED,
        }
    ),
    JobState.REVIEW: frozenset(
        {JobState.APPROVED, JobState.SKIPPED, JobState.BLOCKED, JobState.REVIEW, JobState.MATCHED}
    ),
    JobState.APPROVED: frozenset(
        {JobState.APPLYING, JobState.SKIPPED, JobState.BLOCKED, JobState.REVIEW, JobState.APPROVED}
    ),
    JobState.APPLYING: frozenset(
        {JobState.APPLIED, JobState.FAILED, JobState.BLOCKED, JobState.APPLYING}
    ),
    # Terminal: an applied job is never re-opened, which is what stops repeat
    # applications at the state level.
    JobState.APPLIED: frozenset({JobState.APPLIED}),
    JobState.FAILED: frozenset(
        {JobState.APPROVED, JobState.APPLYING, JobState.SKIPPED, JobState.FAILED, JobState.REVIEW}
    ),
    JobState.SKIPPED: frozenset({JobState.REVIEW, JobState.MATCHED, JobState.SKIPPED}),
    JobState.BLOCKED: frozenset(
        {JobState.APPROVED, JobState.APPLYING, JobState.SKIPPED, JobState.REVIEW, JobState.BLOCKED}
    ),
}


class InvalidTransitionError(RuntimeError):
    def __init__(self, current: JobState, target: JobState) -> None:
        super().__init__(f"Cannot move job from {current.value} to {target.value}")
        self.current = current
        self.target = target


def _coerce(state: JobState | str) -> JobState:
    return state if isinstance(state, JobState) else JobState(str(state))


def can_transition(current: JobState | str, target: JobState | str) -> bool:
    current_state, target_state = _coerce(current), _coerce(target)
    return target_state in ALLOWED_TRANSITIONS.get(current_state, frozenset())


def transition_job(
    session: Session,
    job: Job,
    target: JobState,
    *,
    event: str = "state_change",
    detail: dict | None = None,
    application: Application | None = None,
    strict: bool = True,
) -> bool:
    """Move a job to ``target``, recording an audit event.

    Returns False when the transition is rejected and ``strict`` is False.
    """
    current = _coerce(job.state)
    target = _coerce(target)

    if not can_transition(current, target):
        if strict:
            raise InvalidTransitionError(current, target)
        log.warning(
            "invalid_transition_ignored",
            job_id=job.id,
            from_state=current.value,
            to_state=target.value,
        )
        return False

    if current is not target:
        job.state = target
        job.state_changed_at = utcnow()

    session.add(
        ApplicationEvent(
            application_id=application.id if application is not None else None,
            job_id=job.id,
            event=event,
            from_state=current.value,
            to_state=target.value,
            detail=detail or {},
        )
    )
    if application is not None:
        application.state = target
    session.flush()
    return True


def record_event(
    session: Session,
    *,
    event: str,
    job: Job | None = None,
    application: Application | None = None,
    detail: dict | None = None,
) -> ApplicationEvent:
    """Append a non-transition audit event."""
    record = ApplicationEvent(
        application_id=application.id if application is not None else None,
        job_id=job.id if job is not None else None,
        event=event,
        from_state=str(job.state) if job is not None else None,
        to_state=str(job.state) if job is not None else None,
        detail=detail or {},
    )
    session.add(record)
    session.flush()
    return record

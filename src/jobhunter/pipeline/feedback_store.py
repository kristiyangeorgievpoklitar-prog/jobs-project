"""Recording what the candidate decided.

Feedback is written once and never edited, and every write refreshes the learned
preferences. The rebuild is cheap (the history is small) and doing it here means
the ranking the candidate sees on the next page load already reflects the
decision they just made.
"""

from __future__ import annotations

from jobhunter.db.models import Job, UserFeedback
from jobhunter.db.models import JobEvaluation as JobEvaluationRow
from jobhunter.logging_setup import get_logger
from jobhunter.personalization.learner import VALID_ACTIONS, VALID_REASONS, rebuild_preferences

log = get_logger(__name__)


def record_feedback(
    session,
    job_id: int,
    *,
    action: str,
    reason: str | None = None,
    note: str | None = None,
) -> UserFeedback:
    """Store one decision and refresh what has been learned from it."""
    action = (action or "").strip().lower()
    if action not in VALID_ACTIONS:
        raise ValueError(f"action must be one of {', '.join(VALID_ACTIONS)}, got {action!r}")

    if reason:
        reason = reason.strip().lower()
        if reason not in VALID_REASONS:
            raise ValueError(f"reason must be one of {', '.join(VALID_REASONS)}, got {reason!r}")

    job = session.get(Job, job_id)
    if job is None:
        raise ValueError(f"No job with id {job_id}")

    current = (
        session.query(JobEvaluationRow)
        .filter(JobEvaluationRow.job_id == job_id, JobEvaluationRow.is_current.is_(True))
        .one_or_none()
    )

    row = UserFeedback(
        job_id=job_id,
        evaluation_id=current.id if current else None,
        action=action,
        reason=reason,
        note=note,
        predicted_decision=current.decision if current else None,
    )
    session.add(row)
    session.flush()

    rebuild_preferences(session)

    log.info(
        "user_feedback_recorded",
        job_id=job_id,
        action=action,
        reason=reason,
        predicted=row.predicted_decision,
    )
    return row


def agreement_stats(session) -> dict[str, int]:
    """How often the system's recommendation matched what the candidate did.

    The only honest measure of live quality, as opposed to benchmark quality.
    """
    rows = session.query(UserFeedback).filter(UserFeedback.predicted_decision.isnot(None)).all()
    agreed = sum(
        1
        for r in rows
        if (r.action == "apply" and r.predicted_decision in ("apply", "review"))
        or (r.action == "skip" and r.predicted_decision == "skip")
    )
    return {"total": len(rows), "agreed": agreed, "disagreed": len(rows) - agreed}

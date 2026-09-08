"""Shared dependencies and read queries for the dashboard."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session, joinedload

from jobhunter.context import AppContext
from jobhunter.db.models import (
    Application,
    AutomationRun,
    ErrorRecord,
    Job,
    JobMatch,
    Notification,
)
from jobhunter.domain.enums import JobState, Recommendation

_context: AppContext | None = None


def set_context(context: AppContext) -> None:
    global _context
    _context = context


def get_context() -> AppContext:
    if _context is None:
        raise RuntimeError("Application context is not configured")
    return _context


def latest_match_subquery():
    """Subquery giving the newest match id per job."""
    return select(func.max(JobMatch.id).label("match_id")).group_by(JobMatch.job_id).subquery()


def latest_match_for_job():
    """Correlated subquery: the newest match id for the Job in the outer query."""
    return (
        select(func.max(JobMatch.id))
        .where(JobMatch.job_id == Job.id)
        .correlate(Job)
        .scalar_subquery()
    )


def job_rows(
    session: Session,
    *,
    state: str | None = None,
    min_score: int = 0,
    search: str | None = None,
    only_it: bool = False,
    limit: int = 100,
    offset: int = 0,
    order: str = "score",
) -> list[dict[str, Any]]:
    """Jobs joined to their most recent match, ready for the table."""
    # The company is eagerly loaded: templates render it after the session has
    # closed, so a lazy load here would raise DetachedInstanceError.
    stmt = (
        select(Job, JobMatch)
        .outerjoin(JobMatch, JobMatch.id == latest_match_for_job())
        .options(joinedload(Job.company))
    )

    if state:
        stmt = stmt.where(Job.state == JobState(state))
    if only_it:
        stmt = stmt.where(Job.is_it.is_(True))
    if search:
        pattern = f"%{search.lower()}%"
        stmt = stmt.where(func.lower(Job.title).like(pattern))
    if min_score > 0:
        stmt = stmt.where(func.coalesce(JobMatch.score, 0) >= min_score)

    if order == "date":
        stmt = stmt.order_by(desc(Job.first_seen_at))
    else:
        stmt = stmt.order_by(desc(func.coalesce(JobMatch.score, 0)), desc(Job.first_seen_at))

    stmt = stmt.limit(limit).offset(offset)

    rows = session.execute(stmt).all()

    # The current evaluation is what the table actually shows; the old match row
    # is kept only for the legacy score column in the statistics view.
    from jobhunter.db.models import JobEvaluation as EvaluationRow

    job_ids = [job.id for job, _ in rows]
    evaluations = {}
    if job_ids:
        evaluations = {
            row.job_id: row
            for row in session.scalars(
                select(EvaluationRow).where(
                    EvaluationRow.job_id.in_(job_ids), EvaluationRow.is_current.is_(True)
                )
            ).all()
        }

    return [
        {
            "job": job,
            "match": match,
            "score": match.score if match else 0,
            "evaluation": evaluations.get(job.id),
            "decision": evaluations[job.id].decision if job.id in evaluations else None,
        }
        for job, match in rows
    ]


def overview_stats(session: Session) -> dict[str, Any]:
    """Numbers for the overview cards."""
    today = datetime.now(UTC) - timedelta(hours=24)

    def count(stmt) -> int:
        return session.scalar(stmt) or 0

    latest = latest_match_subquery()
    high_match = count(
        select(func.count())
        .select_from(JobMatch)
        .where(JobMatch.id.in_(select(latest.c.match_id)), JobMatch.score >= 75)
    )
    apply_ready = count(
        select(func.count())
        .select_from(JobMatch)
        .where(
            JobMatch.id.in_(select(latest.c.match_id)),
            JobMatch.recommendation == Recommendation.APPLY,
        )
    )

    return {
        "jobs_total": count(select(func.count()).select_from(Job)),
        "jobs_today": count(
            select(func.count()).select_from(Job).where(Job.first_seen_at >= today)
        ),
        "jobs_relevant": count(select(func.count()).select_from(Job).where(Job.is_it.is_(True))),
        "high_match": high_match,
        "apply_ready": apply_ready,
        "pending_review": count(
            select(func.count()).select_from(Job).where(Job.state == JobState.REVIEW)
        ),
        "approved": count(
            select(func.count()).select_from(Job).where(Job.state == JobState.APPROVED)
        ),
        "applications_sent": count(
            select(func.count()).select_from(Application).where(Application.success.is_(True))
        ),
        # An application stopped at a CAPTCHA or questionnaire is waiting for a
        # person, not failed; counting it as a failure misreads the run.
        "applications_failed": count(
            select(func.count())
            .select_from(Application)
            .where(Application.success.is_(False), Application.is_manual.is_(False))
        ),
        "applications_awaiting_you": count(
            select(func.count())
            .select_from(Application)
            .where(Application.success.isnot(True), Application.is_manual.is_(True))
        ),
        "blocked": count(
            select(func.count()).select_from(Job).where(Job.state == JobState.BLOCKED)
        ),
        "errors": count(
            select(func.count()).select_from(ErrorRecord).where(ErrorRecord.created_at >= today)
        ),
    }


def recent_runs(session: Session, limit: int = 5) -> list[AutomationRun]:
    return list(
        session.scalars(select(AutomationRun).order_by(desc(AutomationRun.id)).limit(limit)).all()
    )


def recent_notifications(session: Session, limit: int = 20) -> list[Notification]:
    return list(
        session.scalars(select(Notification).order_by(desc(Notification.id)).limit(limit)).all()
    )


def unread_notification_count(session: Session) -> int:
    return (
        session.scalar(
            select(func.count()).select_from(Notification).where(Notification.read_at.is_(None))
        )
        or 0
    )


def job_detail(session: Session, job_id: int) -> dict[str, Any] | None:
    job = session.scalar(select(Job).where(Job.id == job_id).options(joinedload(Job.company)))
    if job is None:
        return None
    matches = list(
        session.scalars(
            select(JobMatch).where(JobMatch.job_id == job_id).order_by(desc(JobMatch.id))
        ).all()
    )
    application = session.scalar(select(Application).where(Application.job_id == job_id))

    from jobhunter.db.models import JobEvaluation as EvaluationRow
    from jobhunter.db.models import UserFeedback
    from jobhunter.pipeline.evaluation_store import to_domain

    evaluation_rows = list(
        session.scalars(
            select(EvaluationRow)
            .where(EvaluationRow.job_id == job_id)
            .order_by(desc(EvaluationRow.id))
        ).all()
    )
    current = next((row for row in evaluation_rows if row.is_current), None)
    feedback = list(
        session.scalars(
            select(UserFeedback)
            .where(UserFeedback.job_id == job_id)
            .order_by(desc(UserFeedback.id))
        ).all()
    )

    return {
        "job": job,
        "match": matches[0] if matches else None,
        "history": matches[1:],
        "application": application,
        "evaluation": to_domain(current) if current is not None else None,
        "evaluation_row": current,
        "evaluation_history": [row for row in evaluation_rows if not row.is_current][:5],
        "feedback": feedback,
    }

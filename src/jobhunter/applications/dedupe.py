"""Duplicate detection.

Applying twice to the same posting is the single worst failure mode of an
automated job hunter, so duplicates are checked on four independent keys and
the database additionally enforces one application row per job.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobhunter.db.models import Application, Job
from jobhunter.domain.enums import JobState
from jobhunter.normalize.normalizer import content_fingerprint, normalize_company, normalize_text
from jobhunter.sources.jobsbg.urls import normalize_url


@dataclass(frozen=True)
class DuplicateCheck:
    is_duplicate: bool
    reason: str | None = None
    existing_job_id: int | None = None

    def __bool__(self) -> bool:
        return self.is_duplicate


def find_existing_job(
    session: Session,
    *,
    fingerprint: str | None = None,
    source: str = "jobs.bg",
    source_job_id: str | None = None,
    normalized_url: str | None = None,
    company_name: str | None = None,
    title: str | None = None,
) -> Job | None:
    """Locate an already-stored job using progressively weaker keys."""
    if fingerprint and (job := session.scalar(select(Job).where(Job.fingerprint == fingerprint))):
        return job

    if source_job_id and (
        job := session.scalar(
            select(Job).where(Job.source == source, Job.source_job_id == source_job_id)
        )
    ):
        return job

    if normalized_url:
        canonical = normalize_url(normalized_url)
        job = session.scalar(select(Job).where(Job.normalized_url == canonical))
        # Two listings that both carry a source id, and disagree on it, are
        # genuinely different postings even if a URL happens to collide.
        if job is not None and not (
            source_job_id and job.source_job_id and job.source_job_id != source_job_id
        ):
            return job

    # Company + title is the weakest key, so it only decides identity when the
    # listing carries no id of its own. A genuine re-post under a new id stays a
    # separate row; applying to it is still blocked by :func:`has_applied`.
    if company_name and title and not source_job_id:
        normalized_title = normalize_text(title)
        candidates = session.scalars(
            select(Job).where(Job.title_normalized == normalized_title)
        ).all()
        target_company = normalize_company(company_name)
        for candidate in candidates:
            if normalize_company(candidate.company_display) == target_company:
                return candidate

    return None


def has_applied(session: Session, job: Job) -> DuplicateCheck:
    """Whether an application already exists for this job or an equivalent one."""
    if job.state == JobState.APPLIED:
        return DuplicateCheck(True, "job already marked APPLIED", job.id)

    application = session.scalar(select(Application).where(Application.job_id == job.id))
    if application is not None:
        if application.success or application.state == JobState.APPLIED:
            return DuplicateCheck(True, "application already submitted", job.id)
        if application.state == JobState.APPLYING:
            return DuplicateCheck(True, "an application is already in progress", job.id)

    # Same company + title applied to under a different listing id.
    target = content_fingerprint(job.company_display, job.title)
    applied_jobs = session.scalars(
        select(Job)
        .join(Application, Application.job_id == Job.id)
        .where(Application.success.is_(True))
    ).all()
    for other in applied_jobs:
        if other.id == job.id:
            continue
        if content_fingerprint(other.company_display, other.title) == target:
            return DuplicateCheck(
                True, f"already applied to the same role (job #{other.id})", other.id
            )

    return DuplicateCheck(False)


def assert_not_duplicate(session: Session, job: Job) -> None:
    """Raise if applying to this job would be a duplicate."""
    check = has_applied(session, job)
    if check.is_duplicate:
        raise DuplicateApplicationError(check.reason or "duplicate application")


class DuplicateApplicationError(RuntimeError):
    """Raised when an application would duplicate an existing one."""

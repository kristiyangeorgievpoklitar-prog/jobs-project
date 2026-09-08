"""Persistence helpers that map domain objects onto ORM rows."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobhunter.applications.dedupe import find_existing_job
from jobhunter.db.base import utcnow
from jobhunter.db.models import Company, ErrorRecord, Job, JobMatch
from jobhunter.domain.enums import JobState
from jobhunter.domain.schemas import ClassificationResult, MatchResult, NormalizedJob, RawJob
from jobhunter.logging_setup import get_logger
from jobhunter.normalize.normalizer import normalize_company

log = get_logger(__name__)


def upsert_company(
    session: Session, name: str | None, source_id: str | None = None
) -> Company | None:
    """Get or create a company by its normalized name."""
    if not name or not name.strip():
        return None
    normalized = normalize_company(name)
    if not normalized:
        return None

    company = session.scalar(select(Company).where(Company.normalized_name == normalized))
    if company is None:
        company = Company(
            name=name.strip(), normalized_name=normalized, source_company_id=source_id
        )
        session.add(company)
        session.flush()
    elif source_id and not company.source_company_id:
        company.source_company_id = source_id
    return company


def upsert_job(session: Session, normalized: NormalizedJob) -> tuple[Job, bool]:
    """Insert or refresh a job. Returns (job, is_new).

    Re-seeing a listing updates ``last_seen_at`` and the seen counter rather
    than creating a second row, which is what keeps the pipeline idempotent.
    """
    existing = find_existing_job(
        session,
        fingerprint=normalized.fingerprint,
        source=normalized.source,
        source_job_id=normalized.source_job_id,
        normalized_url=normalized.normalized_url,
        company_name=normalized.company_name,
        title=normalized.title,
    )

    company = upsert_company(session, normalized.company_name, normalized.company_source_id)

    if existing is not None:
        existing.last_seen_at = utcnow()
        existing.seen_count = (existing.seen_count or 0) + 1
        # Backfill anything the earlier pass could not see.
        if normalized.description and not existing.description:
            existing.description = normalized.description
            existing.description_hash = normalized.description_hash
        if normalized.city and not existing.city:
            existing.city = normalized.city
        if normalized.location_raw and not existing.location_raw:
            existing.location_raw = normalized.location_raw
        if normalized.tech_keywords and not existing.tech_keywords:
            existing.tech_keywords = normalized.tech_keywords
        for field in ("level_raw", "experience_raw", "work_mode_raw"):
            if getattr(normalized, field, None) and not getattr(existing, field, None):
                setattr(existing, field, getattr(normalized, field))
        if normalized.languages and not existing.languages_raw:
            existing.languages_raw = list(normalized.languages)
        if normalized.application_method.value != "unknown":
            existing.application_method = normalized.application_method
            existing.application_url = normalized.application_url
        if company is not None and existing.company_id is None:
            existing.company_id = company.id
        session.flush()
        return existing, False

    job = Job(
        fingerprint=normalized.fingerprint,
        source=normalized.source,
        source_job_id=normalized.source_job_id,
        source_url=normalized.source_url,
        normalized_url=normalized.normalized_url,
        title=normalized.title,
        title_normalized=normalized.title_normalized,
        company_id=company.id if company else None,
        company_name_raw=normalized.company_name,
        description=normalized.description,
        description_hash=normalized.description_hash,
        location_raw=normalized.location_raw,
        city=normalized.city,
        work_mode=normalized.work_mode,
        salary_min=normalized.salary.minimum,
        salary_max=normalized.salary.maximum,
        salary_currency=normalized.salary.currency,
        salary_period=normalized.salary.period,
        salary_raw=normalized.salary.raw,
        employment_type=normalized.employment_type,
        language=normalized.language,
        level_raw=normalized.level_raw,
        experience_raw=normalized.experience_raw,
        work_mode_raw=normalized.work_mode_raw,
        languages_raw=list(normalized.languages or []),
        tech_keywords=normalized.tech_keywords,
        years_experience_required=normalized.years_experience_required,
        application_method=normalized.application_method,
        application_url=normalized.application_url,
        posted_at=normalized.posted_at,
        posted_at_raw=normalized.posted_at_raw,
        state=JobState.DISCOVERED,
    )
    session.add(job)
    session.flush()
    return job, True


def apply_classification(job: Job, classification: ClassificationResult) -> None:
    """Write classifier output onto the job row."""
    job.seniority = classification.seniority
    job.seniority_confidence = classification.seniority_confidence
    job.seniority_signals = classification.seniority_signals
    job.is_it = classification.is_it
    job.it_confidence = classification.it_confidence
    job.it_signals = classification.it_signals
    job.location_relevant = classification.location_relevant
    job.requirements_required = classification.requirements_required
    job.requirements_preferred = classification.requirements_preferred
    if classification.years_experience_required is not None:
        job.years_experience_required = classification.years_experience_required
    if classification.language.value != "unknown":
        job.language = classification.language


def record_match(
    session: Session, job: Job, match: MatchResult, *, profile_version: int = 1
) -> JobMatch:
    """Append a scoring result for a job."""
    row = JobMatch(
        job_id=job.id,
        score=match.score,
        confidence=match.confidence,
        recommendation=match.recommendation,
        strengths=match.strengths,
        missing_skills=match.missing_skills,
        disqualifiers=match.disqualifiers,
        component_scores=match.component_scores,
        reasoning=match.reasoning,
        provider=match.provider,
        model=match.model,
        profile_version=profile_version,
    )
    session.add(row)
    session.flush()
    return row


def record_error(
    session: Session,
    *,
    category: str,
    message: str,
    run_id: int | None = None,
    job_id: int | None = None,
    detail: dict | None = None,
    traceback_text: str | None = None,
    screenshot_path: str | None = None,
) -> ErrorRecord:
    row = ErrorRecord(
        run_id=run_id,
        job_id=job_id,
        category=category,
        message=message[:4000],
        detail=detail or {},
        traceback=traceback_text,
        screenshot_path=screenshot_path,
    )
    session.add(row)
    session.flush()
    return row


def raw_job_from_model(job: Job) -> RawJob:
    """Rebuild a RawJob from a stored row.

    Lets anything that works on normalized jobs — the evaluator, the benchmark,
    a re-evaluation pass — run against the database without re-scraping.
    """
    return RawJob(
        source=job.source,
        source_job_id=job.source_job_id,
        source_url=job.source_url,
        title=job.title,
        company_name=job.company_name_raw or (job.company.name if job.company else None),
        location_raw=job.location_raw,
        description=job.description,
        posted_at_raw=job.posted_at_raw,
        salary_raw=job.salary_raw,
        level_raw=job.level_raw,
        experience_raw=job.experience_raw,
        work_mode_raw=job.work_mode_raw,
        employment_raw=job.employment_type.value if job.employment_type else None,
        languages_raw=list(job.languages_raw or []),
        tech_tags=list(job.tech_keywords or []),
        application_method=job.application_method,
        application_url=job.application_url,
    )

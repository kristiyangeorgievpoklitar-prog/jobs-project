"""Reading and writing cached evaluations.

The cache key is every input that could change the answer. Getting that set
right is what makes the cache safe: too narrow and the system serves a stale
verdict after the candidate edits their profile; too wide and it re-runs a
90-second model call on an unchanged listing every morning.
"""

from __future__ import annotations

from sqlalchemy import select, update

from jobhunter.db.models import JobEvaluation as JobEvaluationRow
from jobhunter.domain.enums import Seniority
from jobhunter.domain.evaluation import (
    Decision,
    ExperienceFit,
    JobEvaluation,
    LocationFit,
    RequirementAssessment,
)
from jobhunter.logging_setup import get_logger

log = get_logger(__name__)


def find_cached(
    session,
    *,
    job_content_hash: str,
    candidate_fingerprint: str,
    model: str | None,
    prompt_version: str | None,
    schema_version: int,
) -> JobEvaluationRow | None:
    """The stored evaluation for exactly these inputs, if there is one.

    A degraded row is never served from cache: a timeout is a fact about one run,
    not about the job, and re-trying it is the whole point.
    """
    stmt = (
        select(JobEvaluationRow)
        .where(
            JobEvaluationRow.job_content_hash == job_content_hash,
            JobEvaluationRow.candidate_fingerprint == candidate_fingerprint,
            JobEvaluationRow.model == model,
            JobEvaluationRow.prompt_version == prompt_version,
            JobEvaluationRow.schema_version == schema_version,
            JobEvaluationRow.degraded.is_(False),
        )
        .order_by(JobEvaluationRow.id.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def to_domain(row: JobEvaluationRow) -> JobEvaluation:
    """Rebuild the domain object from a stored row."""
    return JobEvaluation(
        decision=Decision(row.decision),
        confidence=row.confidence,
        recommendation=row.recommendation or "",
        reasoning=row.reasoning or "",
        is_it_role=row.is_it_role,
        seniority=row.seniority or Seniority.UNKNOWN,
        seniority_reasoning=row.seniority_reasoning or "",
        location_fit=LocationFit(row.location_fit) if row.location_fit else LocationFit.UNCLEAR,
        location_reasoning=row.location_reasoning or "",
        employment_fit=row.employment_fit,
        experience_fit=(
            ExperienceFit(row.experience_fit) if row.experience_fit else ExperienceFit.UNKNOWN
        ),
        mandatory_requirements=[
            RequirementAssessment.model_validate(item)
            for item in (row.mandatory_requirements or [])
        ],
        nice_to_have_requirements=[
            RequirementAssessment.model_validate(item)
            for item in (row.nice_to_have_requirements or [])
        ],
        major_strengths=list(row.major_strengths or []),
        major_risks=list(row.major_risks or []),
        rank_score=row.rank_score,
        source=row.source,
        model=row.model,
        prompt_version=row.prompt_version,
        schema_version=row.schema_version,
        job_content_hash=row.job_content_hash,
        profile_version=row.profile_version,
        latency_ms=row.latency_ms,
        degraded=row.degraded,
        degraded_reason=row.degraded_reason,
        created_at=row.created_at,
    )


def store(
    session,
    job_id: int,
    evaluation: JobEvaluation,
    *,
    candidate_fingerprint: str,
) -> JobEvaluationRow:
    """Persist an evaluation and make it the current one for the job."""
    session.execute(
        update(JobEvaluationRow)
        .where(JobEvaluationRow.job_id == job_id, JobEvaluationRow.is_current.is_(True))
        .values(is_current=False)
    )

    row = JobEvaluationRow(
        job_id=job_id,
        decision=evaluation.decision.value,
        confidence=evaluation.confidence,
        recommendation=evaluation.recommendation or None,
        reasoning=evaluation.reasoning or None,
        is_it_role=evaluation.is_it_role,
        seniority=evaluation.seniority,
        seniority_reasoning=evaluation.seniority_reasoning or None,
        location_fit=evaluation.location_fit.value,
        location_reasoning=evaluation.location_reasoning or None,
        employment_fit=evaluation.employment_fit,
        experience_fit=evaluation.experience_fit.value,
        mandatory_requirements=[r.model_dump() for r in evaluation.mandatory_requirements],
        nice_to_have_requirements=[r.model_dump() for r in evaluation.nice_to_have_requirements],
        major_strengths=list(evaluation.major_strengths),
        major_risks=list(evaluation.major_risks),
        rank_score=evaluation.rank_score,
        source=evaluation.source,
        model=evaluation.model,
        prompt_version=evaluation.prompt_version,
        schema_version=evaluation.schema_version,
        job_content_hash=evaluation.job_content_hash,
        candidate_fingerprint=candidate_fingerprint,
        profile_version=evaluation.profile_version,
        latency_ms=evaluation.latency_ms,
        degraded=evaluation.degraded,
        degraded_reason=evaluation.degraded_reason,
        is_current=True,
    )
    session.add(row)
    session.flush()
    return row

"""The evaluation cache and the two-stage evaluator around it.

The cache is the difference between a scan that takes minutes and one that takes
an hour, so what invalidates it is worth pinning down precisely: too eager and
the local model is re-run for nothing, too lazy and the candidate is shown a
verdict about a profile they no longer have.
"""

from __future__ import annotations

import json

import pytest

from jobhunter.ai.local_model import LocalModelConfig, LocalModelProvider
from jobhunter.db.models import Job
from jobhunter.db.models import JobEvaluation as EvaluationRow
from jobhunter.domain.evaluation import Decision
from jobhunter.domain.schemas import CandidateSnapshot, RawJob
from jobhunter.matching.evaluator import EvaluationStats, JobEvaluator
from jobhunter.matching.job_context import job_content_hash
from jobhunter.normalize.normalizer import normalize_job
from jobhunter.pipeline import evaluation_store
from jobhunter.profile.context import candidate_fingerprint

RESPONSE = json.dumps(
    {
        "is_it_role": True,
        "seniority": "junior",
        "seniority_reasoning": "Junior bar.",
        "location_fit": "exact_city",
        "location_reasoning": "Varna office.",
        "experience_fit": "acceptable",
        "mandatory_requirements": [{"requirement": "PHP", "candidate_fit": "strong"}],
        "nice_to_have_requirements": [],
        "major_strengths": ["PHP"],
        "major_risks": ["Little commercial experience"],
        "reasoning": "Good overlap.",
        "decision": "apply",
        "confidence": 0.9,
        "recommendation": "Worth applying.",
    }
)


class CountingProvider(LocalModelProvider):
    """Counts how often the model was actually asked."""

    def __init__(self, **kwargs) -> None:
        super().__init__(LocalModelConfig(**kwargs))
        self.calls = 0

    def _chat(self, system: str, user: str) -> tuple[str, int]:
        self.calls += 1
        return RESPONSE, 500


@pytest.fixture
def candidate() -> CandidateSnapshot:
    return CandidateSnapshot(
        location="Varna",
        preferred_locations=["Varna"],
        years_experience=0.5,
        skills=["php"],
        frameworks=["laravel"],
    )


@pytest.fixture
def stored_job(session):
    normalized = normalize_job(
        RawJob(
            source_url="https://www.jobs.bg/job/555",
            title="Junior PHP Developer",
            company_name="Acme",
            location_raw="Варна",
            description="Requirements: PHP, Laravel and MySQL. " * 30,
        )
    )
    row = Job(
        fingerprint=normalized.fingerprint,
        source=normalized.source,
        source_url=normalized.source_url,
        normalized_url=normalized.normalized_url,
        title=normalized.title,
        title_normalized=normalized.title_normalized,
        company_name_raw=normalized.company_name,
        description=normalized.description,
        city=normalized.city,
    )
    session.add(row)
    session.flush()
    return row, normalized


def build(provider) -> JobEvaluator:
    return JobEvaluator(provider)


def test_an_unchanged_job_is_not_re_evaluated(session, candidate, stored_job):
    row, normalized = stored_job
    provider = CountingProvider(model="m:1b")
    evaluator = build(provider)

    first = evaluator.evaluate(session, row.id, normalized, candidate)
    second = evaluator.evaluate(session, row.id, normalized, candidate)

    assert provider.calls == 1, "the second pass must come from the cache"
    assert first.decision is second.decision


def test_cache_statistics_report_the_saving(session, candidate, stored_job):
    row, normalized = stored_job
    evaluator = build(CountingProvider(model="m:1b"))
    stats = EvaluationStats()

    evaluator.evaluate(session, row.id, normalized, candidate, stats=stats)
    evaluator.evaluate(session, row.id, normalized, candidate, stats=stats)

    assert stats.evaluated == 1
    assert stats.cached == 1
    assert stats.model_calls_avoided == 1


def test_changed_posting_text_invalidates_the_cache(session, candidate, stored_job):
    row, normalized = stored_job
    provider = CountingProvider(model="m:1b")
    evaluator = build(provider)
    evaluator.evaluate(session, row.id, normalized, candidate)

    edited = normalized.model_copy(
        update={"description": normalized.description + " Now also requires Docker."}
    )
    evaluator.evaluate(session, row.id, edited, candidate)

    assert provider.calls == 2


def test_a_changed_profile_invalidates_the_cache(session, candidate, stored_job):
    row, normalized = stored_job
    provider = CountingProvider(model="m:1b")
    evaluator = build(provider)
    evaluator.evaluate(session, row.id, normalized, candidate)

    upskilled = candidate.model_copy(update={"skills": ["php", "docker", "python"]})
    evaluator.evaluate(session, row.id, normalized, upskilled)

    assert provider.calls == 2


def test_a_different_model_invalidates_the_cache(session, candidate, stored_job):
    row, normalized = stored_job
    first = CountingProvider(model="m:1b")
    build(first).evaluate(session, row.id, normalized, candidate)

    second = CountingProvider(model="other:3b")
    build(second).evaluate(session, row.id, normalized, candidate)

    assert second.calls == 1, "another model's verdict is not this model's verdict"


def test_an_edited_prompt_invalidates_the_cache(session, candidate, stored_job):
    row, normalized = stored_job
    provider = CountingProvider(model="m:1b")
    evaluator = build(provider)
    evaluator.evaluate(session, row.id, normalized, candidate)

    # The prompt identity carries a content hash, so editing the text is enough.
    from jobhunter.prompts import PromptTemplate

    provider.prompt = PromptTemplate(
        name="job_evaluation",
        version="v3",
        system=provider.prompt.system + "\nOne more rule.",
        user_template=provider.prompt.user_template,
    )
    evaluator.evaluate(session, row.id, normalized, candidate)

    assert provider.calls == 2


def test_force_bypasses_the_cache(session, candidate, stored_job):
    row, normalized = stored_job
    provider = CountingProvider(model="m:1b")
    evaluator = build(provider)
    evaluator.evaluate(session, row.id, normalized, candidate)
    evaluator.evaluate(session, row.id, normalized, candidate, force=True)
    assert provider.calls == 2


def test_a_degraded_result_is_never_served_from_cache(session, candidate, stored_job):
    """A timeout is a fact about one run, not about the job."""
    row, normalized = stored_job
    fingerprint = candidate_fingerprint(candidate, None)
    from jobhunter.domain.evaluation import JobEvaluation

    degraded = JobEvaluation(
        decision=Decision.REVIEW,
        degraded=True,
        degraded_reason="model timed out",
        model="m:1b",
        prompt_version="job_evaluation.v3.abc",
        job_content_hash=job_content_hash(normalized),
    )
    evaluation_store.store(session, row.id, degraded, candidate_fingerprint=fingerprint)

    found = evaluation_store.find_cached(
        session,
        job_content_hash=job_content_hash(normalized),
        candidate_fingerprint=fingerprint,
        model="m:1b",
        prompt_version="job_evaluation.v3.abc",
        schema_version=1,
    )
    assert found is None


def test_only_the_newest_evaluation_stays_current(session, candidate, stored_job):
    row, normalized = stored_job
    evaluator = build(CountingProvider(model="m:1b"))
    evaluator.evaluate(session, row.id, normalized, candidate)
    evaluator.evaluate(session, row.id, normalized, candidate, force=True)

    current = (
        session.query(EvaluationRow)
        .filter(EvaluationRow.job_id == row.id, EvaluationRow.is_current.is_(True))
        .all()
    )
    assert len(current) == 1


def test_history_is_kept_for_auditing(session, candidate, stored_job):
    row, normalized = stored_job
    evaluator = build(CountingProvider(model="m:1b"))
    evaluator.evaluate(session, row.id, normalized, candidate)
    evaluator.evaluate(session, row.id, normalized, candidate, force=True)

    assert session.query(EvaluationRow).filter(EvaluationRow.job_id == row.id).count() == 2


def test_a_gated_job_never_reaches_the_model(session, candidate):
    normalized = normalize_job(
        RawJob(
            source_url="https://www.jobs.bg/job/777",
            title="Senior Java Architect",
            company_name="Acme",
            location_raw="Варна",
            description="Requirements: 8 years of Java. " * 30,
        )
    )
    row = Job(
        fingerprint=normalized.fingerprint,
        source=normalized.source,
        source_url=normalized.source_url,
        normalized_url=normalized.normalized_url,
        title=normalized.title,
        title_normalized=normalized.title_normalized,
        description=normalized.description,
    )
    session.add(row)
    session.flush()

    provider = CountingProvider(model="m:1b")
    stats = EvaluationStats()
    evaluation = build(provider).evaluate(session, row.id, normalized, candidate, stats=stats)

    assert provider.calls == 0
    assert evaluation.decision is Decision.SKIP
    assert evaluation.source.startswith("gate:")
    assert stats.gated == 1


def test_a_round_trip_through_the_database_preserves_the_evaluation(
    session, candidate, stored_job
):
    row, normalized = stored_job
    evaluator = build(CountingProvider(model="m:1b"))
    original = evaluator.evaluate(session, row.id, normalized, candidate)

    stored = (
        session.query(EvaluationRow)
        .filter(EvaluationRow.job_id == row.id, EvaluationRow.is_current.is_(True))
        .one()
    )
    restored = evaluation_store.to_domain(stored)

    assert restored.decision is original.decision
    assert restored.confidence == original.confidence
    assert restored.major_strengths == original.major_strengths
    assert [r.requirement for r in restored.mandatory_requirements] == [
        r.requirement for r in original.mandatory_requirements
    ]

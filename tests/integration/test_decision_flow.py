"""The daily loop end to end: evaluate, brief, decide, learn, re-rank.

Uses a stubbed local model so the whole path is exercised without a running
one — the point is the wiring between evaluation, briefing, feedback and
ranking, not the model's judgement.
"""

from __future__ import annotations

import json

import pytest

from jobhunter.ai.local_model import LocalModelConfig, LocalModelProvider
from jobhunter.briefing import build_briefing, render_briefing
from jobhunter.db.models import Job
from jobhunter.domain.evaluation import Decision
from jobhunter.domain.schemas import CandidateSnapshot, RawJob
from jobhunter.matching.evaluator import JobEvaluator
from jobhunter.normalize.normalizer import normalize_job
from jobhunter.personalization.learner import load_preferences
from jobhunter.pipeline.feedback_store import record_feedback


def response(decision: str, *, strengths=None, seniority="junior") -> str:
    return json.dumps(
        {
            "is_it_role": True,
            "seniority": seniority,
            "seniority_reasoning": "Junior bar.",
            "location_fit": "exact_city",
            "location_reasoning": "Varna office.",
            "experience_fit": "acceptable",
            "mandatory_requirements": [{"requirement": "PHP", "candidate_fit": "strong"}],
            "nice_to_have_requirements": [
                {"requirement": "Docker", "candidate_fit": "missing"}
            ],
            "major_strengths": strengths or ["PHP", "Laravel"],
            "major_risks": ["No Docker experience"],
            "reasoning": "Strong PHP overlap and a junior-level bar.",
            "decision": decision,
            "confidence": 0.85,
            "recommendation": f"Recommended: {decision}.",
        }
    )


class ScriptedModel(LocalModelProvider):
    """Answers with a canned decision per job title."""

    def __init__(self, by_title: dict[str, str]) -> None:
        super().__init__(LocalModelConfig(model="stub:test"))
        self.by_title = by_title
        self.calls = 0

    def _chat(self, system: str, user: str) -> tuple[str, int]:
        self.calls += 1
        for title, decision in self.by_title.items():
            if title in user:
                return response(decision), 10
        return response("review"), 10


@pytest.fixture
def candidate() -> CandidateSnapshot:
    return CandidateSnapshot(
        location="Varna",
        preferred_locations=["Varna"],
        years_experience=0.5,
        skills=["php"],
        frameworks=["laravel"],
    )


def add_job(session, title: str, *, company="Acme", tech=None, city="Varna"):
    normalized = normalize_job(
        RawJob(
            source_url=f"https://www.jobs.bg/job/{abs(hash(title)) % 99999}",
            title=title,
            company_name=company,
            location_raw="Варна",
            description=f"{title}. Requirements: PHP, Laravel, MySQL. " * 20,
            tech_tags=tech or ["php", "laravel"],
        )
    )
    row = Job(
        fingerprint=normalized.fingerprint,
        source=normalized.source,
        source_url=normalized.source_url,
        normalized_url=normalized.normalized_url,
        title=normalized.title,
        title_normalized=normalized.title_normalized,
        company_name_raw=company,
        description=normalized.description,
        city=city,
        tech_keywords=normalized.tech_keywords,
    )
    session.add(row)
    session.flush()
    return row, normalized


def test_a_full_pass_produces_a_briefing_a_person_can_act_on(session, candidate):
    model = ScriptedModel(
        {
            "Junior PHP Developer": "apply",
            "Junior QA Engineer": "review",
            "Data Entry Clerk": "skip",
        }
    )
    evaluator = JobEvaluator(model)

    for title in ("Junior PHP Developer", "Junior QA Engineer", "Data Entry Clerk"):
        row, normalized = add_job(session, title)
        evaluator.evaluate(session, row.id, normalized, candidate)

    briefing = build_briefing(session)
    assert len(briefing.apply) == 1
    assert len(briefing.review) == 1
    assert briefing.skipped_count == 1
    assert briefing.top_pick is not None
    assert briefing.top_pick.job.title == "Junior PHP Developer"

    text = render_briefing(briefing)
    assert "2 jobs worth your attention" in text
    assert "Junior PHP Developer" in text
    assert "Why:" in text
    assert "Risk:" in text


def test_the_briefing_says_so_plainly_when_there_is_nothing(session, candidate):
    evaluator = JobEvaluator(ScriptedModel({"Data Entry Clerk": "skip"}))
    row, normalized = add_job(session, "Data Entry Clerk")
    evaluator.evaluate(session, row.id, normalized, candidate)

    text = render_briefing(build_briefing(session))
    assert "Nothing worth your attention" in text


def test_feedback_changes_the_order_of_what_is_shown_next(session, candidate):
    """The whole point of personalisation: acting on it must be visible."""
    evaluator = JobEvaluator(ScriptedModel({}))  # everything is REVIEW

    php_row, php_norm = add_job(session, "Junior PHP Developer", tech=["php", "laravel"])
    java_row, java_norm = add_job(session, "Junior Java Developer", tech=["java", "spring"])
    evaluator.evaluate(session, php_row.id, php_norm, candidate)
    evaluator.evaluate(session, java_row.id, java_norm, candidate)

    before = [r.job.title for r in build_briefing(session).review]

    # Turn down three Java roles, saying why.
    for index in range(3):
        row, normalized = add_job(session, f"Java Developer {index}", tech=["java", "spring"])
        evaluator.evaluate(session, row.id, normalized, candidate)
        record_feedback(session, row.id, action="skip", reason="technology")

    preferences = load_preferences(session)
    assert preferences[("technology", "java")] < 0

    after = [r.job.title for r in build_briefing(session).review]
    assert after.index("Junior PHP Developer") < after.index("Junior Java Developer")
    assert before != after or after[0] == "Junior PHP Developer"


def test_a_skipped_listing_is_not_re_evaluated_on_the_next_pass(session, candidate):
    """Feedback feeds the gate, so the model never pays for it twice."""
    model = ScriptedModel({})
    evaluator = JobEvaluator(model)

    row, normalized = add_job(session, "Junior PHP Developer")
    evaluator.evaluate(session, row.id, normalized, candidate)
    record_feedback(session, row.id, action="skip", reason="not_interested")
    calls_before = model.calls

    evaluator.evaluate(
        session,
        row.id,
        normalized,
        candidate,
        rejected_fingerprints={normalized.fingerprint},
        force=True,
    )
    assert model.calls == calls_before, "a listing you turned down must not cost a model call"


def test_a_model_failure_surfaces_the_job_rather_than_hiding_it(session, candidate):
    class BrokenModel(LocalModelProvider):
        def __init__(self) -> None:
            super().__init__(LocalModelConfig(model="stub:test"))

        def _chat(self, system, user):
            raise TimeoutError("model died")

    row, normalized = add_job(session, "Junior PHP Developer")
    evaluation = JobEvaluator(BrokenModel()).evaluate(session, row.id, normalized, candidate)

    assert evaluation.degraded is True
    assert evaluation.decision is Decision.REVIEW

    briefing = build_briefing(session)
    assert briefing.degraded_count == 1
    assert len(briefing.review) == 1, "a failed evaluation must not silently drop the job"
    assert "could not be evaluated" in render_briefing(briefing)


def test_agreement_between_the_system_and_the_candidate_is_recorded(session, candidate):
    evaluator = JobEvaluator(ScriptedModel({"Junior PHP Developer": "apply"}))
    row, normalized = add_job(session, "Junior PHP Developer")
    evaluator.evaluate(session, row.id, normalized, candidate)

    feedback = record_feedback(session, row.id, action="apply")
    assert feedback.predicted_decision == "apply"
    assert feedback.evaluation_id is not None

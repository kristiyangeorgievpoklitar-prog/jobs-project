"""Stage 1 filtering and the safety rules applied to a model's verdict.

The gate and the policy are the two places where the system overrides the
model, so both are tested for the same property from opposite directions: the
gate must never hide a job that deserved a look, and the policy must never
endorse one the evidence does not support.
"""

from __future__ import annotations

import pytest

from jobhunter.domain.evaluation import (
    Decision,
    ExperienceFit,
    Fit,
    JobEvaluation,
    LocationFit,
    RequirementAssessment,
)
from jobhunter.domain.schemas import CandidateSnapshot, RawJob
from jobhunter.matching.gate import GateConfig, Stage1Gate
from jobhunter.matching.policy import DecisionPolicy, apply_policy, may_auto_apply
from jobhunter.normalize.normalizer import normalize_job

LONG_DESCRIPTION = "Requirements: PHP, Laravel, MySQL. " * 30


def job(title: str, *, description: str = LONG_DESCRIPTION, location: str = "Варна"):
    return normalize_job(
        RawJob(
            source_url=f"https://www.jobs.bg/job/{abs(hash(title)) % 10_000}",
            title=title,
            company_name="Acme",
            location_raw=location,
            description=description,
        )
    )


@pytest.fixture
def candidate() -> CandidateSnapshot:
    return CandidateSnapshot(
        location="Varna",
        preferred_locations=["Varna"],
        years_experience=0.5,
        skills=["php", "javascript"],
        frameworks=["laravel"],
    )


# ------------------------------------------------------------------- the gate


@pytest.mark.parametrize(
    "title",
    [
        "Senior Python Developer",
        "Lead Software Engineer",
        "Engineering Manager",
        "Solution Architect",
        "Старши системен администратор",
    ],
)
def test_gate_rejects_unambiguously_senior_titles(title, candidate):
    assert not Stage1Gate().check(job(title), candidate).passed


@pytest.mark.parametrize(
    "title",
    [
        "Junior Software Engineer",
        "Software Developer",
        "PHP Developer",
        "Младши програмист",
        "Стажант разработчик",
        "QA Engineer",
    ],
)
def test_gate_passes_anything_a_junior_could_want(title, candidate):
    assert Stage1Gate().check(job(title), candidate).passed


def test_gate_does_not_treat_junior_senior_wording_as_senior(candidate):
    """"Junior Developer (reporting to a Senior)" is still a junior listing."""
    result = Stage1Gate().check(job("Junior Developer - Senior Team"), candidate)
    assert result.passed


def test_gate_never_judges_skill_overlap(candidate):
    """Skill fit needs a reader; a gate rejection is invisible and final."""
    result = Stage1Gate().check(job("COBOL Developer"), candidate)
    assert result.passed, "an unfamiliar stack is for the model to weigh, not the gate"


def test_gate_rejects_a_listing_the_candidate_already_skipped(candidate):
    listing = job("Junior Developer")
    result = Stage1Gate().check(
        listing, candidate, rejected_fingerprints={listing.fingerprint}
    )
    assert not result.passed
    assert result.reason == "already_rejected"


def test_gate_can_be_told_not_to_require_an_it_role(candidate):
    gate = Stage1Gate(GateConfig(require_it=False))
    assert gate.check(job("Маркетинг специалист", description="Маркетинг."), candidate).passed


# ----------------------------------------------------------------- the policy


def strong_evaluation(**overrides) -> JobEvaluation:
    payload = {
        "decision": Decision.APPLY,
        "confidence": 0.9,
        "is_it_role": True,
        "location_fit": LocationFit.EXACT_CITY,
        "experience_fit": ExperienceFit.GOOD,
        "reasoning": "Good fit.",
        "recommendation": "Worth applying.",
    }
    payload.update(overrides)
    return JobEvaluation(**payload)


def test_policy_leaves_a_well_evidenced_apply_alone(candidate):
    outcome = apply_policy(strong_evaluation(), job("Junior PHP Developer"), candidate)
    assert outcome.evaluation.decision is Decision.APPLY
    assert not outcome.changed


def test_policy_never_endorses_a_job_whose_text_was_never_fetched(candidate):
    outcome = apply_policy(
        strong_evaluation(), job("Junior PHP Developer", description="short"), candidate
    )
    assert outcome.evaluation.decision is Decision.REVIEW
    assert any("posting text" in reason for reason in outcome.adjustments)


def test_policy_downgrades_a_degraded_evaluation(candidate):
    outcome = apply_policy(
        strong_evaluation(degraded=True, degraded_reason="timeout"),
        job("Junior PHP Developer"),
        candidate,
    )
    assert outcome.evaluation.decision is Decision.REVIEW


def test_policy_downgrades_low_confidence(candidate):
    outcome = apply_policy(
        strong_evaluation(confidence=0.3), job("Junior PHP Developer"), candidate
    )
    assert outcome.evaluation.decision is Decision.REVIEW


def test_policy_downgrades_when_a_mandatory_requirement_is_missing(candidate):
    evaluation = strong_evaluation(
        mandatory_requirements=[
            RequirementAssessment(requirement="5 years of Java", candidate_fit=Fit.MISSING)
        ]
    )
    outcome = apply_policy(evaluation, job("Junior PHP Developer"), candidate)
    assert outcome.evaluation.decision is Decision.REVIEW
    assert any("mandatory" in reason for reason in outcome.adjustments)


def test_policy_skips_a_job_in_another_city(candidate):
    outcome = apply_policy(
        strong_evaluation(location_fit=LocationFit.OTHER_CITY),
        job("Junior PHP Developer"),
        candidate,
    )
    assert outcome.evaluation.decision is Decision.SKIP


def test_policy_skips_a_role_the_model_says_is_not_software(candidate):
    outcome = apply_policy(
        strong_evaluation(is_it_role=False), job("Junior PHP Developer"), candidate
    )
    assert outcome.evaluation.decision is Decision.SKIP


def test_policy_only_ever_moves_towards_caution(candidate):
    """A SKIP must never be talked up into a REVIEW or an APPLY."""
    outcome = apply_policy(
        strong_evaluation(decision=Decision.SKIP, confidence=0.99),
        job("Junior PHP Developer"),
        candidate,
    )
    assert outcome.evaluation.decision is Decision.SKIP


def test_policy_records_its_reason_in_the_explanation(candidate):
    from jobhunter.matching.evaluator import _append_adjustments

    text = _append_adjustments("Looks good.", ["posting text was not available"])
    assert "Downgraded because" in text
    assert "posting text was not available" in text


# ------------------------------------------------------------- auto-apply gate


def test_auto_apply_requires_every_condition():
    check = may_auto_apply(
        strong_evaluation(),
        job("Junior PHP Developer"),
        has_valid_cv=True,
        application_route_known=True,
        already_applied=False,
    )
    assert check.allowed
    assert check.blockers == []


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"has_valid_cv": False}, "no valid CV is available"),
        ({"application_route_known": False}, "application route is unknown"),
        ({"already_applied": True}, "an application already exists for this job"),
    ],
)
def test_auto_apply_blocks_on_each_operational_precondition(kwargs, expected):
    base = {
        "has_valid_cv": True,
        "application_route_known": True,
        "already_applied": False,
        **kwargs,
    }
    check = may_auto_apply(strong_evaluation(), job("Junior PHP Developer"), **base)
    assert not check.allowed
    assert expected in check.blockers


def test_auto_apply_refuses_anything_short_of_an_apply_recommendation():
    check = may_auto_apply(
        strong_evaluation(decision=Decision.REVIEW),
        job("Junior PHP Developer"),
        has_valid_cv=True,
        application_route_known=True,
        already_applied=False,
    )
    assert not check.allowed


def test_auto_apply_refuses_a_degraded_evaluation():
    check = may_auto_apply(
        strong_evaluation(degraded=True),
        job("Junior PHP Developer"),
        has_valid_cv=True,
        application_route_known=True,
        already_applied=False,
    )
    assert not check.allowed
    assert "evaluation was degraded" in check.blockers


def test_remote_can_be_excluded_by_configuration(candidate):
    policy = DecisionPolicy(accept_remote=False)
    outcome = apply_policy(
        strong_evaluation(location_fit=LocationFit.REMOTE),
        job("Junior PHP Developer"),
        candidate,
        policy,
    )
    assert outcome.evaluation.decision is Decision.SKIP

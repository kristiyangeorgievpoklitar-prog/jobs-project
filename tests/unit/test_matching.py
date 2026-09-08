"""Scoring, disqualifiers and the decision engine."""

from __future__ import annotations

import pytest

from jobhunter.classify.classifier import classify_job
from jobhunter.domain.enums import Recommendation, Seniority
from jobhunter.domain.schemas import CandidateSnapshot
from jobhunter.matching.rules import (
    ScoringConfig,
    decide,
    expand_tech,
    score_job,
    score_seniority,
)
from tests.conftest import make_job


def evaluate(job, candidate, config=None):
    config = config or ScoringConfig()
    classification = classify_job(
        job, target_locations=candidate.preferred_locations, remote_ok=candidate.remote_ok
    )
    return score_job(job, classification, candidate, config)


class TestScoringConfig:
    def test_weights_total_one_hundred(self) -> None:
        assert ScoringConfig().total_weight == 100.0


class TestExpandTech:
    def test_aliases_resolve_both_directions(self) -> None:
        assert "js" in expand_tech({"javascript"})
        assert "javascript" in expand_tech({"js"})

    def test_sql_family(self) -> None:
        assert "mysql" in expand_tech({"sql"})


class TestSeniorityScore:
    def test_at_or_below_target_is_perfect(self, candidate: CandidateSnapshot) -> None:
        score, _ = score_seniority(Seniority.JUNIOR, candidate, ScoringConfig())
        assert score == 1.0

    def test_far_above_target_is_zero(self, candidate: CandidateSnapshot) -> None:
        score, _ = score_seniority(Seniority.LEAD, candidate, ScoringConfig())
        assert score == 0.0

    def test_unknown_is_neutral(self, candidate: CandidateSnapshot) -> None:
        score, _ = score_seniority(Seniority.UNKNOWN, candidate, ScoringConfig())
        assert 0.4 < score < 0.8


class TestScoring:
    def test_strong_match_scores_high(self, candidate: CandidateSnapshot) -> None:
        job = make_job(
            title="Junior PHP Developer",
            location_raw="Варна",
            tech_tags=["PHP", "Laravel", "MySQL", "JavaScript"],
            description="Junior PHP developer role. Изисквания: PHP, Laravel, MySQL. " * 12,
        )
        result = evaluate(job, candidate)
        assert result.score >= 85
        assert result.recommendation in (Recommendation.APPLY, Recommendation.REVIEW)
        assert not result.disqualifiers

    def test_wrong_location_is_disqualified(self, candidate: CandidateSnapshot) -> None:
        job = make_job(title="Junior PHP Developer", location_raw="София")
        result = evaluate(job, candidate)
        assert result.recommendation is Recommendation.SKIP
        assert any("Location" in d for d in result.disqualifiers)

    def test_senior_role_is_disqualified(self, candidate: CandidateSnapshot) -> None:
        job = make_job(
            title="Senior Java Architect",
            location_raw="Варна",
            level_raw="Ниво Senior-level",
            experience_raw="Години опит от 8 до 12",
        )
        result = evaluate(job, candidate)
        assert result.recommendation is Recommendation.SKIP
        assert result.disqualifiers

    def test_unrelated_profession_is_disqualified(self, candidate: CandidateSnapshot) -> None:
        job = make_job(
            title="Шофьор на камион",
            location_raw="Варна",
            tech_tags=[],
            description="Шофиране на камион в страната и чужбина. " * 12,
        )
        result = evaluate(job, candidate)
        assert result.recommendation is Recommendation.SKIP
        assert any("IT" in d for d in result.disqualifiers)

    def test_reports_missing_skills(self, candidate: CandidateSnapshot) -> None:
        job = make_job(
            title="Junior Developer",
            location_raw="Варна",
            tech_tags=["PHP", "Laravel", "Docker", "Kubernetes"],
            description="Junior developer. Изисквания: PHP, Laravel, Docker, Kubernetes. " * 10,
        )
        result = evaluate(job, candidate)
        assert "docker" in [s.lower() for s in result.missing_skills]

    def test_strengths_list_overlapping_tech(self, candidate: CandidateSnapshot) -> None:
        job = make_job(location_raw="Варна", tech_tags=["PHP", "Laravel"])
        result = evaluate(job, candidate)
        assert any("laravel" in s.lower() for s in result.strengths)

    def test_score_always_within_bounds(self, candidate: CandidateSnapshot) -> None:
        for title in ["Junior PHP Developer", "CEO", "Шофьор", "Senior Architect"]:
            result = evaluate(make_job(title=title, location_raw="Варна"), candidate)
            assert 0 <= result.score <= 100


class TestEvidenceGate:
    """A listing whose description was never fetched must not reach APPLY."""

    def test_thin_evidence_caps_below_auto_apply(self, candidate: CandidateSnapshot) -> None:
        job = make_job(
            title="Junior PHP Developer",
            location_raw="Варна",
            tech_tags=["PHP", "Laravel"],
            description=None,
        )
        result = evaluate(job, candidate)
        assert result.score < ScoringConfig().auto_apply_threshold
        assert result.recommendation is not Recommendation.APPLY
        assert result.confidence <= 0.5

    def test_full_description_can_reach_apply(self, candidate: CandidateSnapshot) -> None:
        job = make_job(
            title="Junior PHP Developer",
            location_raw="Варна",
            tech_tags=["PHP", "Laravel", "MySQL"],
            description="Junior PHP developer with Laravel and MySQL. " * 30,
        )
        result = evaluate(job, candidate)
        assert result.score >= ScoringConfig().auto_apply_threshold


class TestDecisionEngine:
    @pytest.mark.parametrize(
        ("score", "expected"),
        [
            (95, Recommendation.APPLY),
            (90, Recommendation.APPLY),
            (89, Recommendation.REVIEW),
            (75, Recommendation.REVIEW),
            (74, Recommendation.SKIP),
            (0, Recommendation.SKIP),
        ],
    )
    def test_default_thresholds(self, score: int, expected: Recommendation) -> None:
        assert decide(score, [], ScoringConfig()) is expected

    def test_disqualifier_forces_skip_regardless_of_score(self) -> None:
        assert decide(99, ["wrong location"], ScoringConfig()) is Recommendation.SKIP

    def test_thresholds_are_configurable(self) -> None:
        config = ScoringConfig(auto_apply_threshold=70, review_threshold=50)
        assert decide(72, [], config) is Recommendation.APPLY
        assert decide(55, [], config) is Recommendation.REVIEW

"""Tests for the invented-skill check."""

from __future__ import annotations

import pytest

from jobhunter.domain.evaluation import JobEvaluation
from jobhunter.domain.schemas import CandidateSnapshot
from jobhunter.matching.verification import unverified_technologies, verification_warning


@pytest.fixture
def candidate() -> CandidateSnapshot:
    return CandidateSnapshot(
        location="Varna",
        skills=["php", "javascript", "c", "c++"],
        frameworks=["laravel", "livewire", "tailwind"],
        databases=["mysql"],
        tools=["git", "github", "html", "css"],
    )


def strengths(*items: str) -> JobEvaluation:
    return JobEvaluation(major_strengths=list(items))


class TestInventedSkillsAreCaught:
    """Measured: 5 of 36 real evaluations credited the candidate with C#,
    Python, TypeScript or WordPress, none of which they have."""

    def test_a_technology_the_profile_lacks_is_flagged(self, candidate):
        found = unverified_technologies(
            strengths("The candidate has experience with C#, JavaScript, HTML and CSS."),
            candidate,
        )
        assert "c#" in found

    def test_several_are_all_reported(self, candidate):
        found = unverified_technologies(
            strengths("The candidate has practical experience with FastAPI, Flask and Linux."),
            candidate,
        )
        assert {"fastapi", "flask", "linux"} <= set(found)

    def test_a_technology_the_profile_has_is_not_flagged(self, candidate):
        assert (
            unverified_technologies(
                strengths("The candidate has experience with PHP, Laravel and MySQL."), candidate
            )
            == []
        )


class TestTransferArgumentsAreLeftAlone:
    """ "Laravel is related to Spring" argues a transfer; it claims nothing."""

    def test_a_relation_clause_is_not_a_claim(self, candidate):
        found = unverified_technologies(
            strengths(
                "The candidate has experience with PHP, JavaScript and Laravel, "
                "which are related to SQL and Python."
            ),
            candidate,
        )
        assert "python" not in found

    def test_a_similarity_clause_is_not_a_claim(self, candidate):
        found = unverified_technologies(
            strengths("They have experience in Laravel, similar to Spring and Django."),
            candidate,
        )
        assert found == []


class TestTheWarning:
    def test_it_names_the_technologies_and_says_what_to_do(self):
        message = verification_warning(["c#", "python"])
        assert "c#" in message and "python" in message
        assert "profile does not list" in message

    def test_nothing_to_warn_about_produces_no_line(self):
        assert verification_warning([]) == ""

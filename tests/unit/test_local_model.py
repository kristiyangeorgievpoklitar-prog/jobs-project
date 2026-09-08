"""The local model client: parsing, validation and safe failure.

None of these need a running model. A test suite that requires one is a test
suite that stops being run, and the behaviour that matters here — what happens
when the model returns nonsense — is exactly what a live model will not
reliably produce on demand.
"""

from __future__ import annotations

import json

import httpx
import pytest

from jobhunter.ai.local_model import (
    LocalModelConfig,
    LocalModelProvider,
    _close_open_structures,
    _response_schema,
)
from jobhunter.domain.evaluation import Decision, Fit, JobEvaluation
from jobhunter.domain.schemas import CandidateSnapshot, RawJob
from jobhunter.normalize.normalizer import normalize_job

VALID_RESPONSE = {
    "is_it_role": True,
    "seniority": "junior",
    "seniority_reasoning": "Welcomes students and offers mentoring.",
    "location_fit": "exact_city",
    "location_reasoning": "Office in Varna.",
    "experience_fit": "acceptable",
    "mandatory_requirements": [
        {"requirement": "PHP", "candidate_fit": "strong", "evidence": "Laravel internship"},
        {"requirement": "Docker", "candidate_fit": "missing", "evidence": "Not in the profile"},
    ],
    "nice_to_have_requirements": [
        {"requirement": "Vue", "candidate_fit": "unknown", "evidence": ""}
    ],
    "major_strengths": ["PHP", "Laravel"],
    "major_risks": ["No Docker experience"],
    "reasoning": "Strong PHP overlap and a junior-level bar.",
    "decision": "apply",
    "confidence": 0.85,
    "recommendation": "Worth applying - the stack matches closely.",
}


def make_job(description: str = "x" * 600, title: str = "Junior PHP Developer"):
    return normalize_job(
        RawJob(
            source_url="https://www.jobs.bg/job/1",
            title=title,
            company_name="Acme",
            location_raw="Варна",
            description=description,
        )
    )


def make_candidate() -> CandidateSnapshot:
    return CandidateSnapshot(
        full_name="Test Candidate",
        location="Varna",
        years_experience=0.5,
        skills=["php", "javascript"],
        frameworks=["laravel"],
    )


class StubProvider(LocalModelProvider):
    """A provider whose model returns whatever the test says it does."""

    def __init__(self, content: str | Exception, **kwargs) -> None:
        super().__init__(LocalModelConfig(**kwargs))
        self._content = content
        self.calls = 0

    def _chat(self, system: str, user: str) -> tuple[str, int]:
        self.calls += 1
        if isinstance(self._content, Exception):
            raise self._content
        return self._content, 1234


# --------------------------------------------------------------- schema shape


def test_response_schema_avoids_constructs_the_grammar_compiler_rejects():
    """Ollama compiles the schema to GBNF and chokes on $ref/anyOf/maxLength."""
    encoded = json.dumps(_response_schema())
    assert "$ref" not in encoded
    assert "anyOf" not in encoded
    assert "maxLength" not in encoded


def test_response_schema_orders_evidence_before_the_decision():
    """The grammar emits fields in order, so the model must reason first."""
    required = _response_schema()["required"]
    assert required.index("mandatory_requirements") < required.index("decision")
    assert required.index("reasoning") < required.index("decision")


def test_response_schema_bounds_the_arrays():
    """Unbounded arrays run the model past num_predict and truncate the JSON."""
    properties = _response_schema()["properties"]
    assert properties["mandatory_requirements"]["maxItems"] == 5
    assert properties["major_strengths"]["maxItems"] == 3


# -------------------------------------------------------------------- parsing


def test_parses_a_well_formed_response():
    evaluation = LocalModelProvider.parse(json.dumps(VALID_RESPONSE))
    assert evaluation is not None
    assert evaluation.decision is Decision.APPLY
    assert evaluation.confidence == 0.85
    assert evaluation.blocking_gaps[0].requirement == "Docker"


def test_parses_a_response_wrapped_in_a_code_fence():
    fenced = "```json\n" + json.dumps(VALID_RESPONSE) + "\n```"
    assert LocalModelProvider.parse(fenced) is not None


def test_parses_a_response_with_trailing_prose():
    noisy = json.dumps(VALID_RESPONSE) + "\n\nI hope this assessment helps!"
    assert LocalModelProvider.parse(noisy) is not None


@pytest.mark.parametrize(
    "content",
    ["", "   ", "I cannot help with that.", "{not json at all", "[1, 2, 3]", "null"],
)
def test_unusable_output_returns_none_rather_than_a_default(content):
    assert LocalModelProvider.parse(content) is None


def test_recovers_an_evaluation_truncated_mid_string():
    """A response cut off by the token limit still carries usable evidence."""
    truncated = (
        '{"is_it_role": true, "seniority": "junior", "location_fit": "exact_city", '
        '"decision": "review", "confidence": 0.6, "reasoning": "The posting asks for PH'
    )
    evaluation = LocalModelProvider.parse(truncated)
    assert evaluation is not None
    assert evaluation.decision is Decision.REVIEW
    assert evaluation.reasoning.startswith("The posting asks for PH")


def test_closing_open_structures_leaves_valid_json():
    repaired = _close_open_structures('{"a": [1, 2, {"b": "unfinis')
    assert json.loads(repaired)["a"][:2] == [1, 2]


def test_out_of_range_confidence_does_not_discard_the_evaluation():
    """A measured qwen3 response returned confidence 3; the rest was sound."""
    payload = {**VALID_RESPONSE, "confidence": 3}
    evaluation = LocalModelProvider.parse(json.dumps(payload))
    assert evaluation is not None
    assert evaluation.confidence == 0.5, "an uninterpretable value must not imply certainty"


def test_percentage_confidence_is_converted():
    payload = {**VALID_RESPONSE, "confidence": 85}
    evaluation = LocalModelProvider.parse(json.dumps(payload))
    assert evaluation is not None
    assert evaluation.confidence == 0.85


def test_unknown_fields_from_the_model_are_ignored():
    payload = {**VALID_RESPONSE, "salary_estimate": "3000 BGN", "vibe": "good"}
    assert LocalModelProvider.parse(json.dumps(payload)) is not None


def test_an_invalid_enum_value_is_rejected():
    payload = {**VALID_RESPONSE, "decision": "definitely_apply"}
    assert LocalModelProvider.parse(json.dumps(payload)) is None


# ------------------------------------------------------------ safe degrading


def test_a_timeout_degrades_to_review_never_skip_or_apply():
    provider = StubProvider(httpx.TimeoutException("too slow"))
    evaluation = provider.evaluate(make_job(), make_candidate())
    assert evaluation.degraded is True
    assert evaluation.decision is Decision.REVIEW, "a failure says nothing about the job"
    assert "timed out" in (evaluation.degraded_reason or "")


def test_a_transport_error_degrades_safely():
    provider = StubProvider(httpx.ConnectError("no server"))
    evaluation = provider.evaluate(make_job(), make_candidate())
    assert evaluation.degraded is True
    assert evaluation.decision is Decision.REVIEW


def test_unusable_json_degrades_rather_than_inventing_a_match():
    provider = StubProvider("the candidate seems fine to me")
    evaluation = provider.evaluate(make_job(), make_candidate())
    assert evaluation.degraded is True
    assert evaluation.decision is Decision.REVIEW
    assert evaluation.confidence == 0.0


def test_a_successful_evaluation_records_its_provenance():
    provider = StubProvider(json.dumps(VALID_RESPONSE), model="test-model:1b")
    evaluation = provider.evaluate(make_job(), make_candidate())
    assert evaluation.degraded is False
    assert evaluation.model == "test-model:1b"
    assert evaluation.prompt_version and evaluation.prompt_version.startswith("job_evaluation.")
    assert evaluation.job_content_hash
    assert evaluation.latency_ms == 1234


def test_requirement_fit_distinguishes_missing_from_unknown():
    evaluation = LocalModelProvider.parse(json.dumps(VALID_RESPONSE))
    assert evaluation is not None
    fits = {r.requirement: r.candidate_fit for r in evaluation.mandatory_requirements}
    assert fits["Docker"] is Fit.MISSING
    nice = evaluation.nice_to_have_requirements[0]
    assert nice.candidate_fit is Fit.UNKNOWN
    assert nice.candidate_fit not in (Fit.MISSING,)


def test_mandatory_coverage_is_none_when_no_requirements_were_found():
    assert JobEvaluation().mandatory_coverage is None


class TestCoverLetters:
    """A letter that claims what the candidate cannot back up is worse than none."""

    def test_a_letter_comes_back_stripped_of_model_scaffolding(self):
        provider = StubProvider("")
        provider._chat = lambda s, u: ("", 0)  # unused; the letter path has its own client

        from jobhunter.ai.local_model import _strip_letter_furniture

        assert _strip_letter_furniture("```\nDear team,\nI write...\n```") == (
            "Dear team,\nI write..."
        )
        assert _strip_letter_furniture("Sure! Here is the letter:\n\nDear team,") == "Dear team,"

    def test_a_failed_letter_returns_empty_so_the_caller_can_fall_back(self, monkeypatch):
        provider = StubProvider("unused")

        def explode(*args, **kwargs):
            raise httpx.ConnectError("no server")

        monkeypatch.setattr(httpx.Client, "post", explode)
        assert provider.generate_cover_letter(make_job(), make_candidate()) == ""


class TestLegacyInterface:
    def test_the_provider_satisfies_the_ai_provider_contract(self):
        from jobhunter.ai.base import AIProvider

        assert isinstance(StubProvider("{}"), AIProvider)

    def test_score_job_reports_confidence_not_a_match_percentage(self):
        from jobhunter.classify.classifier import classify_job

        provider = StubProvider(json.dumps(VALID_RESPONSE))
        job = make_job()
        result = provider.score_job(job, classify_job(job), make_candidate())

        assert result.score == 85, "the number is the model's confidence"
        assert result.recommendation.value == "apply"
        assert result.missing_skills == ["Docker"]

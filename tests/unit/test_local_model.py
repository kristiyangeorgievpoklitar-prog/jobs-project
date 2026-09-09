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


class TestContextOverflowIsDetected:
    """A prompt that does not fit is truncated silently; it must be logged."""

    @staticmethod
    def _respond(prompt_tokens: int):
        class Response:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "message": {"content": json.dumps(VALID_RESPONSE)},
                    "prompt_eval_count": prompt_tokens,
                }

        return Response()

    def test_a_prompt_that_does_not_fit_is_reported(self, capsys, monkeypatch):
        provider = StubProvider("{}", num_ctx=4096, num_predict=900)
        monkeypatch.setattr(httpx.Client, "post", lambda *a, **k: self._respond(3500))

        content, _ = LocalModelProvider._chat(provider, "system", "user")

        assert "local_model_context_overflow" in capsys.readouterr().out
        assert content, "the reply is still returned; the warning is the point"

    def test_a_prompt_that_fits_is_not_reported(self, capsys, monkeypatch):
        provider = StubProvider("{}", num_ctx=6144, num_predict=900)
        monkeypatch.setattr(httpx.Client, "post", lambda *a, **k: self._respond(3300))

        LocalModelProvider._chat(provider, "system", "user")

        assert "local_model_context_overflow" not in capsys.readouterr().out


class TestVerbosityDoesNotDiscardAnEvaluation:
    """Every schema rejection measured in the benchmark was `string_too_long`."""

    def test_an_over_long_recommendation_is_trimmed_not_rejected(self):
        payload = {**VALID_RESPONSE, "recommendation": "Worth applying. " * 60}
        evaluation = LocalModelProvider.parse(json.dumps(payload))

        assert evaluation is not None, "a verbose model must not lose its whole assessment"
        assert len(evaluation.recommendation) == 300
        assert evaluation.decision is Decision.APPLY

    def test_an_over_long_reasoning_is_trimmed(self):
        payload = {**VALID_RESPONSE, "reasoning": "Because. " * 500}
        evaluation = LocalModelProvider.parse(json.dumps(payload))
        assert evaluation is not None
        assert len(evaluation.reasoning) == 2000

    def test_over_long_requirement_evidence_is_trimmed(self):
        payload = {
            **VALID_RESPONSE,
            "mandatory_requirements": [
                {"requirement": "PHP " * 200, "candidate_fit": "strong", "evidence": "x " * 500}
            ],
        }
        evaluation = LocalModelProvider.parse(json.dumps(payload))
        assert evaluation is not None
        assert len(evaluation.mandatory_requirements[0].requirement) <= 300
        assert len(evaluation.mandatory_requirements[0].evidence) <= 400

    def test_a_genuinely_broken_field_is_still_rejected(self):
        """Leniency is about verbosity, not about meaning."""
        payload = {**VALID_RESPONSE, "seniority": "extremely_senior"}
        assert LocalModelProvider.parse(json.dumps(payload)) is None


class TestThePromptExplainsWhatTheSchemaDemands:
    """The grammar forces every required field; the prompt must ask for them.

    A field present in the schema but absent from the prompt is still emitted —
    the model has no choice — but it is filled blind. Measured, `is_it_role` was
    never mentioned in prompts v2 to v7 and came back false for every single
    listing, including "Junior C++ Developer" and "Junior Software Engineer",
    which made the policy skip all 41 benchmark cases as non-software work.
    """

    def test_every_required_field_is_named_in_the_prompt(self):
        from jobhunter.prompts import job_evaluation_prompt

        prompt = job_evaluation_prompt()
        unexplained = [
            field for field in _response_schema()["required"] if field not in prompt.system
        ]
        assert unexplained == [], (
            f"the schema demands {unexplained} but the prompt never mentions them, "
            "so the model fills them blind"
        )

    def test_the_enum_values_are_offered_to_the_model(self):
        """A constrained field the model cannot see the options for is a guess."""
        from jobhunter.prompts import job_evaluation_prompt

        system = job_evaluation_prompt().system
        for value in ("junior_mid", "mid_senior", "hybrid_city", "other_city", "insufficient"):
            assert value in system, f"{value!r} is a legal answer the prompt never offers"


class TestTheHeadlineIsComposedNotAsked:
    """The model will not write this line, so the code does.

    Asked for a one-line recommendation, qwen2.5:3b returned "Review the
    candidate's profile to determine if they meet the requirements" on nearly
    every listing — instructions for a reader rather than anything about the
    job — and went on doing so after the prompt named that exact phrasing as
    forbidden. It is also the last field generated, so it is the first to be
    truncated when the rest of the answer runs long.
    """

    def test_the_headline_agrees_with_the_decision(self):
        from jobhunter.domain.evaluation import Decision, JobEvaluation

        for decision, expected in (
            (Decision.APPLY, "Worth applying"),
            (Decision.REVIEW, "Worth a look"),
            (Decision.SKIP, "Not worth applying"),
        ):
            evaluation = JobEvaluation(decision=decision, major_strengths=["Laravel matches"])
            assert evaluation.headline().startswith(expected)

    def test_a_skip_leads_with_the_reason_not_the_strength(self):
        from jobhunter.domain.evaluation import Decision, JobEvaluation

        evaluation = JobEvaluation(
            decision=Decision.SKIP,
            major_strengths=["Some PHP overlap"],
            major_risks=["Requires three years of Python the candidate does not have."],
        )
        headline = evaluation.headline()
        assert "three years of Python" in headline
        assert "PHP overlap" not in headline, "a skip must explain itself, not console"

    def test_a_long_model_sentence_is_cut_to_one_line(self):
        from jobhunter.domain.evaluation import Decision, JobEvaluation

        evaluation = JobEvaluation(
            decision=Decision.REVIEW,
            major_strengths=["The candidate has " + "very " * 80 + "relevant experience."],
        )
        assert len(evaluation.headline()) < 200

    def test_it_says_something_even_with_no_strengths_or_risks(self):
        from jobhunter.domain.evaluation import Decision, JobEvaluation

        assert JobEvaluation(decision=Decision.REVIEW).headline() == "Worth a look"

    def test_a_repeated_risk_is_reported_once(self):
        """One measured evaluation listed the same risk three times."""
        from jobhunter.domain.evaluation import JobEvaluation

        evaluation = JobEvaluation(
            major_risks=["No Docker experience", "No Docker experience", "No Kubernetes"]
        )
        assert evaluation.major_risks == ["No Docker experience", "No Kubernetes"]


class TestTheHeadlineReadsAsFinishedText:
    """A line cut mid-word reads as a rendering fault, not a summary."""

    def _headline(self, strength: str, risk: str = "") -> str:
        from jobhunter.domain.evaluation import Decision, JobEvaluation

        return JobEvaluation(
            decision=Decision.APPLY,
            major_strengths=[strength] if strength else [],
            major_risks=[risk] if risk else [],
        ).headline()

    def test_a_long_strength_is_cut_on_a_word_boundary(self):
        """Measured output ended '...which aligns with the J, but'."""
        line = self._headline(
            "The candidate has hands-on experience in software development and web "
            "application modernization, which aligns with the Junior C# Developer role."
        )
        assert "..." in line
        head = line.split("...")[0]
        assert head.endswith(tuple("abcdefghijklmnopqrstuvwxyz")), "cut mid-word"

    def test_a_truncation_keeps_its_ellipsis(self):
        """An earlier version stripped the ellipsis it had just added."""
        line = self._headline("A very long strength " + "indeed " * 40)
        assert line.rstrip().endswith("...")

    def test_a_short_strength_is_left_alone(self):
        line = self._headline("Laravel and MySQL match the stack.")
        assert "..." not in line
        assert line == "Worth applying - Laravel and MySQL match the stack"

    def test_the_line_stays_glanceable(self):
        line = self._headline("x " * 200, "y " * 200)
        assert len(line) < 200

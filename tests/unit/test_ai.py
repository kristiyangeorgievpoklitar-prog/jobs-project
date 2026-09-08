"""AI provider abstraction, fallbacks and data minimisation."""

from __future__ import annotations

import pytest

from jobhunter.ai._json import match_result_from_payload, parse_json_object
from jobhunter.ai.anthropic_provider import AnthropicProvider
from jobhunter.ai.factory import build_provider
from jobhunter.ai.openai_provider import OpenAIProvider
from jobhunter.ai.prompts import build_scoring_prompt, candidate_payload
from jobhunter.ai.rule_based import RuleBasedProvider, display_tech
from jobhunter.classify.classifier import classify_job
from jobhunter.config import Settings
from jobhunter.domain.enums import Language, Recommendation
from jobhunter.matching.rules import ScoringConfig
from tests.conftest import make_job


class TestParseJsonObject:
    def test_plain_json(self) -> None:
        assert parse_json_object('{"score": 91}') == {"score": 91}

    def test_markdown_fenced(self) -> None:
        assert parse_json_object('```json\n{"score": 91}\n```') == {"score": 91}

    def test_json_surrounded_by_prose(self) -> None:
        assert parse_json_object('Here: {"score": 42} thanks') == {"score": 42}

    def test_rejects_non_object(self) -> None:
        with pytest.raises(ValueError):
            parse_json_object("[1, 2]")

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError):
            parse_json_object("")


class TestMatchResultFromPayload:
    def _baseline(self, candidate):
        job = make_job(location_raw="Варна")
        classification = classify_job(job, target_locations=["Varna"])
        return RuleBasedProvider().score_job(job, classification, candidate)

    def test_uses_model_values(self, candidate) -> None:
        baseline = self._baseline(candidate)
        result = match_result_from_payload(
            {"score": 88, "confidence": 0.8, "strengths": ["PHP"], "missing_skills": ["Docker"]},
            provider="anthropic",
            model="m",
            config=ScoringConfig(),
            baseline=baseline,
        )
        assert result.score == 88
        assert result.strengths == ["PHP"]
        assert result.provider == "anthropic"

    def test_clamps_out_of_range_score(self, candidate) -> None:
        baseline = self._baseline(candidate)
        result = match_result_from_payload(
            {"score": 5000}, provider="p", model=None, config=ScoringConfig(), baseline=baseline
        )
        assert result.score == 100

    def test_rule_disqualifiers_are_never_dropped(self, candidate) -> None:
        """A model must not be able to talk the system past a hard blocker."""
        job = make_job(title="Шофьор", location_raw="София", tech_tags=[])
        classification = classify_job(job, target_locations=["Varna"])
        baseline = RuleBasedProvider().score_job(job, classification, candidate)
        assert baseline.disqualifiers

        result = match_result_from_payload(
            {"score": 99, "disqualifiers": []},
            provider="p",
            model=None,
            config=ScoringConfig(),
            baseline=baseline,
        )
        assert result.disqualifiers
        assert result.recommendation is Recommendation.SKIP

    def test_malformed_fields_fall_back(self, candidate) -> None:
        baseline = self._baseline(candidate)
        result = match_result_from_payload(
            {"score": "not a number", "confidence": None},
            provider="p",
            model=None,
            config=ScoringConfig(),
            baseline=baseline,
        )
        assert result.score == baseline.score


class TestRuleBasedProvider:
    def test_is_always_available(self) -> None:
        assert RuleBasedProvider().is_available() is True

    def test_cover_letter_only_uses_profile_facts(self, candidate) -> None:
        job = make_job(title="Junior Laravel Developer", tech_tags=["Laravel", "Docker"])
        letter = RuleBasedProvider().generate_cover_letter(job, candidate, language=Language.EN)
        assert "Laravel" in letter
        # Docker is not in the candidate profile and must not be claimed.
        assert "Docker" not in letter
        assert candidate.full_name in letter

    def test_bulgarian_letter(self, candidate) -> None:
        job = make_job(title="Junior Laravel Developer")
        letter = RuleBasedProvider().generate_cover_letter(job, candidate, language=Language.BG)
        assert "Уважаеми" in letter

    def test_bulgarian_letter_does_not_embed_an_english_summary(self, candidate) -> None:
        """A summary written in English must not leak into a Bulgarian letter."""
        candidate.summary = "Computer Science student with hands-on experience in development."
        job = make_job(title="Стажант WordPress", company_name="Фирма ООД")
        letter = RuleBasedProvider().generate_cover_letter(job, candidate, language=Language.BG)
        assert "Computer Science student" not in letter
        assert "Уважаеми" in letter

    def test_english_letter_keeps_an_english_summary(self, candidate) -> None:
        candidate.summary = "Computer Science student with hands-on experience in development."
        letter = RuleBasedProvider().generate_cover_letter(
            make_job(), candidate, language=Language.EN
        )
        assert "Computer Science student" in letter

    def test_respects_word_budget(self, candidate) -> None:
        job = make_job(title="Junior Laravel Developer")
        letter = RuleBasedProvider().generate_cover_letter(job, candidate, max_words=40)
        assert len(letter.split()) <= 41

    def test_display_tech_casing(self) -> None:
        assert display_tech("php") == "PHP"
        assert display_tech("javascript") == "JavaScript"
        assert display_tech("laravel") == "Laravel"


class TestProviderFallbacks:
    def test_anthropic_without_key_falls_back_to_rules(self, candidate) -> None:
        provider = AnthropicProvider(None)
        assert provider.is_available() is False
        job = make_job(location_raw="Варна")
        classification = classify_job(job, target_locations=["Varna"])
        result = provider.score_job(job, classification, candidate)
        assert result.provider == "rule_based"

    def test_openai_without_key_falls_back_to_rules(self, candidate) -> None:
        provider = OpenAIProvider(None)
        assert provider.is_available() is False
        job = make_job(location_raw="Варна")
        classification = classify_job(job, target_locations=["Varna"])
        assert provider.score_job(job, classification, candidate).provider == "rule_based"

    def test_cover_letter_falls_back(self, candidate) -> None:
        letter = AnthropicProvider(None).generate_cover_letter(make_job(), candidate)
        assert len(letter) > 50


class TestFactory:
    def test_auto_with_no_keys_selects_rules(self, tmp_path) -> None:
        settings = Settings(_env_file=None, data_dir=tmp_path, ai_provider="auto")
        assert build_provider(settings).name == "rule_based"

    def test_explicit_rule_based(self, tmp_path) -> None:
        settings = Settings(_env_file=None, data_dir=tmp_path, ai_provider="rule_based")
        assert build_provider(settings).name == "rule_based"

    def test_requested_provider_without_key_degrades(self, tmp_path) -> None:
        settings = Settings(_env_file=None, data_dir=tmp_path, ai_provider="anthropic")
        assert build_provider(settings).name == "rule_based"


class TestDataMinimisation:
    def test_payload_excludes_direct_identifiers(self, candidate) -> None:
        payload = candidate_payload(candidate)
        for forbidden in ("full_name", "email", "phone", "location"):
            assert forbidden not in payload

    def test_education_is_reduced_to_a_level(self, candidate) -> None:
        candidate.education = [
            {"institution": "University of Economics - Varna", "degree": "Bachelor"}
        ]
        payload = candidate_payload(candidate)
        assert payload["education_level"] == "bachelor"
        assert "University" not in str(payload["education_level"])

    def test_scoring_prompt_truncates_description(self, candidate) -> None:
        job = make_job(description="x" * 50_000)
        classification = classify_job(job, target_locations=["Varna"])
        prompt = build_scoring_prompt(job, classification, candidate, max_description_chars=500)
        assert prompt.count("x") <= 600

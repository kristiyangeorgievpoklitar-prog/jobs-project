"""Anthropic-backed scoring and cover-letter generation.

Any failure degrades to the deterministic rule engine rather than breaking a
scan, so a missing key or a rate limit never costs a run.
"""

from __future__ import annotations

from typing import Any

from jobhunter.ai import prompts
from jobhunter.ai._json import match_result_from_payload, parse_json_object
from jobhunter.ai.base import AIProvider
from jobhunter.ai.rule_based import RuleBasedProvider
from jobhunter.domain.enums import Language
from jobhunter.domain.schemas import (
    CandidateSnapshot,
    ClassificationResult,
    MatchResult,
    NormalizedJob,
)
from jobhunter.logging_setup import get_logger
from jobhunter.matching.rules import ScoringConfig

log = get_logger(__name__)

DEFAULT_MODEL = "claude-opus-5"


class AnthropicProvider(AIProvider):
    name = "anthropic"

    def __init__(
        self,
        api_key: str | None,
        *,
        model: str = DEFAULT_MODEL,
        timeout: float = 60.0,
        config: ScoringConfig | None = None,
        max_description_chars: int = 6000,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.config = config or ScoringConfig()
        self.max_description_chars = max_description_chars
        self._fallback = RuleBasedProvider(self.config)
        self._client: Any | None = None

    def _get_client(self) -> Any | None:
        if self._client is not None:
            return self._client
        if not self.api_key:
            return None
        try:
            import anthropic
        except ImportError:
            log.warning("anthropic_sdk_missing")
            return None
        self._client = anthropic.Anthropic(api_key=self.api_key, timeout=self.timeout)
        return self._client

    def is_available(self) -> bool:
        return bool(self.api_key) and self._get_client() is not None

    def _complete(self, system: str, user: str, *, max_tokens: int = 2000) -> str:
        client = self._get_client()
        if client is None:
            raise RuntimeError("Anthropic client unavailable")

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        try:
            response = client.messages.create(**kwargs, output_config={"effort": "low"})
        except TypeError:
            # Older SDKs do not accept output_config.
            response = client.messages.create(**kwargs)

        if getattr(response, "stop_reason", None) == "refusal":
            raise RuntimeError("model declined the request")

        return "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        ).strip()

    def score_job(
        self,
        job: NormalizedJob,
        classification: ClassificationResult,
        candidate: CandidateSnapshot,
    ) -> MatchResult:
        baseline = self._fallback.score_job(job, classification, candidate)
        if not self.is_available():
            return baseline
        try:
            raw = self._complete(
                prompts.SCORING_SYSTEM_PROMPT,
                prompts.build_scoring_prompt(
                    job,
                    classification,
                    candidate,
                    max_description_chars=self.max_description_chars,
                ),
            )
            payload = parse_json_object(raw)
        except Exception as exc:
            log.warning(
                "ai_scoring_failed", provider=self.name, error=str(exc), job=job.fingerprint
            )
            return baseline
        return match_result_from_payload(
            payload,
            provider=self.name,
            model=self.model,
            config=self.config,
            baseline=baseline,
        )

    def generate_cover_letter(
        self,
        job: NormalizedJob,
        candidate: CandidateSnapshot,
        *,
        language: Language = Language.EN,
        max_words: int = 180,
    ) -> str:
        if not self.is_available():
            return self._fallback.generate_cover_letter(
                job, candidate, language=language, max_words=max_words
            )
        try:
            return self._complete(
                prompts.COVER_LETTER_SYSTEM_PROMPT,
                prompts.build_cover_letter_prompt(
                    job, candidate, language=language, max_words=max_words
                ),
                max_tokens=1500,
            )
        except Exception as exc:
            log.warning("ai_cover_letter_failed", provider=self.name, error=str(exc))
            return self._fallback.generate_cover_letter(
                job, candidate, language=language, max_words=max_words
            )

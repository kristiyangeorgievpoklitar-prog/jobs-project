"""Provider selection.

``auto`` prefers a configured cloud provider and silently uses the rule engine
when none is available, so the app runs with zero credentials.
"""

from __future__ import annotations

from jobhunter.ai.anthropic_provider import AnthropicProvider
from jobhunter.ai.base import AIProvider
from jobhunter.ai.openai_provider import OpenAIProvider
from jobhunter.ai.rule_based import RuleBasedProvider
from jobhunter.config import Settings
from jobhunter.logging_setup import get_logger
from jobhunter.matching.rules import ScoringConfig

log = get_logger(__name__)


def scoring_config_from_settings(settings: Settings) -> ScoringConfig:
    from jobhunter.domain.enums import Seniority

    return ScoringConfig(
        auto_apply_threshold=settings.auto_apply_threshold,
        review_threshold=settings.review_threshold,
        max_seniority=Seniority(settings.max_seniority),
    )


def build_provider(settings: Settings, config: ScoringConfig | None = None) -> AIProvider:
    """Instantiate the configured provider, falling back to rules."""
    config = config or scoring_config_from_settings(settings)

    anthropic_key = (
        settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
    )
    openai_key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else None

    choice = settings.ai_provider

    if choice == "rule_based":
        return RuleBasedProvider(config)

    if choice == "anthropic":
        provider: AIProvider = AnthropicProvider(
            anthropic_key,
            model=settings.anthropic_model,
            timeout=settings.ai_timeout_seconds,
            config=config,
            max_description_chars=settings.ai_max_description_chars,
        )
        if provider.is_available():
            return provider
        log.warning("ai_provider_unavailable", requested="anthropic", using="rule_based")
        return RuleBasedProvider(config)

    if choice == "openai":
        provider = OpenAIProvider(
            openai_key,
            model=settings.openai_model,
            timeout=settings.ai_timeout_seconds,
            config=config,
            max_description_chars=settings.ai_max_description_chars,
        )
        if provider.is_available():
            return provider
        log.warning("ai_provider_unavailable", requested="openai", using="rule_based")
        return RuleBasedProvider(config)

    # auto
    candidate: AIProvider
    if anthropic_key:
        candidate = AnthropicProvider(
            anthropic_key,
            model=settings.anthropic_model,
            timeout=settings.ai_timeout_seconds,
            config=config,
            max_description_chars=settings.ai_max_description_chars,
        )
        if candidate.is_available():
            log.info("ai_provider_selected", provider="anthropic", model=settings.anthropic_model)
            return candidate
    if openai_key:
        candidate = OpenAIProvider(
            openai_key,
            model=settings.openai_model,
            timeout=settings.ai_timeout_seconds,
            config=config,
            max_description_chars=settings.ai_max_description_chars,
        )
        if candidate.is_available():
            log.info("ai_provider_selected", provider="openai", model=settings.openai_model)
            return candidate

    log.info("ai_provider_selected", provider="rule_based")
    return RuleBasedProvider(config)

"""Cover letter generation."""

from __future__ import annotations

from jobhunter.ai.base import AIProvider
from jobhunter.ai.local_model import LocalModelProvider
from jobhunter.domain.enums import Language
from jobhunter.domain.schemas import CandidateSnapshot, NormalizedJob
from jobhunter.logging_setup import get_logger

log = get_logger(__name__)


def choose_language(job: NormalizedJob, candidate: CandidateSnapshot) -> Language:
    """Pick the letter language from the posting, defaulting to English.

    A Bulgarian-language posting gets a Bulgarian letter only if the candidate
    actually lists Bulgarian.
    """
    speaks_bg = any(
        str(item.get("name", "")).strip().lower() in {"bulgarian", "български"}
        for item in candidate.languages
    )
    if job.language is Language.BG and (speaks_bg or not candidate.languages):
        return Language.BG
    return Language.EN


def generate(
    provider: AIProvider,
    job: NormalizedJob,
    candidate: CandidateSnapshot,
    *,
    max_words: int = 180,
    language: Language | None = None,
    cv_text: str | None = None,
) -> tuple[str, Language]:
    """Generate a cover letter, falling back to the template on any error.

    The fallback is deliberate and not a failure mode to be avoided: the
    deterministic writer only ever assembles facts from the profile, so an empty
    or failed model response degrades into something honest rather than into
    something fluent and untrue.
    """
    chosen = language or choose_language(job, candidate)
    text = ""

    try:
        if isinstance(provider, LocalModelProvider):
            text = provider.generate_cover_letter(
                job, candidate, language=chosen, max_words=max_words, cv_text=cv_text
            )
        else:
            text = provider.generate_cover_letter(
                job, candidate, language=chosen, max_words=max_words
            )
    except Exception as exc:
        log.warning("cover_letter_generation_failed", error=str(exc))

    if not text.strip():
        from jobhunter.ai.rule_based import RuleBasedProvider

        log.info("cover_letter_fell_back_to_template")
        text = RuleBasedProvider().generate_cover_letter(
            job, candidate, language=chosen, max_words=max_words
        )

    return text.strip(), chosen

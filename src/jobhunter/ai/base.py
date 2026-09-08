"""Provider-agnostic AI interface.

The pipeline depends only on this protocol, so swapping providers (or running
with none at all) never touches calling code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from jobhunter.domain.enums import Language
from jobhunter.domain.schemas import (
    CandidateSnapshot,
    ClassificationResult,
    MatchResult,
    NormalizedJob,
)


class AIProviderError(RuntimeError):
    """Raised when a provider fails in a way the caller should know about."""


class AIProvider(ABC):
    """Scores jobs and writes cover letters."""

    name: str = "base"
    model: str | None = None

    @abstractmethod
    def is_available(self) -> bool:
        """Whether this provider is configured and usable right now."""

    @abstractmethod
    def score_job(
        self,
        job: NormalizedJob,
        classification: ClassificationResult,
        candidate: CandidateSnapshot,
    ) -> MatchResult:
        """Return a 0-100 match assessment."""

    @abstractmethod
    def generate_cover_letter(
        self,
        job: NormalizedJob,
        candidate: CandidateSnapshot,
        *,
        language: Language = Language.EN,
        max_words: int = 180,
    ) -> str:
        """Write a job-specific cover letter grounded in the profile."""

    def describe(self) -> str:
        return f"{self.name}({self.model})" if self.model else self.name

"""Classification + scoring for one job, behind a single call."""

from __future__ import annotations

from jobhunter.ai.base import AIProvider
from jobhunter.classify.classifier import classify_job
from jobhunter.domain.schemas import (
    CandidateSnapshot,
    ClassificationResult,
    MatchResult,
    NormalizedJob,
)
from jobhunter.logging_setup import get_logger
from jobhunter.matching.rules import ScoringConfig

log = get_logger(__name__)


class MatchingEngine:
    """Runs the classifier, then the configured AI provider, over a job."""

    def __init__(self, provider: AIProvider, config: ScoringConfig | None = None) -> None:
        self.provider = provider
        self.config = config or ScoringConfig()

    def classify(
        self,
        job: NormalizedJob,
        candidate: CandidateSnapshot,
    ) -> ClassificationResult:
        return classify_job(
            job,
            target_locations=candidate.preferred_locations
            or ([candidate.location] if candidate.location else []),
            remote_ok=candidate.remote_ok,
        )

    def score(
        self,
        job: NormalizedJob,
        classification: ClassificationResult,
        candidate: CandidateSnapshot,
    ) -> MatchResult:
        try:
            return self.provider.score_job(job, classification, candidate)
        except Exception as exc:
            # A provider must never take down a scan.
            log.warning(
                "scoring_provider_error",
                provider=self.provider.name,
                error=str(exc),
                job=job.fingerprint,
            )
            from jobhunter.ai.rule_based import RuleBasedProvider

            return RuleBasedProvider(self.config).score_job(job, classification, candidate)

    def evaluate(
        self,
        job: NormalizedJob,
        candidate: CandidateSnapshot,
    ) -> tuple[ClassificationResult, MatchResult]:
        classification = self.classify(job, candidate)
        return classification, self.score(job, classification, candidate)

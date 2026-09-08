"""Running matchers over the labelled set.

Every matcher is wrapped to the same interface and produces the same
:class:`JobEvaluation`, so the old scorer and the local model are measured by
identical code on identical inputs. That is the only way the comparison means
anything.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

from jobhunter.ai.local_model import LocalModelConfig, LocalModelProvider
from jobhunter.classify.classifier import classify_job
from jobhunter.domain.enums import Recommendation, WorkMode
from jobhunter.domain.evaluation import Decision, JobEvaluation, LocationFit
from jobhunter.domain.schemas import CandidateSnapshot
from jobhunter.evaluation.dataset import BenchmarkCase, Dataset
from jobhunter.evaluation.metrics import BenchmarkReport, build_outcome
from jobhunter.logging_setup import get_logger
from jobhunter.matching.rules import ScoringConfig, score_job

log = get_logger(__name__)


class Matcher(Protocol):
    """Anything that can turn a case into an evaluation."""

    name: str

    def evaluate_case(self, case: BenchmarkCase) -> JobEvaluation: ...


class LegacyMatcher:
    """The original classifier + weighted scorer, as the baseline to beat."""

    name = "legacy_rule_score"

    def __init__(self, candidate: CandidateSnapshot, config: ScoringConfig | None = None) -> None:
        self.candidate = candidate
        self.config = config or ScoringConfig()

    def evaluate_case(self, case: BenchmarkCase) -> JobEvaluation:
        job = case.to_normalized_job()
        started = time.monotonic()
        classification = classify_job(
            job,
            target_locations=self.candidate.preferred_locations
            or ([self.candidate.location] if self.candidate.location else []),
            remote_ok=self.candidate.remote_ok,
        )
        match = score_job(job, classification, self.candidate, self.config)
        latency_ms = int((time.monotonic() - started) * 1000)

        return JobEvaluation(
            decision={
                Recommendation.APPLY: Decision.APPLY,
                Recommendation.REVIEW: Decision.REVIEW,
                Recommendation.SKIP: Decision.SKIP,
            }[match.recommendation],
            confidence=match.confidence,
            is_it_role=classification.is_it,
            seniority=classification.seniority,
            location_fit=_legacy_location(classification.location_relevant, job.work_mode),
            major_strengths=match.strengths[:4],
            major_risks=match.disqualifiers[:4],
            reasoning=match.reasoning or "",
            recommendation=f"score {match.score}",
            source=self.name,
            rank_score=match.score / 100,
            latency_ms=latency_ms,
        )


def _legacy_location(relevant: bool | None, work_mode: WorkMode) -> LocationFit:
    """Best-effort mapping of the old boolean onto the new location taxonomy."""
    if relevant is None:
        return LocationFit.UNCLEAR
    if not relevant:
        return LocationFit.OTHER_CITY
    if work_mode is WorkMode.REMOTE:
        return LocationFit.REMOTE
    if work_mode is WorkMode.HYBRID:
        return LocationFit.HYBRID_CITY
    return LocationFit.EXACT_CITY


class AlwaysSkipMatcher:
    """The degenerate baseline: refuse everything.

    Included because most listings genuinely are skips, so plain accuracy flatters
    any cautious matcher. Reporting this alongside the real ones makes it obvious
    how much of a matcher's score is actual skill.
    """

    name = "always_skip_baseline"

    def evaluate_case(self, case: BenchmarkCase) -> JobEvaluation:
        return JobEvaluation(
            decision=Decision.SKIP,
            confidence=1.0,
            reasoning="Baseline that skips every listing.",
            source=self.name,
            latency_ms=0,
        )


class LocalModelMatcher:
    """The local instruct model."""

    def __init__(
        self,
        candidate: CandidateSnapshot,
        config: LocalModelConfig,
        *,
        cv_text: str | None = None,
        label: str | None = None,
    ) -> None:
        self.provider = LocalModelProvider(config)
        self.candidate = candidate
        self.cv_text = cv_text
        self.name = label or f"local:{config.model}"

    def evaluate_case(self, case: BenchmarkCase) -> JobEvaluation:
        return self.provider.evaluate(
            case.to_normalized_job(), self.candidate, cv_text=self.cv_text
        )


def run_benchmark(
    matcher: Matcher,
    dataset: Dataset,
    *,
    progress: Callable[[int, int, BenchmarkCase], None] | None = None,
) -> BenchmarkReport:
    """Run one matcher over every case and collect the outcomes."""
    report = BenchmarkReport(matcher=matcher.name)

    for index, case in enumerate(dataset.cases, start=1):
        if progress is not None:
            progress(index, len(dataset.cases), case)
        try:
            evaluation = matcher.evaluate_case(case)
        except Exception as exc:  # a broken matcher must not lose the whole run
            log.warning("benchmark_case_failed", case=case.id, error=str(exc))
            evaluation = JobEvaluation(
                decision=Decision.REVIEW,
                degraded=True,
                degraded_reason=f"{type(exc).__name__}: {exc}",
                source=matcher.name,
            )
        report.outcomes.append(build_outcome(case, evaluation, matcher.name))

    return report

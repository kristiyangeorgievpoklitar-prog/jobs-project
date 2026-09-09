"""Running matchers over the labelled set.

Every matcher is wrapped to the same interface and produces the same
:class:`JobEvaluation`, so the old scorer and the local model are measured by
identical code on identical inputs. That is the only way the comparison means
anything.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from jobhunter.ai.local_model import LocalModelConfig, LocalModelProvider
from jobhunter.classify.classifier import classify_job
from jobhunter.domain.enums import Recommendation, WorkMode
from jobhunter.domain.evaluation import Decision, JobEvaluation, LocationFit
from jobhunter.domain.schemas import CandidateSnapshot
from jobhunter.evaluation.dataset import BenchmarkCase, Dataset
from jobhunter.evaluation.metrics import BenchmarkReport, build_outcome
from jobhunter.logging_setup import get_logger
from jobhunter.matching.policy import apply_policy
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


class AlwaysReviewMatcher:
    """The other degenerate baseline: show the candidate everything.

    It scores 100% worth-surfacing recall and zero harmful errors, because it
    never declines anything — which is exactly why it is here. A matcher that
    hedges every listing produces those same two headline numbers while doing
    none of the work, and llama3.2:3b was measured answering REVIEW to 13 of 14
    cases. Without this row that result reads as excellent.
    """

    name = "always_review_baseline"

    def evaluate_case(self, case: BenchmarkCase) -> JobEvaluation:
        return JobEvaluation(
            decision=Decision.REVIEW,
            confidence=0.5,
            reasoning="Baseline that reviews every listing.",
            source=self.name,
            latency_ms=0,
        )


class MemoisingMatcher:
    """Runs the wrapped matcher once per case and remembers the answer.

    Two reasons, both measured. Reporting the raw and the policy-applied numbers
    would otherwise mean two full inference passes — an extra hour for a result
    that is a pure function of the first. And with ``cache_path`` set, each
    answer is written to disk as it lands, so an interrupted run resumes instead
    of starting over: a full pass is around two hours on this hardware, and one
    was lost outright to a shell timeout before this existed.
    """

    def __init__(self, inner: Matcher, cache_path: Path | None = None) -> None:
        self.inner = inner
        self.name = inner.name
        self.cache_path = cache_path
        self._answers: dict[str, JobEvaluation] = {}

        if cache_path is not None and cache_path.exists():
            stored = json.loads(cache_path.read_text(encoding="utf-8"))
            self._answers = {
                case_id: JobEvaluation.model_validate(payload)
                for case_id, payload in stored.items()
            }
            log.info("benchmark_cache_loaded", cases=len(self._answers), path=str(cache_path))

    @property
    def resumed(self) -> int:
        """How many answers came from a previous run."""
        return len(self._answers)

    def evaluate_case(self, case: BenchmarkCase) -> JobEvaluation:
        if case.id not in self._answers:
            self._answers[case.id] = self.inner.evaluate_case(case)
            self._persist()
        return self._answers[case.id]

    def _persist(self) -> None:
        if self.cache_path is None:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            case_id: json.loads(evaluation.model_dump_json())
            for case_id, evaluation in self._answers.items()
        }
        self.cache_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )


class PolicyMatcher:
    """Another matcher, plus the safety rules the pipeline actually applies.

    The model's raw output is the right thing to measure when *choosing* a
    model, but it is not what the candidate sees: in the pipeline every
    evaluation passes through :func:`apply_policy`, which downgrades what the
    evidence cannot carry. Reporting only the raw numbers would understate the
    product; reporting only the policy-applied numbers would hide which part
    earned the result. So both are measured.
    """

    def __init__(self, inner: Matcher, candidate: CandidateSnapshot, policy=None) -> None:
        self.inner = inner
        self.candidate = candidate
        self.policy = policy
        self.name = f"{inner.name} + policy"

    def evaluate_case(self, case: BenchmarkCase) -> JobEvaluation:
        evaluation = self.inner.evaluate_case(case)
        outcome = apply_policy(evaluation, case.to_normalized_job(), self.candidate, self.policy)
        return outcome.evaluation


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

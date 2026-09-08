"""The two-stage matcher: cheap filter, then local model, then policy.

This is the replacement for the old weighted score as the system's decision
path. The ordering is the whole design:

1. the gate removes what no reading could rescue, cheaply;
2. the cache answers for anything already judged on identical inputs;
3. the model reads what is left;
4. the policy refuses to endorse what the evidence does not support.

Stage 3 is the only expensive step, and stages 1 and 2 exist to keep it rare.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from jobhunter.ai.local_model import SCHEMA_VERSION, LocalModelProvider
from jobhunter.domain.evaluation import Decision, JobEvaluation, LocationFit
from jobhunter.domain.schemas import CandidateSnapshot, NormalizedJob
from jobhunter.logging_setup import get_logger
from jobhunter.matching.gate import GateResult, Stage1Gate
from jobhunter.matching.job_context import job_content_hash
from jobhunter.matching.policy import DecisionPolicy, apply_policy
from jobhunter.pipeline import evaluation_store
from jobhunter.profile.context import candidate_fingerprint

log = get_logger(__name__)


@dataclass
class EvaluationStats:
    """Where a scan's decisions came from, for the run summary and the logs."""

    gated: int = 0
    cached: int = 0
    evaluated: int = 0
    degraded: int = 0
    downgraded: int = 0
    gate_reasons: dict[str, int] = field(default_factory=dict)

    def record_gate(self, reason: str | None) -> None:
        self.gated += 1
        key = reason or "unknown"
        self.gate_reasons[key] = self.gate_reasons.get(key, 0) + 1

    @property
    def model_calls_avoided(self) -> int:
        return self.gated + self.cached


def gate_evaluation(job: NormalizedJob, result: GateResult) -> JobEvaluation:
    """A SKIP that records why the model was never asked."""
    return JobEvaluation(
        decision=Decision.SKIP,
        confidence=0.9,
        recommendation=result.detail or "Filtered out before analysis.",
        reasoning=result.detail or "Removed by the first-pass filter.",
        location_fit=LocationFit.UNCLEAR,
        source=f"gate:{result.reason}",
        job_content_hash=job_content_hash(job),
    )


class JobEvaluator:
    """Evaluates one job, spending a model call only when it is warranted."""

    def __init__(
        self,
        provider: LocalModelProvider,
        *,
        gate: Stage1Gate | None = None,
        policy: DecisionPolicy | None = None,
    ) -> None:
        self.provider = provider
        self.gate = gate or Stage1Gate()
        self.policy = policy or DecisionPolicy()

    def evaluate(
        self,
        session,
        job_id: int,
        job: NormalizedJob,
        candidate: CandidateSnapshot,
        *,
        cv_text: str | None = None,
        rejected_fingerprints: set[str] | None = None,
        stats: EvaluationStats | None = None,
        force: bool = False,
    ) -> JobEvaluation:
        stats = stats or EvaluationStats()
        fingerprint = candidate_fingerprint(candidate, cv_text)

        # --- stage 1: the cheap filter
        gate_result = self.gate.check(job, candidate, rejected_fingerprints=rejected_fingerprints)
        if not gate_result.passed:
            stats.record_gate(gate_result.reason)
            evaluation = gate_evaluation(job, gate_result)
            evaluation_store.store(session, job_id, evaluation, candidate_fingerprint=fingerprint)
            return evaluation

        # --- stage 2: an identical question already answered
        content_hash = job_content_hash(job)
        if not force:
            cached = evaluation_store.find_cached(
                session,
                job_content_hash=content_hash,
                candidate_fingerprint=fingerprint,
                model=self.provider.config.model,
                prompt_version=self.provider.prompt.identity,
                schema_version=SCHEMA_VERSION,
            )
            if cached is not None:
                stats.cached += 1
                evaluation = evaluation_store.to_domain(cached)
                # If the cached row already *is* this job's current evaluation,
                # there is nothing to write. Storing anyway would append a
                # duplicate row on every re-scan of an unchanged listing, which
                # over a months-long job hunt is most of what the table holds.
                if not (cached.job_id == job_id and cached.is_current):
                    evaluation_store.store(
                        session, job_id, evaluation, candidate_fingerprint=fingerprint
                    )
                return evaluation

        # --- stage 3: the model
        #
        # Commit first, and deliberately. Inference takes minutes, and SQLite
        # allows exactly one writer at a time: holding the caller's write
        # transaction open across the call makes every other write fail with
        # "database is locked" after the busy timeout. Measured, a dashboard
        # click during a scan failed in 5.0s. WAL lets a reader coexist with a
        # writer, so releasing the write lock here is enough.
        #
        # It also means a listing already recorded stays recorded if the model
        # call dies: the job is durable, only its evaluation is missing, and
        # `jobhunter evaluate` picks that up later.
        session.commit()

        evaluation = self.provider.evaluate(job, candidate, cv_text=cv_text)
        stats.evaluated += 1
        if evaluation.degraded:
            stats.degraded += 1

        # --- stage 4: what the system is willing to stand behind
        outcome = apply_policy(evaluation, job, candidate, self.policy)
        if outcome.changed:
            stats.downgraded += 1
            log.info(
                "evaluation_downgraded",
                job=job.fingerprint,
                to=outcome.evaluation.decision.value,
                reasons=outcome.adjustments,
            )
            final = outcome.evaluation.model_copy(
                update={
                    "reasoning": _append_adjustments(
                        outcome.evaluation.reasoning, outcome.adjustments
                    )
                }
            )
        else:
            final = outcome.evaluation

        evaluation_store.store(session, job_id, final, candidate_fingerprint=fingerprint)
        return final


def _append_adjustments(reasoning: str, adjustments: list[str]) -> str:
    """Make a policy downgrade visible in the explanation the candidate reads."""
    if not adjustments:
        return reasoning
    note = "Downgraded because " + "; ".join(adjustments) + "."
    return f"{reasoning} {note}".strip()

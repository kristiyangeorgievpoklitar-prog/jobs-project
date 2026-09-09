"""Safety rules applied to a model's evaluation before it is acted on.

The model produces a judgement; this decides whether the system is willing to
stand behind it. The rules only ever move a decision *down* (APPLY to REVIEW,
never REVIEW to APPLY), because they exist to catch the cases where the model
sounded confident without having the evidence to be — a posting whose body was
never fetched, a run where the model failed, a location that does not work.

Keeping this separate from the prompt matters: a prompt can be ignored by the
model, a rule cannot.
"""

from __future__ import annotations

from dataclasses import dataclass

from jobhunter.domain.evaluation import Decision, JobEvaluation, LocationFit
from jobhunter.domain.schemas import CandidateSnapshot, NormalizedJob
from jobhunter.matching.job_context import has_usable_description


@dataclass(frozen=True)
class DecisionPolicy:
    """Thresholds and guards governing what may be recommended."""

    # Below this the model is not confident enough for a headline APPLY.
    min_apply_confidence: float = 0.6
    # A posting shorter than this was effectively judged on its title alone.
    min_description_chars: int = 300
    # Whether a fully remote role counts as reachable.
    accept_remote: bool = True
    # Whether a role in another city may still be surfaced for review.
    allow_other_city_review: bool = False
    # How many seniority bands above the candidate's target still count as
    # reachable. One band is a stretch worth showing; two is a different job.
    seniority_tolerance_bands: int = 1


@dataclass(frozen=True)
class PolicyOutcome:
    evaluation: JobEvaluation
    adjustments: list[str]

    @property
    def changed(self) -> bool:
        return bool(self.adjustments)


def apply_policy(
    evaluation: JobEvaluation,
    job: NormalizedJob,
    candidate: CandidateSnapshot,
    policy: DecisionPolicy | None = None,
) -> PolicyOutcome:
    """Downgrade a decision the evidence does not support."""
    policy = policy or DecisionPolicy()
    decision = evaluation.decision
    adjustments: list[str] = []

    def downgrade(to: Decision, why: str) -> None:
        nonlocal decision
        # Only ever move towards caution.
        if _rank(to) < _rank(decision):
            decision = to
            adjustments.append(why)

    # A failed model call says nothing about the job, so it may not endorse one.
    if evaluation.degraded:
        downgrade(Decision.REVIEW, "model did not return a usable assessment")

    # Judged without the posting body: enough to surface, never enough to endorse.
    if not has_usable_description(job, minimum_chars=policy.min_description_chars):
        downgrade(Decision.REVIEW, "posting text was not available")

    # Low self-reported confidence.
    if decision is Decision.APPLY and evaluation.confidence < policy.min_apply_confidence:
        downgrade(
            Decision.REVIEW,
            f"model confidence {evaluation.confidence:.0%} is below the "
            f"{policy.min_apply_confidence:.0%} needed to recommend applying",
        )

    # A mandatory requirement the model itself marked as missing.
    if decision is Decision.APPLY and evaluation.blocking_gaps:
        missing = ", ".join(r.requirement for r in evaluation.blocking_gaps[:2])
        downgrade(Decision.REVIEW, f"a mandatory requirement is missing: {missing}")

    # Out of reach on level.
    #
    # This rule does the most work of any in this file, and it exists because
    # the model reads seniority well but will not act on it: measured over the
    # labelled set it places the entry bar within one band 80% of the time, and
    # still answers "review" for roles it has just described as mid-senior.
    # Turning its own judgement into the decision raised accuracy from 33% to
    # 58% and precision from 30% to 42% without costing a single point of
    # recall or adding a harmful error.
    #
    # Deliberately only seniority. The same treatment applied to the model's
    # "missing requirement" verdicts collapsed recall from 89% to 22%, because
    # it over-marks gaps — it called HTML and CSS missing for a candidate whose
    # profile lists both.
    if evaluation.seniority.is_known:
        reachable = candidate.desired_seniority.rank + policy.seniority_tolerance_bands
        if evaluation.seniority.rank > reachable:
            downgrade(
                Decision.SKIP,
                f"the entry bar is {evaluation.seniority.value.replace('_', '-')}, "
                f"beyond {candidate.desired_seniority.value.replace('_', '/')} by more than "
                f"{policy.seniority_tolerance_bands} band(s)",
            )

    # Location the candidate cannot actually take.
    if evaluation.location_fit is LocationFit.OTHER_CITY:
        downgrade(
            Decision.SKIP if not policy.allow_other_city_review else Decision.REVIEW,
            "the role is based in another city",
        )
    elif evaluation.location_fit is LocationFit.REMOTE and not policy.accept_remote:
        downgrade(Decision.SKIP, "remote roles are excluded by your settings")

    # Explicitly not a software role — but only as a demotion to REVIEW, not a
    # veto. Anything reaching the policy has already passed the Stage 1 gate,
    # which makes that same call from the deterministic classifier and measured
    # 79% accurate on it. The model's single boolean is worth less than that:
    # asked for it, qwen2.5:3b called "Junior C++ Developer" and "Junior
    # Software Engineer" non-software. Disagreement means uncertainty, so the
    # candidate gets to look rather than never seeing the job.
    if evaluation.is_it_role is False:
        downgrade(Decision.REVIEW, "the model does not think this is a software role")

    if decision is evaluation.decision:
        return PolicyOutcome(evaluation, [])

    return PolicyOutcome(evaluation.model_copy(update={"decision": decision}), adjustments)


def _rank(decision: Decision) -> int:
    """Higher means a stronger endorsement."""
    return {Decision.SKIP: 0, Decision.REVIEW: 1, Decision.APPLY: 2}[decision]


@dataclass(frozen=True)
class AutoApplyCheck:
    """Whether an APPLY may be submitted without a human looking at it."""

    allowed: bool
    blockers: list[str]


def may_auto_apply(
    evaluation: JobEvaluation,
    job: NormalizedJob,
    *,
    has_valid_cv: bool,
    application_route_known: bool,
    already_applied: bool,
    policy: DecisionPolicy | None = None,
) -> AutoApplyCheck:
    """Every condition that must hold before an application is sent unattended.

    Deliberately strict and deliberately separate from :func:`apply_policy`: a
    strong recommendation is a reason to show the job to the candidate, not a
    reason to act on their behalf.
    """
    policy = policy or DecisionPolicy()
    blockers: list[str] = []

    if evaluation.decision is not Decision.APPLY:
        blockers.append(f"recommendation is {evaluation.decision.value}, not apply")
    if evaluation.degraded:
        blockers.append("evaluation was degraded")
    if evaluation.confidence < policy.min_apply_confidence:
        blockers.append("confidence below the auto-apply threshold")
    if evaluation.blocking_gaps:
        blockers.append("a mandatory requirement is unmet")
    if not evaluation.location_fit.is_viable:
        blockers.append("location is not viable")
    if not has_usable_description(job, minimum_chars=policy.min_description_chars):
        blockers.append("posting text was never fetched")
    if not has_valid_cv:
        blockers.append("no valid CV is available")
    if not application_route_known:
        blockers.append("application route is unknown")
    if already_applied:
        blockers.append("an application already exists for this job")

    return AutoApplyCheck(allowed=not blockers, blockers=blockers)

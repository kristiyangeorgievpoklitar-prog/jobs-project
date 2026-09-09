"""Ordering the jobs the candidate is shown.

Ranking and deciding are kept apart on purpose. The decision (APPLY / REVIEW /
SKIP) answers "am I eligible and is this worth my time", and is the model's to
make from the posting. The rank answers "which of these do I look at first", and
is where the candidate's demonstrated taste belongs.

Letting a learned preference change a decision would mean a few skips could hide
a job the candidate is perfectly qualified for. Letting it change an order only
means they see things in a more useful sequence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from jobhunter.db.models import Job
from jobhunter.domain.evaluation import Decision, JobEvaluation
from jobhunter.personalization.learner import observations_for_job

# How far preferences may move a job's position, as a share of the base score.
# Small on purpose: taste breaks ties, it does not outrank eligibility.
MAX_PREFERENCE_SHIFT = 0.15

# Base ordering by decision. The gaps are wide enough that no amount of learned
# preference can lift a REVIEW above an APPLY.
DECISION_BASE: dict[Decision, float] = {
    Decision.APPLY: 1.0,
    Decision.REVIEW: 0.5,
    Decision.SKIP: 0.0,
}


@dataclass
class RankedJob:
    job: Job
    evaluation: JobEvaluation
    score: float
    preference_shift: float
    matched_preferences: list[tuple[str, str, float]]
    # Filled in by the presentation layer, which knows how to phrase it.
    preference_explanation: str = ""
    # Technologies the evaluation credits the candidate with that their profile
    # does not list. Shown, never silently removed.
    unverified_claims: list[str] = field(default_factory=list)

    @property
    def personalised(self) -> bool:
        return abs(self.preference_shift) > 0.001


def preference_shift(
    job: Job, preferences: dict[tuple[str, str], float]
) -> tuple[float, list[tuple[str, str, float]]]:
    """How far the candidate's history nudges this job, and on what grounds."""
    if not preferences:
        return 0.0, []

    matched: list[tuple[str, str, float]] = []
    for observation in observations_for_job(job):
        weight = preferences.get((observation.dimension, observation.value))
        if weight:
            matched.append((observation.dimension, observation.value, weight))

    if not matched:
        return 0.0, []

    # The mean, not the sum: a job tagged with ten technologies must not
    # outweigh one tagged with two simply by having more tags.
    average = sum(weight for _, _, weight in matched) / len(matched)
    return average * MAX_PREFERENCE_SHIFT, matched


def rank_jobs(
    pairs: list[tuple[Job, JobEvaluation]],
    preferences: dict[tuple[str, str], float] | None = None,
) -> list[RankedJob]:
    """Order jobs by decision first, then confidence, then learned preference."""
    preferences = preferences or {}
    ranked: list[RankedJob] = []

    for job, evaluation in pairs:
        base = DECISION_BASE.get(evaluation.decision, 0.0)
        # Confidence separates jobs within a decision band without crossing bands.
        confidence_term = evaluation.confidence * 0.25
        shift, matched = preference_shift(job, preferences)

        ranked.append(
            RankedJob(
                job=job,
                evaluation=evaluation,
                score=base + confidence_term + shift,
                preference_shift=shift,
                matched_preferences=sorted(matched, key=lambda m: -abs(m[2]))[:4],
            )
        )

    ranked.sort(key=lambda r: r.score, reverse=True)
    return ranked


def explain_preferences(matched: list[tuple[str, str, float]]) -> str:
    """A short phrase saying why a job moved up or down the list."""
    if not matched:
        return ""
    parts = []
    for dimension, value, weight in matched[:2]:
        direction = "you tend to like" if weight > 0 else "you tend to skip"
        parts.append(f"{direction} {dimension} {value}")
    return "; ".join(parts)

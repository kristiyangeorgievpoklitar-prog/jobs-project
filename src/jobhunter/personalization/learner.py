"""Learning what the candidate actually wants from what they actually do.

Deliberately simple. A job hunt produces a few dozen decisions over months, not
a training set, so anything with real capacity would memorise noise — three
skips of Sofia roles is not evidence that the candidate hates Sofia, it is three
data points. What is used instead is a smoothed preference per observed value,
which cannot move far on thin evidence and can be read and corrected by the
person it is about.

The learned signal only ever *reorders* what is shown. It never overrides the
model's decision, because a preference is about taste and the decision is about
whether the candidate is eligible.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select

from jobhunter.db.models import CandidatePreference, Job, UserFeedback
from jobhunter.logging_setup import get_logger
from jobhunter.normalize.normalizer import normalize_company

log = get_logger(__name__)

# How many observations a value needs before it moves the ranking at all.
MIN_EVIDENCE = 3

# Pulls the smoothed weight towards zero, so a single decision cannot swing it.
SMOOTHING = 2.0

# Dimensions read off a job when feedback lands on it.
DIMENSION_TECHNOLOGY = "technology"
DIMENSION_COMPANY = "company"
DIMENSION_SENIORITY = "seniority"
DIMENSION_WORK_MODE = "work_mode"
DIMENSION_CITY = "city"

# Explicit reasons the candidate can give, mapped to the dimension they are
# really about. An explicit reason is worth more than an implicit skip, because
# it says *why*.
REASON_DIMENSIONS: dict[str, str] = {
    "too_senior": DIMENSION_SENIORITY,
    "wrong_location": DIMENSION_CITY,
    "technology": DIMENSION_TECHNOLOGY,
    "company": DIMENSION_COMPANY,
}

EXPLICIT_REASON_MULTIPLIER = 2.0

VALID_ACTIONS = ("apply", "skip", "not_sure")
VALID_REASONS = (
    "too_senior",
    "wrong_location",
    "salary",
    "technology",
    "company",
    "not_interested",
    "other",
)


@dataclass(frozen=True)
class Observation:
    """One (dimension, value) pair seen in a job the candidate judged."""

    dimension: str
    value: str
    weight: float


def observations_for_job(job: Job, *, reason: str | None = None) -> list[Observation]:
    """The features of a job that a preference could attach to."""
    out: list[Observation] = []

    def emphasis(dimension: str) -> float:
        """Weight an explicit reason more than an incidental co-occurrence."""
        if reason and REASON_DIMENSIONS.get(reason) == dimension:
            return EXPLICIT_REASON_MULTIPLIER
        return 1.0

    for tech in (job.tech_keywords or [])[:12]:
        value = str(tech).strip().lower()
        if value:
            out.append(Observation(DIMENSION_TECHNOLOGY, value, emphasis(DIMENSION_TECHNOLOGY)))

    company = job.company_name_raw or (job.company.name if job.company else None)
    if company:
        out.append(
            Observation(DIMENSION_COMPANY, normalize_company(company), emphasis(DIMENSION_COMPANY))
        )

    if job.seniority is not None and job.seniority.is_known:
        out.append(
            Observation(DIMENSION_SENIORITY, job.seniority.value, emphasis(DIMENSION_SENIORITY))
        )

    if job.work_mode is not None and job.work_mode.value != "unknown":
        out.append(
            Observation(DIMENSION_WORK_MODE, job.work_mode.value, emphasis(DIMENSION_WORK_MODE))
        )

    if job.city:
        out.append(Observation(DIMENSION_CITY, job.city.strip().lower(), emphasis(DIMENSION_CITY)))

    return out


def smoothed_weight(applies: float, skips: float) -> float:
    """A preference in [-1, 1], pulled towards 0 when the evidence is thin.

    With one apply and no skips this returns +0.33, not +1.0 — which is the
    point: the system should lean, not conclude.
    """
    total = applies + skips
    if total <= 0:
        return 0.0
    return (applies - skips) / (total + SMOOTHING)


def rebuild_preferences(session) -> dict[str, int]:
    """Recompute every preference from the full feedback history.

    A full rebuild rather than an incremental update: the history is small, and
    recomputing means a corrected or deleted piece of feedback actually takes
    effect instead of leaving a residue behind.
    """
    tallies: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])

    feedback_rows = session.scalars(select(UserFeedback).order_by(UserFeedback.id)).all()

    for feedback in feedback_rows:
        if feedback.action not in ("apply", "skip"):
            continue  # "not_sure" carries no directional signal
        job = session.get(Job, feedback.job_id)
        if job is None:
            continue

        for observation in observations_for_job(job, reason=feedback.reason):
            key = (observation.dimension, observation.value)
            entry = tallies[key]
            if feedback.action == "apply":
                entry[0] += observation.weight
            else:
                entry[1] += observation.weight
            entry[2] += 1

    session.query(CandidatePreference).delete()

    written = 0
    for (dimension, value), (applies, skips, count) in tallies.items():
        session.add(
            CandidatePreference(
                dimension=dimension,
                value=value,
                weight=smoothed_weight(applies, skips),
                applies=int(applies),
                skips=int(skips),
                evidence_count=int(count),
            )
        )
        written += 1

    session.flush()
    log.info("preferences_rebuilt", preferences=written, feedback=len(feedback_rows))
    return {"preferences": written, "feedback": len(feedback_rows)}


def load_preferences(session, *, min_evidence: int = MIN_EVIDENCE) -> dict[tuple[str, str], float]:
    """Preferences strong enough in evidence to be worth applying."""
    rows = session.scalars(
        select(CandidatePreference).where(CandidatePreference.evidence_count >= min_evidence)
    ).all()
    return {(row.dimension, row.value): row.weight for row in rows}

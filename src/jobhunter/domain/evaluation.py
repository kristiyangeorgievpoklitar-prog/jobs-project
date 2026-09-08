"""The structured evaluation of one job against the candidate.

This replaces the 0-100 match score as the thing the system actually decides on.
A score is a single number with no failure mode: it cannot say *why*, it cannot
distinguish a missing mandatory requirement from a missing bonus, and it hides
the difference between "confidently a poor fit" and "we could not tell".

An evaluation is instead a set of claims that can each be right or wrong, and so
can be checked against a human label — which is what makes the benchmark
possible at all.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jobhunter.domain.enums import Seniority


class Decision(StrEnum):
    """What the candidate should do about this listing."""

    APPLY = "apply"
    REVIEW = "review"
    SKIP = "skip"


class Fit(StrEnum):
    """How well the candidate covers one requirement.

    ``UNKNOWN`` is deliberately distinct from ``MISSING``: absence of evidence is
    not evidence of absence, and conflating the two is how a matcher invents
    gaps the candidate does not have.
    """

    STRONG = "strong"
    ACCEPTABLE = "acceptable"
    WEAK = "weak"
    MISSING = "missing"
    UNKNOWN = "unknown"

    @property
    def is_covered(self) -> bool:
        return self in (Fit.STRONG, Fit.ACCEPTABLE)


class LocationFit(StrEnum):
    """Where the job actually is, independent of the search filter used.

    Jobs.bg returns listings for a city that are really elsewhere, so this is
    re-derived from the posting text rather than trusted from the query.
    """

    EXACT_CITY = "exact_city"
    HYBRID_CITY = "hybrid_city"
    REMOTE = "remote"
    OTHER_CITY = "other_city"
    UNCLEAR = "unclear"

    @property
    def is_viable(self) -> bool:
        return self in (LocationFit.EXACT_CITY, LocationFit.HYBRID_CITY, LocationFit.REMOTE)


class ExperienceFit(StrEnum):
    GOOD = "good"
    ACCEPTABLE = "acceptable"
    STRETCH = "stretch"
    INSUFFICIENT = "insufficient"
    UNKNOWN = "unknown"


class RequirementAssessment(BaseModel):
    """One requirement lifted from the posting, judged against the candidate."""

    model_config = ConfigDict(extra="ignore")

    requirement: str = Field(max_length=300)
    candidate_fit: Fit = Fit.UNKNOWN
    evidence: str | None = Field(default=None, max_length=400)

    @field_validator("requirement", mode="before")
    @classmethod
    def _clean(cls, value: object) -> str:
        return str(value or "").strip()[:300]

    @field_validator("evidence", mode="before")
    @classmethod
    def _clean_evidence(cls, value: object) -> str | None:
        if value is None:
            return None
        return str(value).strip()[:400] or None


class JobEvaluation(BaseModel):
    """A complete, explainable assessment of one job for one candidate.

    Every field here is either something a human could disagree with (and so can
    be scored in the benchmark), or provenance needed to know whether the
    evaluation is still valid.
    """

    model_config = ConfigDict(extra="ignore")

    # --- the decision
    decision: Decision = Decision.REVIEW
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    recommendation: str = Field(default="", max_length=300)
    reasoning: str = Field(default="", max_length=2000)

    # --- the claims the decision rests on
    is_it_role: bool | None = None
    seniority: Seniority = Seniority.UNKNOWN
    seniority_reasoning: str = Field(default="", max_length=600)
    location_fit: LocationFit = LocationFit.UNCLEAR
    location_reasoning: str = Field(default="", max_length=400)
    employment_fit: bool | None = None
    experience_fit: ExperienceFit = ExperienceFit.UNKNOWN

    mandatory_requirements: list[RequirementAssessment] = Field(default_factory=list)
    nice_to_have_requirements: list[RequirementAssessment] = Field(default_factory=list)

    major_strengths: list[str] = Field(default_factory=list)
    major_risks: list[str] = Field(default_factory=list)

    # --- provenance: what produced this, and over what inputs
    source: str = "unknown"
    model: str | None = None
    prompt_version: str | None = None
    schema_version: int = 1
    job_content_hash: str | None = None
    profile_version: int = 1
    latency_ms: int | None = None
    degraded: bool = False
    degraded_reason: str | None = None
    created_at: datetime | None = None

    # --- optional internal ordering signal. Never the headline.
    rank_score: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator(
        "recommendation",
        "reasoning",
        "seniority_reasoning",
        "location_reasoning",
        mode="before",
    )
    @classmethod
    def _truncate(cls, value: object, info) -> str:
        """Trim an over-long string instead of failing the whole evaluation.

        Every schema rejection observed in the benchmark was ``string_too_long``
        — a model writing three sentences where the cap allowed two. Discarding
        a sound assessment over that, and reporting the job as "could not be
        evaluated", was the single largest source of degraded results:
        llama3.2:3b lost 29% of its answers this way.
        """
        limit = 2000
        for meta in cls.model_fields[info.field_name].metadata:
            limit = getattr(meta, "max_length", None) or limit
        return str(value or "").strip()[:limit]

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, value: object) -> float:
        """Rescue an out-of-range confidence instead of failing the evaluation.

        Small models routinely answer this on the wrong scale — a measured case
        returned ``3``. Rejecting the whole object over one bad number throws away
        a sound assessment, but clamping upward would silently assert certainty
        the model never expressed. So a recognisable percentage is converted, and
        anything else falls back to "no view".
        """
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.5
        if 0.0 <= number <= 1.0:
            return number
        if 1.0 < number <= 100.0 and number >= 10.0:
            return round(number / 100.0, 3)
        return 0.5

    @field_validator("major_strengths", "major_risks", mode="before")
    @classmethod
    def _clean_list(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(v).strip()[:200] for v in value if str(v).strip()][:8]

    @property
    def blocking_gaps(self) -> list[RequirementAssessment]:
        """Mandatory requirements the candidate demonstrably does not meet."""
        return [r for r in self.mandatory_requirements if r.candidate_fit is Fit.MISSING]

    @property
    def mandatory_coverage(self) -> float | None:
        """Share of mandatory requirements the candidate covers, if any were found."""
        if not self.mandatory_requirements:
            return None
        covered = sum(1 for r in self.mandatory_requirements if r.candidate_fit.is_covered)
        return covered / len(self.mandatory_requirements)

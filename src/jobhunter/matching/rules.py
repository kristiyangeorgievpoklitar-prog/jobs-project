"""Deterministic, explainable job-to-candidate scoring.

This is the reference scorer: it needs no API key, always produces the same
result for the same input, and is what the AI providers fall back to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jobhunter.classify import keywords as K
from jobhunter.domain.enums import EmploymentType, Recommendation, Seniority, WorkMode
from jobhunter.domain.schemas import (
    CandidateSnapshot,
    ClassificationResult,
    MatchResult,
    NormalizedJob,
)
from jobhunter.normalize.normalizer import normalize_text


@dataclass(frozen=True)
class ScoringConfig:
    """Component weights (summing to 100) and decision thresholds."""

    weight_seniority: float = 30.0
    weight_location: float = 20.0
    weight_tech: float = 30.0
    weight_experience: float = 10.0
    weight_language: float = 5.0
    weight_employment: float = 5.0

    auto_apply_threshold: int = 90
    review_threshold: int = 75
    max_seniority: Seniority = Seniority.MID

    # How far above the candidate's experience a listing may sit before it is
    # treated as out of reach.
    experience_tolerance_years: float = 1.5

    # A listing whose description we never fetched is scored on the card alone.
    # Below this many characters the evidence is treated as thin.
    min_description_chars: int = 200

    aliases: dict[str, set[str]] = field(default_factory=dict)

    @property
    def total_weight(self) -> float:
        return (
            self.weight_seniority
            + self.weight_location
            + self.weight_tech
            + self.weight_experience
            + self.weight_language
            + self.weight_employment
        )


# Terms that should count as a match for one another.
TECH_ALIASES: dict[str, set[str]] = {
    "javascript": {"js", "ecmascript", "vanilla js"},
    "typescript": {"ts"},
    "postgresql": {"postgres"},
    "node.js": {"nodejs", "node"},
    "react": {"reactjs", "react.js"},
    "vue": {"vuejs", "vue.js"},
    "tailwind": {"tailwindcss", "tailwind css"},
    "c#": {"csharp", "c sharp"},
    "golang": {"go"},
    "kubernetes": {"k8s"},
    ".net": {"dotnet", "asp.net", ".net core"},
    "sql": {"mysql", "postgresql", "mssql", "sql server", "plsql", "t-sql", "mariadb"},
}


def expand_tech(terms: set[str]) -> set[str]:
    """Expand a set of technologies with their aliases, both directions."""
    expanded = set(terms)
    for canonical, aliases in TECH_ALIASES.items():
        if canonical in terms:
            expanded |= aliases
        if terms & aliases:
            expanded.add(canonical)
    return expanded


def _tokens_from_text(text: str) -> set[str]:
    lowered = normalize_text(text)
    found = set()
    for term in K.ALL_TECH_KEYWORDS:
        pattern = re.escape(term)
        regex = rf"(?<![\w]){pattern}(?![\w])" if term.isalnum() else pattern
        if re.search(regex, lowered):
            found.add(term)
    return found


def job_tech_requirements(
    job: NormalizedJob, classification: ClassificationResult
) -> tuple[set[str], set[str]]:
    """Split the job's technologies into required and preferred sets."""
    tagged = {t.lower() for t in job.tech_keywords}
    required = {t for t in tagged if t in K.ALL_TECH_KEYWORDS}
    required |= _tokens_from_text(" ".join(classification.requirements_required))
    required |= _tokens_from_text(job.title)

    preferred = _tokens_from_text(" ".join(classification.requirements_preferred))
    preferred -= required

    if not required and job.description:
        required = _tokens_from_text(job.description[:3000])

    return required, preferred


def score_seniority(
    job_seniority: Seniority, candidate: CandidateSnapshot, config: ScoringConfig
) -> tuple[float, str]:
    """1.0 when the entry bar sits at or below the candidate's target."""
    if job_seniority is Seniority.UNKNOWN:
        return 0.6, "seniority unknown"

    target = candidate.desired_seniority
    gap = job_seniority.rank - target.rank

    if gap <= 0:
        return 1.0, f"{job_seniority.value} is at or below target {target.value}"
    if gap == 1:
        return 0.65, f"{job_seniority.value} is one level above target"
    if gap == 2:
        return 0.25, f"{job_seniority.value} is two levels above target"
    return 0.0, f"{job_seniority.value} is far above target {target.value}"


def score_location(
    job: NormalizedJob, classification: ClassificationResult, candidate: CandidateSnapshot
) -> tuple[float, str]:
    if classification.location_relevant is True:
        if job.work_mode is WorkMode.REMOTE:
            return 1.0, "remote"
        return 1.0, f"location matches ({job.city or 'unknown'})"
    if classification.location_relevant is None:
        return 0.5, "location undetermined"
    if job.work_mode is WorkMode.REMOTE and candidate.remote_ok:
        return 0.9, "remote despite city mismatch"
    return 0.0, f"location mismatch ({job.city or 'unknown'})"


def score_tech(
    required: set[str], preferred: set[str], candidate: CandidateSnapshot
) -> tuple[float, list[str], list[str], str]:
    """Overlap between the job's technologies and the candidate's."""
    have = expand_tech(candidate.all_tech)
    req = expand_tech(required)
    pref = expand_tech(preferred)

    matched_req = sorted({t for t in required if t in have or expand_tech({t}) & have})
    missing_req = sorted({t for t in required if t not in have and not (expand_tech({t}) & have)})
    matched_pref = sorted({t for t in preferred if t in have or expand_tech({t}) & have})

    if not req and not pref:
        return 0.5, [], [], "no technologies detected"

    ratio = len(matched_req) / len(req) if req else 0.5

    # Preferred skills can lift the score but never carry it alone.
    if pref:
        ratio = min(1.0, ratio + 0.15 * (len(matched_pref) / len(pref)))

    detail = f"{len(matched_req)}/{len(req)} required technologies matched"
    return ratio, matched_req + matched_pref, missing_req, detail


def score_experience(
    job: NormalizedJob, candidate: CandidateSnapshot, config: ScoringConfig
) -> tuple[float, str]:
    required = job.years_experience_required
    if required is None:
        return 0.7, "experience requirement not stated"
    have = candidate.years_experience
    if have >= required:
        return 1.0, f"{have:g}y experience meets {required:g}y"
    shortfall = required - have
    if shortfall <= config.experience_tolerance_years:
        return 0.6, f"{shortfall:g}y short of {required:g}y (within tolerance)"
    if shortfall <= config.experience_tolerance_years * 2:
        return 0.25, f"{shortfall:g}y short of {required:g}y"
    return 0.0, f"{shortfall:g}y short of {required:g}y"


def score_language(
    job: NormalizedJob, classification: ClassificationResult, candidate: CandidateSnapshot
) -> tuple[float, str]:
    spoken = candidate.spoken_languages
    text = " ".join(job.languages).lower() if job.languages else ""
    if not text and job.description:
        text = normalize_text(job.description[:2500])

    required_languages = {
        name
        for name, markers in K.LANGUAGE_REQUIREMENT_MARKERS.items()
        if any(m in text for m in markers)
    }
    required_languages.discard("bulgarian")

    if not required_languages:
        return 1.0, "no specific language requirement"

    missing = {lang for lang in required_languages if lang not in spoken}
    if not missing:
        return 1.0, f"speaks required: {', '.join(sorted(required_languages))}"
    ratio = 1 - (len(missing) / len(required_languages))
    return max(ratio, 0.0), f"missing language(s): {', '.join(sorted(missing))}"


def score_employment(job: NormalizedJob, candidate: CandidateSnapshot) -> tuple[float, str]:
    wanted = {e.lower() for e in candidate.employment_types}
    if not wanted or job.employment_type is EmploymentType.UNKNOWN:
        return 1.0, "no employment-type constraint"
    if job.employment_type.value in wanted:
        return 1.0, f"employment type {job.employment_type.value} accepted"
    return 0.3, f"employment type {job.employment_type.value} not preferred"


def find_disqualifiers(
    job: NormalizedJob,
    classification: ClassificationResult,
    candidate: CandidateSnapshot,
    config: ScoringConfig,
    missing_required: list[str],
    required: set[str],
) -> list[str]:
    """Hard blockers that force a SKIP regardless of the numeric score."""
    out: list[str] = []

    if classification.is_it is False:
        out.append("Not an IT/software position")

    if classification.location_relevant is False and not (
        job.work_mode is WorkMode.REMOTE and candidate.remote_ok
    ):
        out.append(f"Location not viable ({job.city or job.location_raw or 'unknown'})")

    if classification.seniority.rank > config.max_seniority.rank + 1:
        out.append(f"Seniority too high ({classification.seniority.value})")

    required_years = job.years_experience_required
    if required_years is not None and required_years - candidate.years_experience > (
        config.experience_tolerance_years * 2
    ):
        out.append(f"Requires {required_years:g}y experience vs {candidate.years_experience:g}y")

    if (
        required
        and missing_required
        and len(missing_required) == len(required)
        and len(required) >= 3
    ):
        out.append("No overlap with the required technology stack")

    return out


def decide(score: int, disqualifiers: list[str], config: ScoringConfig) -> Recommendation:
    if disqualifiers:
        return Recommendation.SKIP
    if score >= config.auto_apply_threshold:
        return Recommendation.APPLY
    if score >= config.review_threshold:
        return Recommendation.REVIEW
    return Recommendation.SKIP


def score_job(
    job: NormalizedJob,
    classification: ClassificationResult,
    candidate: CandidateSnapshot,
    config: ScoringConfig | None = None,
) -> MatchResult:
    """Score one job against the candidate profile."""
    config = config or ScoringConfig()

    required, preferred = job_tech_requirements(job, classification)

    seniority_score, seniority_note = score_seniority(classification.seniority, candidate, config)
    location_score, location_note = score_location(job, classification, candidate)
    tech_score, matched_tech, missing_tech, tech_note = score_tech(required, preferred, candidate)
    experience_score, experience_note = score_experience(job, candidate, config)
    language_score, language_note = score_language(job, classification, candidate)
    employment_score, employment_note = score_employment(job, candidate)

    weighted = (
        seniority_score * config.weight_seniority
        + location_score * config.weight_location
        + tech_score * config.weight_tech
        + experience_score * config.weight_experience
        + language_score * config.weight_language
        + employment_score * config.weight_employment
    )
    score = round(weighted / config.total_weight * 100)
    score = max(0, min(100, score))

    disqualifiers = find_disqualifiers(
        job, classification, candidate, config, missing_tech, required
    )
    if disqualifiers:
        score = min(score, config.review_threshold - 1)

    # Evidence gate: a listing whose description was never fetched has been
    # judged on its title and card metadata alone. That is enough to shortlist
    # it, but never enough to recommend applying unseen, so the score is capped
    # below the auto-apply threshold and confidence is reduced.
    thin_evidence = len((job.description or "").strip()) < config.min_description_chars
    if thin_evidence:
        score = min(score, config.auto_apply_threshold - 1)

    strengths: list[str] = []
    if matched_tech:
        strengths.extend(
            sorted({t.title() if t.islower() and t.isalpha() else t for t in matched_tech})[:10]
        )
    if seniority_score >= 1.0 and classification.seniority.is_known:
        strengths.append(f"Seniority fit ({classification.seniority.value})")
    if location_score >= 0.9:
        strengths.append(location_note.capitalize())
    if experience_score >= 1.0:
        strengths.append("Experience requirement met")

    # Confidence reflects how much evidence the classifiers actually had.
    confidence = min(
        1.0,
        0.35
        + 0.25 * classification.it_confidence
        + 0.25 * classification.seniority_confidence
        + (0.15 if required else 0.0),
    )
    if thin_evidence:
        confidence = min(confidence, 0.5)

    notes = [
        seniority_note,
        location_note,
        tech_note,
        experience_note,
        language_note,
        employment_note,
    ]
    if thin_evidence:
        notes.append("description not fetched - capped below auto-apply on thin evidence")
    reasoning = "; ".join(notes)

    return MatchResult(
        score=score,
        confidence=round(confidence, 2),
        recommendation=decide(score, disqualifiers, config),
        strengths=strengths[:12],
        missing_skills=missing_tech[:12],
        disqualifiers=disqualifiers,
        component_scores={
            "seniority": round(seniority_score, 3),
            "location": round(location_score, 3),
            "tech": round(tech_score, 3),
            "experience": round(experience_score, 3),
            "language": round(language_score, 3),
            "employment": round(employment_score, 3),
        },
        reasoning=reasoning,
        provider="rule_based",
    )

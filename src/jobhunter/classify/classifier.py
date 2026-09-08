"""Rule-based classification of a normalized job.

Answers three questions the pipeline depends on:

* is this really an IT role?
* what is the *minimum* seniority the employer will accept?
* is it relevant to the candidate's target location?

``seniority`` is deliberately the entry bar, not the ceiling: a listing tagged
"Mid-level, Senior-level" is classified MID, because that is the level a
candidate must reach to be eligible.
"""

from __future__ import annotations

import re

from jobhunter.classify import keywords as K
from jobhunter.domain.enums import Language, Seniority, WorkMode
from jobhunter.domain.schemas import ClassificationResult, NormalizedJob
from jobhunter.normalize.normalizer import detect_language, normalize_text

_BULLET = re.compile(r"^\s*[-•*·▪◦o]\s*|^\s*\d+[.)]\s*")


def _contains(haystack: str, needle: str) -> bool:
    """Word-ish containment that tolerates the punctuation in tech names."""
    if not needle:
        return False
    if not needle.isalnum():
        return needle in haystack
    return re.search(rf"(?<![\w]){re.escape(needle)}(?![\w])", haystack) is not None


def _contains_role(haystack: str, needle: str) -> bool:
    """Role match that also accepts the plural form ("engineer" ~ "engineers")."""
    if _contains(haystack, needle):
        return True
    if needle.isalnum() and len(needle) > 3 and not needle.endswith("s"):
        return re.search(rf"(?<![\w]){re.escape(needle)}s(?![\w])", haystack) is not None
    return False


def years_to_seniority(years: float | None) -> Seniority | None:
    """Map a required-experience lower bound onto a seniority band."""
    if years is None:
        return None
    if years < 0.5:
        return Seniority.ENTRY
    if years < 2:
        return Seniority.JUNIOR
    if years < 3:
        return Seniority.JUNIOR_MID
    if years < 4:
        return Seniority.MID
    if years < 5:
        return Seniority.MID_SENIOR
    return Seniority.SENIOR


def classify_seniority(job: NormalizedJob) -> tuple[Seniority, float, list[str]]:
    """Determine the minimum seniority the employer accepts."""
    signals: list[str] = []
    title = normalize_text(job.title)

    # 1. The site's own level taxonomy is the strongest signal available.
    site_levels: list[Seniority] = []
    if job.level_raw:
        level_text = job.level_raw.lower()
        for token, seniority in K.SITE_LEVEL_MAP.items():
            if token in level_text:
                site_levels.append(seniority)
                signals.append(f"site-level:{token}")

    # 2. Title wording.
    title_hits: list[Seniority] = []
    for seniority, patterns in K.SENIORITY_PATTERNS:
        for pattern in patterns:
            if _contains(title, pattern):
                title_hits.append(seniority)
                signals.append(f"title:{pattern}")
                break

    # 3. Declared years of experience.
    years_level = years_to_seniority(job.years_experience_required)
    if years_level is not None:
        signals.append(f"years:{job.years_experience_required}")

    # 4. Description wording, as a weak fallback.
    desc_hits: list[Seniority] = []
    if job.description:
        body = normalize_text(job.description[:4000])
        for seniority, patterns in K.SENIORITY_PATTERNS:
            for pattern in patterns:
                if _contains(body, pattern):
                    desc_hits.append(seniority)
                    signals.append(f"desc:{pattern}")
                    break

    # Resolve. A listed range means the *lowest* listed level is the entry bar.
    if site_levels:
        result = min(site_levels, key=lambda s: s.rank)
        confidence = 0.9
    elif title_hits:
        result = min(title_hits, key=lambda s: s.rank)
        confidence = 0.75
    elif years_level is not None:
        result = years_level
        confidence = 0.7
    elif desc_hits:
        result = min(desc_hits, key=lambda s: s.rank)
        confidence = 0.45
    else:
        return Seniority.UNKNOWN, 0.0, signals

    # Be conservative: an explicit senior/lead word in the title outranks a
    # softer signal elsewhere, so we never under-call a senior posting.
    senior_titles = [s for s in title_hits if s.rank >= Seniority.SENIOR.rank]
    if senior_titles and result.rank < Seniority.SENIOR.rank:
        result = min(senior_titles, key=lambda s: s.rank)
        signals.append("conservative:title-senior-override")
        confidence = max(confidence, 0.8)

    # Cross-check against declared years; take the stricter of the two.
    if years_level is not None and years_level.rank > result.rank + 1:
        result = years_level
        signals.append("conservative:years-override")

    return result, confidence, signals


def classify_it(job: NormalizedJob) -> tuple[bool | None, float, list[str]]:
    """Decide whether the listing is genuinely an IT/software role."""
    signals: list[str] = []
    title = normalize_text(job.title)
    haystack = normalize_text(f"{job.title} {' '.join(job.tech_keywords)}")
    body = normalize_text(job.description[:5000]) if job.description else ""

    role_hits = [kw for kw in K.IT_ROLE_KEYWORDS if _contains_role(title, kw)]
    signals += [f"role:{k}" for k in role_hits[:6]]

    tech_hits = {kw for kw in K.ALL_TECH_KEYWORDS if _contains(haystack, kw)}
    tag_hits = {t.lower() for t in job.tech_keywords if t.lower() in K.ALL_TECH_KEYWORDS}
    tech_hits |= tag_hits
    signals += [f"tech:{k}" for k in sorted(tech_hits)[:8]]

    body_tech = {kw for kw in K.ALL_TECH_KEYWORDS if _contains(body, kw)} if body else set()

    non_it = [kw for kw in K.NON_IT_MARKERS if _contains_role(title, kw)]
    if non_it:
        signals += [f"non-it:{k}" for k in non_it[:3]]

    # A non-IT job title with no compensating technical role wording is a reject.
    if non_it and not role_hits:
        return False, 0.8, signals

    score = 0.0
    score += 0.55 if role_hits else 0.0
    score += min(len(tech_hits), 4) * 0.12
    score += min(len(body_tech), 6) * 0.04
    if job.source_job_id and job.tech_keywords:
        score += 0.05
    if non_it:
        score -= 0.25

    if score >= 0.55:
        return True, min(score, 0.99), signals
    if score <= 0.15:
        return False, min(0.6 + (0.15 - score), 0.9), signals
    return None, max(score, 0.2), signals


def classify_location(
    job: NormalizedJob,
    *,
    target_locations: list[str],
    remote_ok: bool = True,
) -> tuple[bool | None, list[str]]:
    """Decide whether the job is reachable for the candidate."""
    signals: list[str] = []
    targets = [normalize_text(t) for t in target_locations if t.strip()]
    city = normalize_text(job.city or "")
    location_blob = normalize_text(f"{job.city or ''} {job.location_raw or ''}")

    if targets and city:
        for target in targets:
            if target and (target == city or target in location_blob):
                signals.append(f"city:{target}")
                return True, signals

    if job.work_mode is WorkMode.REMOTE:
        signals.append("mode:remote")
        return (True, signals) if remote_ok else (False, signals)

    if job.work_mode is WorkMode.HYBRID:
        # Hybrid still requires being near the office, so the city must match.
        signals.append("mode:hybrid")
        if targets and city and any(t in location_blob for t in targets):
            return True, signals
        return False, signals

    if not city:
        signals.append("city:unknown")
        return None, signals

    signals.append(f"city:mismatch:{city}")
    return False, signals


def _split_lines(description: str) -> list[str]:
    return [ln.strip() for ln in description.splitlines() if ln.strip()]


def extract_requirements(description: str | None) -> tuple[list[str], list[str]]:
    """Split a description into required vs preferred bullet points.

    Walks the text as a simple state machine: a heading switches the active
    bucket and subsequent bullet lines are collected into it.
    """
    if not description:
        return [], []

    required: list[str] = []
    preferred: list[str] = []
    bucket: list[str] | None = None

    for line in _split_lines(description):
        lowered = line.lower()
        is_heading = len(line) < 120

        if is_heading and any(m in lowered for m in K.PREFERRED_SECTION_MARKERS):
            bucket = preferred
            # A one-line "X would be an advantage" is itself a requirement.
            stripped = _BULLET.sub("", line).strip(" :-–")
            if len(stripped) > 25:
                preferred.append(stripped)
            continue
        if is_heading and any(m in lowered for m in K.REQUIRED_SECTION_MARKERS):
            bucket = required
            continue
        if is_heading and any(m in lowered for m in K.RESPONSIBILITY_MARKERS):
            bucket = None
            continue

        if bucket is None:
            continue
        cleaned = _BULLET.sub("", line).strip(" :-–")
        if 8 <= len(cleaned) <= 300:
            bucket.append(cleaned)

    return required[:25], preferred[:25]


def classify_job(
    job: NormalizedJob,
    *,
    target_locations: list[str] | None = None,
    remote_ok: bool = True,
) -> ClassificationResult:
    """Run every classifier over one normalized job."""
    seniority, seniority_conf, seniority_signals = classify_seniority(job)
    is_it, it_conf, it_signals = classify_it(job)
    location_relevant, location_signals = classify_location(
        job, target_locations=target_locations or [], remote_ok=remote_ok
    )
    required, preferred = extract_requirements(job.description)

    language = job.language
    if language is Language.UNKNOWN:
        language = detect_language(job.title, job.description)

    return ClassificationResult(
        seniority=seniority,
        seniority_confidence=seniority_conf,
        seniority_signals=seniority_signals[:12],
        is_it=is_it,
        it_confidence=it_conf,
        it_signals=it_signals[:12],
        location_relevant=location_relevant,
        location_signals=location_signals,
        language=language,
        requirements_required=required,
        requirements_preferred=preferred,
        years_experience_required=job.years_experience_required,
    )

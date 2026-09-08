"""Candidate profile persistence and CV-driven bootstrapping.

The profile is never hard-coded: it is either extracted from the candidate's own
CV on first run or edited through the dashboard.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobhunter.classify import keywords as K
from jobhunter.db.models import CandidateProfile
from jobhunter.domain.enums import Seniority
from jobhunter.domain.schemas import CandidateSnapshot
from jobhunter.logging_setup import get_logger

log = get_logger(__name__)

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE = re.compile(r"(?:\+\d{1,3}[\s-]?)?(?:\(?\d{2,4}\)?[\s-]?){2,4}\d{2,4}")
_GITHUB = re.compile(r"https?://(?:www\.)?github\.com/[\w.-]+", re.I)
_LINKEDIN = re.compile(r"https?://(?:www\.)?linkedin\.com/[\w/%.-]+", re.I)

LANGUAGE_LEVELS = {
    "native": "native",
    "роден": "native",
    "майчин": "native",
    "fluent": "fluent",
    "professional working": "professional",
    "professional": "professional",
    "advanced": "advanced",
    "intermediate": "intermediate",
    "basic": "basic",
    "conversational": "basic",
    "начално": "basic",
}

SKILL_SECTIONS = {
    "frameworks": K.FRAMEWORKS,
    "databases": K.DATABASES,
    "tools": K.TOOLS_AND_PLATFORMS,
    "skills": K.PROGRAMMING_LANGUAGES,
}


def _find_tokens(text: str, vocabulary: frozenset[str]) -> list[str]:
    """Find vocabulary terms present in free text, preserving canonical casing."""
    lowered = text.lower()
    found: list[str] = []
    for term in vocabulary:
        pattern = re.escape(term)
        regex = rf"(?<![\w]){pattern}(?![\w])" if term.isalnum() else pattern
        if re.search(regex, lowered):
            found.append(term)
    return sorted(set(found))


def extract_profile_fields(cv_text: str) -> dict[str, Any]:
    """Best-effort structured extraction from CV text.

    Only fills fields it can actually find; anything uncertain stays empty for
    the user to complete in the dashboard.
    """
    data: dict[str, Any] = {}
    if not cv_text:
        return data

    # PDF extraction sometimes escapes "@" and other punctuation; undo that.
    cv_text = cv_text.replace("\\@", "@").replace("\\.", ".").replace("\\-", "-")

    lines = [ln.strip() for ln in cv_text.splitlines() if ln.strip()]

    # The name is conventionally the first non-empty line of a CV.
    if lines:
        first = lines[0]
        if 3 < len(first) < 60 and "@" not in first and not first.upper().startswith("CV"):
            data["full_name"] = first
    if "full_name" not in data:
        for line in lines[:4]:
            candidate = line.replace("CV -", "").strip()
            if 3 < len(candidate) < 60 and "@" not in candidate and "[" not in candidate:
                data["full_name"] = candidate
                break

    if m := _EMAIL.search(cv_text):
        data["email"] = m.group(0).replace("\\", "")
    if m := _GITHUB.search(cv_text):
        data["github_url"] = m.group(0)
    if m := _LINKEDIN.search(cv_text):
        data["linkedin_url"] = m.group(0)

    # Phone: only accept it near an explicit contact marker to avoid dates.
    header = "\n".join(lines[:6])
    for match in _PHONE.finditer(header):
        digits = re.sub(r"\D", "", match.group(0))
        if 8 <= len(digits) <= 15:
            data["phone"] = match.group(0).strip(" []")
            break

    for city in ("Varna", "Варна", "Sofia", "София", "Plovdiv", "Пловдив", "Burgas", "Бургас"):
        if re.search(rf"\b{city}\b", cv_text, re.I):
            data["location"] = city
            break

    for bucket, vocabulary in SKILL_SECTIONS.items():
        data[bucket] = _find_tokens(cv_text, vocabulary)

    languages: list[dict[str, str]] = []
    for name, markers in K.LANGUAGE_REQUIREMENT_MARKERS.items():
        for marker in markers:
            if language_match := re.search(
                rf"{re.escape(marker)}\s*[—\-–:]?\s*([A-Za-zА-Яа-я /]{{0,30}})", cv_text, re.I
            ):
                level_text = language_match.group(1).lower()
                level = next((v for k, v in LANGUAGE_LEVELS.items() if k in level_text), "unknown")
                languages.append({"name": name.capitalize(), "level": level})
                break
    if languages:
        data["languages"] = languages

    education: list[dict[str, str]] = []
    for line in lines:
        if 5 < len(line) < 140 and re.search(
            r"universit|университет|college|академия|school of", line, re.I
        ):
            education.append({"institution": line, "degree": "", "year": ""})
    if education:
        data["education"] = education[:4]

    if m := re.search(r"SUMMARY|PROFILE|ПРОФИЛ", cv_text, re.I):
        tail = cv_text[m.end() :].strip()
        summary = " ".join(tail.splitlines()[:4]).strip()
        if 40 < len(summary) < 900:
            data["summary"] = summary

    return data


def get_active_profile(session: Session) -> CandidateProfile | None:
    return session.scalar(
        select(CandidateProfile)
        .where(CandidateProfile.is_active.is_(True))
        .order_by(CandidateProfile.id)
    )


def get_or_create_profile(session: Session) -> CandidateProfile:
    profile = get_active_profile(session)
    if profile is None:
        profile = CandidateProfile(is_active=True, version=1)
        session.add(profile)
        session.flush()
    return profile


def update_profile(session: Session, values: dict[str, Any]) -> CandidateProfile:
    """Apply a partial update and bump the profile version.

    The version is stamped onto every match so scores can be traced back to the
    profile that produced them.
    """
    profile = get_or_create_profile(session)
    changed = False
    for key, value in values.items():
        if not hasattr(profile, key) or key in {"id", "version", "is_active"}:
            continue
        if getattr(profile, key) != value:
            setattr(profile, key, value)
            changed = True
    if changed:
        profile.version = (profile.version or 1) + 1
    session.flush()
    return profile


def bootstrap_profile_from_cv(session: Session, cv_text: str) -> CandidateProfile:
    """Populate an empty profile from CV text without overwriting user edits."""
    extracted = extract_profile_fields(cv_text)
    profile = get_or_create_profile(session)

    values: dict[str, Any] = {}
    for key, value in extracted.items():
        if not value:
            continue
        current = getattr(profile, key, None)
        if not current:  # only fill blanks
            values[key] = value

    if values:
        update_profile(session, values)
        log.info("profile_bootstrapped", fields=sorted(values.keys()))
    return profile


def to_snapshot(profile: CandidateProfile | None) -> CandidateSnapshot:
    """Convert the ORM profile into the immutable object matchers consume."""
    if profile is None:
        return CandidateSnapshot()
    desired = profile.desired_seniority
    if not isinstance(desired, Seniority):
        try:
            desired = Seniority(str(desired))
        except ValueError:
            desired = Seniority.JUNIOR_MID
    return CandidateSnapshot(
        full_name=profile.full_name or "",
        location=profile.location or "",
        years_experience=float(profile.years_experience or 0.0),
        desired_seniority=desired,
        preferred_locations=list(profile.preferred_locations or []),
        remote_ok=bool(profile.remote_ok),
        skills=list(profile.skills or []),
        frameworks=list(profile.frameworks or []),
        databases=list(profile.databases or []),
        tools=list(profile.tools or []),
        soft_skills=list(profile.soft_skills or []),
        languages=list(profile.languages or []),
        education=list(profile.education or []),
        employment_types=list(profile.employment_types or []),
        salary_expectation_min=profile.salary_expectation_min,
        salary_expectation_max=profile.salary_expectation_max,
        summary=profile.summary,
        version=profile.version or 1,
    )

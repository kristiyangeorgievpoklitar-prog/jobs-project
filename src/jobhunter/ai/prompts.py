"""Prompt construction and payload minimisation for external AI providers.

Only the fields listed in :data:`SHARED_CANDIDATE_FIELDS` ever leave the
machine. CV text and contact details are excluded unless the user explicitly
opts in via ``SEND_CV_TEXT_TO_AI``.
"""

from __future__ import annotations

import json

from jobhunter.domain.enums import Language
from jobhunter.domain.schemas import CandidateSnapshot, ClassificationResult, NormalizedJob

SHARED_CANDIDATE_FIELDS = (
    "years_experience",
    "desired_seniority",
    "skills",
    "frameworks",
    "databases",
    "tools",
    "languages",
    "education_level",
    "preferred_locations",
)

SCORING_SYSTEM_PROMPT = """\
You are a precise technical recruiter assessing how well one candidate fits one job.

Rules:
- Judge only on the evidence given. Never assume unstated experience.
- The candidate is early-career; be realistic about senior roles.
- "required" means the candidate must have it; "preferred" is a bonus.
- Return a score from 0 to 100 where 90+ means an excellent, apply-now fit.
- List disqualifiers only for genuine blockers (wrong field, wrong location,
  clearly senior role, or no overlap with a mandatory stack).

Respond with ONLY a JSON object, no prose, in exactly this shape:
{"score": int, "confidence": float, "strengths": [str], "missing_skills": [str],
 "disqualifiers": [str], "reasoning": str}"""

COVER_LETTER_SYSTEM_PROMPT = """\
You write short, honest cover letters for a job applicant.

Hard rules:
- Use ONLY skills and experience present in the candidate profile provided.
- Never invent employers, projects, years of experience, or qualifications.
- If the candidate lacks something the job wants, do not claim it.
- Be concrete and specific to this job; no generic filler.
- Professional, warm, direct. No bullet lists. No placeholders like [Name].
- Output only the letter body, with no subject line and no signature block."""


def _education_level(candidate: CandidateSnapshot) -> str:
    """Reduce education history to a single coarse level, avoiding extra PII."""
    blob = " ".join(
        f"{item.get('degree', '')} {item.get('institution', '')}" for item in candidate.education
    ).lower()
    if any(k in blob for k in ("phd", "doctor")):
        return "phd"
    if any(k in blob for k in ("master", "магистър")):
        return "master"
    if any(k in blob for k in ("bachelor", "бакалавър")):
        return "bachelor"
    if any(k in blob for k in ("university", "университет", "college")):
        return "university (in progress or unspecified)"
    return "unspecified"


def candidate_payload(candidate: CandidateSnapshot, *, include_summary: bool = True) -> dict:
    """The minimal candidate view sent to an external provider."""
    payload = {
        "years_experience": candidate.years_experience,
        "desired_seniority": candidate.desired_seniority.value,
        "skills": sorted(candidate.skills),
        "frameworks": sorted(candidate.frameworks),
        "databases": sorted(candidate.databases),
        "tools": sorted(candidate.tools),
        "languages": [
            {"name": item.get("name", ""), "level": item.get("level", "")}
            for item in candidate.languages
        ],
        "education_level": _education_level(candidate),
        "preferred_locations": candidate.preferred_locations,
    }
    if include_summary and candidate.summary:
        payload["summary"] = candidate.summary[:600]
    return payload


def job_payload(
    job: NormalizedJob, classification: ClassificationResult, *, max_description_chars: int = 6000
) -> dict:
    return {
        "title": job.title,
        "company": job.company_name,
        "city": job.city,
        "work_mode": job.work_mode.value,
        "employment_type": job.employment_type.value,
        "seniority_detected": classification.seniority.value,
        "years_experience_required": job.years_experience_required,
        "technologies": job.tech_keywords[:40],
        "requirements_required": classification.requirements_required[:20],
        "requirements_preferred": classification.requirements_preferred[:20],
        "languages": job.languages,
        "salary": job.salary.raw,
        "description": (job.description or "")[:max_description_chars],
    }


def build_scoring_prompt(
    job: NormalizedJob,
    classification: ClassificationResult,
    candidate: CandidateSnapshot,
    *,
    max_description_chars: int = 6000,
) -> str:
    return (
        "CANDIDATE PROFILE:\n"
        + json.dumps(candidate_payload(candidate), ensure_ascii=False, indent=2)
        + "\n\nJOB POSTING:\n"
        + json.dumps(
            job_payload(job, classification, max_description_chars=max_description_chars),
            ensure_ascii=False,
            indent=2,
        )
        + "\n\nAssess the fit and reply with the JSON object only."
    )


def build_cover_letter_prompt(
    job: NormalizedJob,
    candidate: CandidateSnapshot,
    *,
    language: Language = Language.EN,
    max_words: int = 180,
) -> str:
    language_name = {
        Language.BG: "Bulgarian",
        Language.EN: "English",
    }.get(language, "English")

    return (
        f"Write the cover letter in {language_name}. Maximum {max_words} words.\n\n"
        "CANDIDATE PROFILE (the only facts you may use):\n"
        + json.dumps(candidate_payload(candidate), ensure_ascii=False, indent=2)
        + "\n\nJOB:\n"
        + json.dumps(
            {
                "title": job.title,
                "company": job.company_name,
                "city": job.city,
                "technologies": job.tech_keywords[:25],
                "description": (job.description or "")[:3000],
            },
            ensure_ascii=False,
            indent=2,
        )
    )

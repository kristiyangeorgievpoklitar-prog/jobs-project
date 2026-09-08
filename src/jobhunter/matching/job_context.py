"""Rendering a job posting into the text a matcher reasons over.

The posting is given to the model close to how a person would read it: the
metadata the site states outright, then the body. Site metadata is labelled as
such because it is frequently wrong — the level tag and the city in particular —
and the model is told elsewhere to prefer what the body actually says.
"""

from __future__ import annotations

import hashlib

from jobhunter.domain.enums import WorkMode
from jobhunter.domain.schemas import ClassificationResult, NormalizedJob

# Long postings are mostly benefits boilerplate after the requirements. Small
# models degrade sharply with context length, so the body is bounded.
DEFAULT_MAX_DESCRIPTION_CHARS = 5000


def render_job(
    job: NormalizedJob,
    *,
    max_description_chars: int = DEFAULT_MAX_DESCRIPTION_CHARS,
    classification: ClassificationResult | None = None,
) -> str:
    """A posting as the matcher sees it.

    ``classification`` is the deterministic classifier's read of the site's own
    tags. Measured on the labelled set it calls seniority and location right
    about 79% of the time, against 42% for the local model — so it is given to
    the model as evidence rather than discarded. It has not read the posting
    body, which is exactly what the model is for, so the prompt tells the model
    to override it whenever the body disagrees.
    """
    lines: list[str] = [f"Title: {job.title}"]

    if job.company_name:
        lines.append(f"Company: {job.company_name}")

    stated_location = job.location_raw or job.city or "not stated"
    lines.append(f"Location stated by the site: {stated_location}")

    # The site's own home-office field, verbatim. "Възможност за работа от вкъщи"
    # (home office *possible*) and "Дистанционна работа" (remote) are different
    # claims, and collapsing them into one enum loses the distinction the model
    # needs to tell a hybrid office job from a genuinely remote one.
    if job.work_mode_raw:
        lines.append(f"Home-office field on the site: {job.work_mode_raw}")
    elif job.work_mode is not WorkMode.UNKNOWN:
        lines.append(f"Work mode tag: {job.work_mode.value}")
    else:
        lines.append("Home-office field on the site: not set (assume onsite)")

    if job.level_raw:
        lines.append(f"Level tag on the site: {job.level_raw}")
    if job.experience_raw:
        lines.append(f"Experience stated: {job.experience_raw}")
    elif job.years_experience_required is not None:
        lines.append(f"Experience stated: from {job.years_experience_required:g} years")

    if job.employment_type.value != "unknown":
        lines.append(f"Employment type: {job.employment_type.value}")
    if job.salary.raw:
        lines.append(f"Salary: {job.salary.raw}")
    if job.languages:
        lines.append(f"Languages required by the site tags: {', '.join(job.languages)}")
    if job.tech_keywords:
        lines.append(f"Technology tags on the site: {', '.join(job.tech_keywords[:25])}")

    if classification is not None:
        lines.append("")
        lines.append("Rule-based pre-assessment (from the site tags only, may be wrong):")
        if classification.seniority.is_known:
            signals = ", ".join(classification.seniority_signals[:3]) or "no signals"
            lines.append(
                f"  entry-level bar: {classification.seniority.value} "
                f"(confidence {classification.seniority_confidence:.0%}; {signals})"
            )
        else:
            lines.append("  entry-level bar: could not be determined from the tags")
        location = {
            True: "reachable for the candidate",
            False: "NOT reachable for the candidate",
            None: "could not be determined",
        }[classification.location_relevant]
        lines.append(f"  location: {location}")

    body = (job.description or "").strip()
    if body:
        truncated = body[:max_description_chars]
        if len(body) > max_description_chars:
            truncated += "\n[...posting truncated...]"
        lines.append(f"\nPosting text:\n{truncated}")
    else:
        lines.append(
            "\nPosting text: NOT AVAILABLE. The description could not be fetched, "
            "so only the tags above are known about this job."
        )

    return "\n".join(lines)


def job_content_hash(job: NormalizedJob) -> str:
    """Hash of everything about the job that could change an evaluation.

    Re-running a scan re-discovers the same listings; without this the model
    would re-evaluate every unchanged posting on every run.
    """
    payload = render_job(job, max_description_chars=100_000)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def has_usable_description(job: NormalizedJob, *, minimum_chars: int = 300) -> bool:
    """Whether there is enough posting text to reason about requirements at all."""
    return len((job.description or "").strip()) >= minimum_chars

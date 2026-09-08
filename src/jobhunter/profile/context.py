"""Rendering the candidate into the text a matcher reasons over.

The matcher is only as good as what it knows about the candidate. A bare list of
skill tokens ("php, laravel, mysql") loses the two things that decide most junior
applications: whether the experience is commercial or academic, and what the
person actually built. Both live in the CV, so the CV is part of the context —
which is safe precisely because the default model runs locally.
"""

from __future__ import annotations

import hashlib
import re

from jobhunter.domain.schemas import CandidateSnapshot

# CV headings, in both languages, that mark the sections worth keeping.
_SECTION_PATTERNS: dict[str, tuple[str, ...]] = {
    "experience": ("experience", "employment", "опит", "трудов стаж", "професионален опит"),
    "projects": ("projects", "проекти", "личен проект", "portfolio"),
    "education": ("education", "образование"),
    "skills": ("technical skills", "skills", "умения", "технически умения"),
}

# Headings that end a section we are capturing.
_ANY_HEADING = (
    *(h for group in _SECTION_PATTERNS.values() for h in group),
    "languages",
    "езици",
    "summary",
    "профил",
    "цел",
    "допълнителни умения",
    "contact",
)

# Placeholder markers left in an unfilled CV template. A CV containing these is
# not a real document and must never reach an employer.
_PLACEHOLDER = re.compile(
    r"\[\s*(име|фамилия|град|телефон|имейл|линк|година|име на университет|name|city|phone|email|link)"
    r"[^\]]*\]",
    re.IGNORECASE,
)


def cv_has_placeholders(text: str | None) -> list[str]:
    """Unfilled template markers found in a CV, if any.

    ``CV_IT_Junior_BG.pdf`` in the shipped data is an untouched template whose
    "name" is literally "[Име Фамилия]". Attaching it to an application would be
    worse than sending nothing, so callers use this as a hard gate.
    """
    if not text:
        return []
    seen: list[str] = []
    for match in _PLACEHOLDER.findall(text):
        token = f"[{match}...]" if match else "[...]"
        if token not in seen:
            seen.append(token)
    return seen[:6]


def _is_heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > 60:
        return False
    lowered = stripped.lower().rstrip(":")
    return any(lowered.startswith(h) for h in _ANY_HEADING)


def extract_cv_sections(text: str | None) -> dict[str, str]:
    """Pull the evidence-bearing sections out of a CV's plain text.

    Deliberately simple: CVs vary too much for a parser to be reliable, so this
    keeps whole sections and lets the model read them, rather than pretending to
    extract structured fields it would get wrong.
    """
    if not text:
        return {}

    sections: dict[str, list[str]] = {}
    current: str | None = None

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        if _is_heading(stripped):
            lowered = stripped.lower().rstrip(":")
            current = None
            for name, markers in _SECTION_PATTERNS.items():
                if any(lowered.startswith(m) for m in markers):
                    current = name
                    sections.setdefault(name, [])
                    break
            continue

        if current is not None:
            sections[current].append(stripped)

    return {name: "\n".join(lines).strip() for name, lines in sections.items() if lines}


def _bullet_list(label: str, values: list[str]) -> str:
    return f"{label}: {', '.join(values)}" if values else ""


def render_candidate(
    candidate: CandidateSnapshot,
    *,
    cv_text: str | None = None,
    max_cv_chars: int = 1800,
) -> str:
    """The candidate as the matcher sees them: claims plus the evidence for them."""
    lines: list[str] = []

    lines.append(f"Location: {candidate.location or 'unknown'}")
    if candidate.preferred_locations:
        lines.append(f"Willing to work in: {', '.join(candidate.preferred_locations)}")
    lines.append(f"Open to remote: {'yes' if candidate.remote_ok else 'no'}")
    lines.append(
        f"Commercial experience: {candidate.years_experience:g} years "
        f"(targeting {candidate.desired_seniority.value.replace('_', '/')} roles)"
    )

    for label, values in (
        ("Programming languages", candidate.skills),
        ("Frameworks", candidate.frameworks),
        ("Databases", candidate.databases),
        ("Tools", candidate.tools),
    ):
        if line := _bullet_list(label, sorted(values)):
            lines.append(line)

    if candidate.languages:
        spoken = ", ".join(
            f"{item.get('name', '')} ({item.get('level', '')})".strip()
            for item in candidate.languages
            if item.get("name")
        )
        lines.append(f"Spoken languages: {spoken}")

    if candidate.education:
        study = "; ".join(
            " ".join(filter(None, (item.get("degree"), item.get("institution"))))
            for item in candidate.education
        )
        if study.strip():
            lines.append(f"Education: {study}")

    if candidate.summary:
        lines.append(f"\nSummary: {candidate.summary.strip()[:600]}")

    sections = extract_cv_sections(cv_text)
    for name in ("experience", "projects"):
        if body := sections.get(name):
            lines.append(f"\n{name.capitalize()} (from CV):\n{body[:max_cv_chars]}")

    return "\n".join(line for line in lines if line).strip()


def candidate_fingerprint(candidate: CandidateSnapshot, cv_text: str | None = None) -> str:
    """Stable hash of everything about the candidate that affects an evaluation.

    Used as part of the cache key, so editing the profile or swapping the CV
    invalidates evaluations that were made against the old version.
    """
    payload = render_candidate(candidate, cv_text=cv_text)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

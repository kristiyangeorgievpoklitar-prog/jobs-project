"""Checking the model's claims about the candidate against the candidate.

A small model sometimes credits the candidate with a technology they do not
have. Measured over 36 real evaluations, 5 did — claiming C#, Python, TypeScript
and WordPress for a profile listing none of them. One said outright that the
candidate "has experience with Python, which is a requirement, even though it is
not explicitly" stated, and asserted it anyway.

Four successive prompt versions failed to stop this, so it is checked in code
instead. The claims are flagged rather than deleted: the surrounding sentence is
usually a legitimate transferable-skill argument, and silently editing the
model's reasoning would hide the very thing the candidate needs to see. A named
warning lets them check it in seconds, which is the whole point of showing the
evidence.
"""

from __future__ import annotations

import re

from jobhunter.classify import keywords as K
from jobhunter.domain.evaluation import JobEvaluation
from jobhunter.domain.schemas import CandidateSnapshot
from jobhunter.matching.rules import expand_tech

# Only phrasings that assert the candidate possesses something. "Laravel is
# related to Spring" is a comparison, not a claim, and must not be flagged.
_POSSESSION = re.compile(
    r"(?:candidate|they|he|she)?\s*(?:has|have|is|are)\s+"
    r"(?:hands-on\s+|practical\s+|professional\s+|prior\s+|solid\s+|good\s+)?"
    r"(?:experience|experienced|knowledge|background|familiarity|skills?)\s+"
    r"(?:with|in|of)\s+([^.;]{0,120})",
    re.IGNORECASE,
)


def unverified_technologies(evaluation: JobEvaluation, candidate: CandidateSnapshot) -> list[str]:
    """Technologies the evaluation credits the candidate with, that they lack.

    Scans only the possession clause of a claim, so a sentence arguing that one
    technology transfers to another is left alone.
    """
    known = expand_tech(candidate.all_tech) | {
        "html",
        "css",
        "api",
        "testing",
        "debugging",
        "refactoring",
    }

    found: list[str] = []
    for text in evaluation.major_strengths:
        for raw_claim in _POSSESSION.findall(text):
            claim = _claim_subject(raw_claim)
            for term in K.ALL_TECH_KEYWORDS:
                if term in known or term in found:
                    continue
                pattern = re.escape(term)
                boundary = rf"(?<![\w#+.]){pattern}(?![\w#+.])" if term.isalnum() else pattern
                if re.search(boundary, claim.lower()):
                    found.append(term)
    return sorted(found)


# Where a claim stops being a list of what the candidate has and starts arguing
# about something else. "...has experience with PHP and Laravel, which are
# related to Python" claims PHP and Laravel, not Python.
_CLAIM_END = re.compile(
    r"\b(which|that|as it|since|because|similar to|related to|comparable|transferable)\b",
    re.IGNORECASE,
)


def _claim_subject(claim: str) -> str:
    """Just the part of a claim that lists what the candidate has."""
    return _CLAIM_END.split(claim, maxsplit=1)[0]


def verification_warning(unverified: list[str]) -> str:
    """The line shown beside an evaluation that credits you with something new."""
    if not unverified:
        return ""
    named = ", ".join(sorted(unverified)[:4])
    return (
        f"This assessment credits you with {named}, which your profile does not list. "
        "Check it before relying on it — or add it to your profile if it belongs there."
    )

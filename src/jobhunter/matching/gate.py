"""Stage 1: the cheap filter that decides what the model never has to see.

Running a local model costs tens of seconds per listing, so most of a scan's
listings must be settled without it. The rule that governs everything here: a
gate rejection is final and invisible — the candidate never sees the job and
never learns it existed — so the gate may only reject on grounds that are
*certain from the listing alone*.

That is why it deliberately does not look at technology overlap. Keyword overlap
between a CV and a posting is exactly the judgement that needs a reader, and the
previous system's worst failures came from treating a low overlap count as a
reason to discard a junior role. Skill fit is the model's job; the gate only
removes listings that no reading could rescue.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from jobhunter.classify import keywords as K
from jobhunter.classify.classifier import classify_job
from jobhunter.domain.enums import Seniority
from jobhunter.domain.schemas import CandidateSnapshot, NormalizedJob
from jobhunter.normalize.normalizer import normalize_text


class GateVerdict(StrEnum):
    PASS = "pass"
    REJECT = "reject"


@dataclass(frozen=True)
class GateResult:
    verdict: GateVerdict
    reason: str | None = None
    detail: str | None = None

    @property
    def passed(self) -> bool:
        return self.verdict is GateVerdict.PASS


@dataclass(frozen=True)
class GateConfig:
    """How aggressive the cheap filter is allowed to be."""

    # Titles at or above this level are rejected without reading the body. Set
    # generously: a "mid" listing can still be worth a junior's time, and only
    # unambiguous senior wording is used here anyway.
    reject_at_or_above: Seniority = Seniority.SENIOR
    # A non-IT verdict is only trusted above this confidence.
    non_it_confidence: float = 0.75
    require_it: bool = True


# Words in a title that mean senior on their own, in either language. Kept
# separate from the classifier's broader patterns because these must be
# unambiguous enough to reject on without reading the posting.
_UNAMBIGUOUS_SENIOR_TITLE = (
    "senior",
    "sr.",
    "lead",
    "principal",
    "staff engineer",
    "head of",
    "director",
    "manager",
    "architect",
    "старши",
    "ръководител",
    "мениджър",
)

# ...except where the word is part of a junior-facing phrase.
_SENIOR_EXCEPTIONS = ("junior", "младши", "стажант", "intern", "entry", "graduate")


def _title_is_clearly_senior(title: str) -> str | None:
    lowered = normalize_text(title)
    if any(exception in lowered for exception in _SENIOR_EXCEPTIONS):
        return None
    for marker in _UNAMBIGUOUS_SENIOR_TITLE:
        pattern = re.escape(marker)
        if re.search(rf"(?<![\w]){pattern}", lowered):
            return marker
    return None


def _title_has_it_role_word(title: str) -> bool:
    lowered = normalize_text(title)
    return any(kw in lowered for kw in K.IT_ROLE_KEYWORDS)


def _named_non_it_marker(title: str) -> str | None:
    """A recognised non-IT profession in the title, if there is one."""
    lowered = normalize_text(title)
    return next((m for m in K.NON_IT_MARKERS if m in lowered), None)


class Stage1Gate:
    """Rejects only what is certainly not worth a model call."""

    def __init__(self, config: GateConfig | None = None) -> None:
        self.config = config or GateConfig()

    def check(
        self,
        job: NormalizedJob,
        candidate: CandidateSnapshot,
        *,
        rejected_fingerprints: set[str] | None = None,
    ) -> GateResult:
        # 1. The candidate has already turned this exact listing down.
        if rejected_fingerprints and job.fingerprint in rejected_fingerprints:
            return GateResult(GateVerdict.REJECT, "already_rejected", "Previously skipped by you")

        # 2. Unambiguously senior wording in the title.
        if marker := _title_is_clearly_senior(job.title):
            return GateResult(
                GateVerdict.REJECT, "senior_title", f"Title states a senior role ({marker!r})"
            )

        # 3. Not a software role at all.
        #
        # This leans on the classifier's verdict rather than a list of non-IT job
        # titles, because the list can only ever name professions someone thought
        # of ("учител" is in it, "преподавател" is not) while the classifier reads
        # the whole listing. The title is still required to contain no IT role
        # word, so an unusual developer title is never gated away on this ground.
        if self.config.require_it and not _title_has_it_role_word(job.title):
            classification = classify_job(job, target_locations=[], remote_ok=True)
            if (
                classification.is_it is False
                and classification.it_confidence >= self.config.non_it_confidence
            ):
                named = _named_non_it_marker(job.title)
                detail = f"Not a software role ({named!r})" if named else "Not a software role"
                return GateResult(GateVerdict.REJECT, "not_it", detail)

        return GateResult(GateVerdict.PASS)

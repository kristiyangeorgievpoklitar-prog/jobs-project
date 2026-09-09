"""Versioned prompt templates, kept as files rather than inline strings.

Two reasons they live here instead of next to the code that calls them:

* a prompt is an input to the system's behaviour exactly like the model weights
  are, so a cached evaluation has to be invalidated when it changes — which
  needs a stable identity (``PromptTemplate.version``) and a content hash;
* prompts get edited far more often than the code around them, and reviewing
  that diff is much easier when it is not buried in Python quoting.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

TEMPLATE_DIR = Path(__file__).parent / "templates"

# Bumped whenever the meaning of a prompt changes. Cached evaluations produced
# under an older version are recomputed rather than trusted.
#
# The lineage, kept because each step was a response to something measured and
# the reasons are worth more than the diffs:
#
#   v1  first draft. Put responsibilities in mandatory_requirements, ignored
#       "considered an advantage, not a requirement", and overran its own caps.
#   v2  separated responsibilities from requirements; reordered the schema so
#       the evidence fields are generated before the decision.
#   v3  hardened against inventing skills, after qwen3:1.7b credited the
#       candidate with Rust, Python and React, none of which they have.
#   v4  taught it the site's own fields, in particular that "Възможност за
#       работа от вкъщи" is a hybrid office job and not a remote one.
#   v5  added a rule-based seniority and location pre-assessment. REGRESSED:
#       the seniority half anchored the model onto the level tag and turned a
#       correct SKIP on a Mid-Senior role into an APPLY.
#   v6  keeps the location check, drops the seniority pre-assessment, and says
#       outright that the level tag is the least reliable field on the page
#       because it is chosen to widen the applicant pool.
#   v7  stopped telling the model to hedge. Up to v6 the prompt said
#       "when genuinely torn, choose review", and every model obliged: on the
#       14-case screen llama3.2 and gemma3 returned ZERO skips and qwen2.5 two,
#       against 8 labelled skips. That made them statistically indistinguishable
#       from a stub that reviews everything. v7 states the base rate — most
#       listings are skips — lists concrete skip triggers, and defines "review"
#       as "I could not decide" rather than "I would rather not say".
#   v8  describes every field the schema demands. Six of the fourteen
#       required fields were never named in the prompt — the grammar forces the
#       model to emit them regardless, so it filled them blind. is_it_role came
#       back false for EVERY listing, including "Junior C++ Developer", and the
#       policy skipped all 41 benchmark cases as non-software work.
#   v9  current. v8 fixed the blind fields but regressed the decision: on the
#       same 14 cases it issued ZERO skips against v7's eight, and accuracy fell
#       57% -> 29%. The field list sits at the end of the prompt, so the last
#       thing read before answering was a bare "decision: apply, review or skip"
#       with the anti-hedging rules far above it. v9 restates them at the point
#       of use, keeping v8's glossary — which lifted seniority to 86%, the best
#       measured.
JOB_EVALUATION_VERSION = "v9"
COVER_LETTER_VERSION = "v1"


class PromptNotFoundError(LookupError):
    """Raised when a named prompt version has no template on disk."""


@dataclass(frozen=True)
class PromptTemplate:
    """One versioned prompt: a system message and a user-message template."""

    name: str
    version: str
    system: str
    user_template: str

    @property
    def checksum(self) -> str:
        """Content hash, so an edited prompt invalidates cached evaluations."""
        payload = f"{self.name}:{self.version}:{self.system}:{self.user_template}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    @property
    def identity(self) -> str:
        """Short identifier stored alongside every evaluation."""
        return f"{self.name}.{self.version}.{self.checksum}"

    def render_user(self, **values: str) -> str:
        return self.user_template.format(**values)


@lru_cache(maxsize=16)
def load_prompt(name: str, version: str) -> PromptTemplate:
    """Read a prompt pair off disk. Cached: templates do not change at runtime."""
    system_path = TEMPLATE_DIR / f"{name}.{version}.system.txt"
    user_path = TEMPLATE_DIR / f"{name}.{version}.user.txt"

    if not system_path.exists() or not user_path.exists():
        raise PromptNotFoundError(f"No prompt {name!r} version {version!r} in {TEMPLATE_DIR}")

    return PromptTemplate(
        name=name,
        version=version,
        system=system_path.read_text(encoding="utf-8").strip(),
        user_template=user_path.read_text(encoding="utf-8").strip(),
    )


def job_evaluation_prompt(version: str | None = None) -> PromptTemplate:
    return load_prompt("job_evaluation", version or JOB_EVALUATION_VERSION)


def cover_letter_prompt(version: str | None = None) -> PromptTemplate:
    return load_prompt("cover_letter", version or COVER_LETTER_VERSION)


def available_versions(name: str) -> list[str]:
    """Every version of ``name`` present on disk, for the benchmark to sweep."""
    versions = {
        path.name.split(".")[1]
        for path in TEMPLATE_DIR.glob(f"{name}.*.system.txt")
        if len(path.name.split(".")) >= 4
    }
    return sorted(versions)


__all__ = [
    "COVER_LETTER_VERSION",
    "JOB_EVALUATION_VERSION",
    "PromptNotFoundError",
    "PromptTemplate",
    "available_versions",
    "cover_letter_prompt",
    "job_evaluation_prompt",
    "load_prompt",
]

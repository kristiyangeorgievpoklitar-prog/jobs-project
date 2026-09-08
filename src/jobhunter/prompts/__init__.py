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
JOB_EVALUATION_VERSION = "v6"
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

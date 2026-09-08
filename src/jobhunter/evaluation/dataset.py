"""The labelled benchmark set: real postings with a human verdict attached.

Without this the project cannot answer its own question. "The AI matcher is
better" is only a claim until there is a fixed set of cases, labelled once, that
every matcher is run against — including the old one, so the comparison is
honest rather than a demo of the new thing working on examples chosen after the
fact.

Cases are real Jobs.bg listings rather than invented ones. Invented postings
quietly encode the assumptions of whoever wrote them, and a matcher tuned on
them looks excellent right up until it meets a real listing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobhunter.domain.enums import Seniority
from jobhunter.domain.evaluation import Decision, LocationFit
from jobhunter.domain.schemas import NormalizedJob, RawJob
from jobhunter.normalize.normalizer import normalize_job

DEFAULT_DATASET_PATH = Path(__file__).resolve().parents[3] / "evaluation" / "dataset.json"


@dataclass(frozen=True)
class CaseLabel:
    """The human verdict for one case.

    ``decision`` is the field that matters; the rest are the intermediate claims
    worth checking separately, because a matcher can reach the right decision
    through the wrong reasoning and that will not generalise.
    """

    decision: Decision
    is_it_role: bool
    seniority: Seniority
    location_fit: LocationFit
    notes: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CaseLabel:
        return cls(
            decision=Decision(data["decision"]),
            is_it_role=bool(data["is_it_role"]),
            seniority=Seniority(data["seniority"]),
            location_fit=LocationFit(data["location_fit"]),
            notes=data.get("notes", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "is_it_role": self.is_it_role,
            "seniority": self.seniority.value,
            "location_fit": self.location_fit.value,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class BenchmarkCase:
    """One labelled posting."""

    id: str
    title: str
    company: str | None
    location_raw: str | None
    description: str | None
    label: CaseLabel
    tags: list[str] = field(default_factory=list)
    source_url: str | None = None
    level_raw: str | None = None
    experience_raw: str | None = None
    salary_raw: str | None = None
    employment_raw: str | None = None
    work_mode_raw: str | None = None
    tech_tags: list[str] = field(default_factory=list)
    languages_raw: list[str] = field(default_factory=list)

    def to_normalized_job(self) -> NormalizedJob:
        """Rebuild the pipeline object, so matchers see exactly what they see live."""
        raw = RawJob(
            source_url=self.source_url or f"https://www.jobs.bg/job/{self.id}",
            title=self.title,
            company_name=self.company,
            location_raw=self.location_raw,
            description=self.description,
            level_raw=self.level_raw,
            experience_raw=self.experience_raw,
            salary_raw=self.salary_raw,
            employment_raw=self.employment_raw,
            work_mode_raw=self.work_mode_raw,
            tech_tags=self.tech_tags,
            languages_raw=self.languages_raw,
        )
        return normalize_job(raw)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BenchmarkCase:
        return cls(
            id=data["id"],
            title=data["title"],
            company=data.get("company"),
            location_raw=data.get("location_raw"),
            description=data.get("description"),
            label=CaseLabel.from_dict(data["label"]),
            tags=list(data.get("tags", [])),
            source_url=data.get("source_url"),
            level_raw=data.get("level_raw"),
            experience_raw=data.get("experience_raw"),
            salary_raw=data.get("salary_raw"),
            employment_raw=data.get("employment_raw"),
            work_mode_raw=data.get("work_mode_raw"),
            tech_tags=list(data.get("tech_tags", [])),
            languages_raw=list(data.get("languages_raw", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "company": self.company,
            "location_raw": self.location_raw,
            "source_url": self.source_url,
            "level_raw": self.level_raw,
            "experience_raw": self.experience_raw,
            "salary_raw": self.salary_raw,
            "employment_raw": self.employment_raw,
            "work_mode_raw": self.work_mode_raw,
            "tech_tags": self.tech_tags,
            "languages_raw": self.languages_raw,
            "tags": self.tags,
            "label": self.label.to_dict(),
            "description": self.description,
        }


@dataclass(frozen=True)
class Dataset:
    """A named collection of labelled cases."""

    cases: list[BenchmarkCase]
    candidate_note: str = ""

    def __len__(self) -> int:
        return len(self.cases)

    def filter_by_tag(self, tag: str) -> Dataset:
        return Dataset([c for c in self.cases if tag in c.tags], self.candidate_note)

    @property
    def tags(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for case in self.cases:
            for tag in case.tags:
                counts[tag] = counts.get(tag, 0) + 1
        return dict(sorted(counts.items()))

    @property
    def decision_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for case in self.cases:
            key = case.label.decision.value
            counts[key] = counts.get(key, 0) + 1
        return counts


def load_dataset(path: Path | None = None) -> Dataset:
    path = path or DEFAULT_DATASET_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"No benchmark dataset at {path}. Build one with `jobhunter dataset build`."
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    return Dataset(
        cases=[BenchmarkCase.from_dict(item) for item in payload["cases"]],
        candidate_note=payload.get("candidate_note", ""),
    )


def save_dataset(dataset: Dataset, path: Path | None = None) -> Path:
    path = path or DEFAULT_DATASET_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "candidate_note": dataset.candidate_note,
        "case_count": len(dataset.cases),
        "cases": [case.to_dict() for case in dataset.cases],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path

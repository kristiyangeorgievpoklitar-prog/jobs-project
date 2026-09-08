"""Turning labelled job ids into a self-contained benchmark file.

The dataset embeds the full posting text rather than referencing the database,
so the benchmark stays reproducible after the local database is re-scanned,
cleared, or moved to another machine.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select

from jobhunter.db.models import Job
from jobhunter.evaluation.dataset import BenchmarkCase, CaseLabel, Dataset, save_dataset

DEFAULT_LABELS_PATH = Path(__file__).resolve().parents[3] / "evaluation" / "labels.json"


class MissingJobError(LookupError):
    """A labelled job id is not in the database."""


def build_dataset(session, labels_path: Path | None = None) -> Dataset:
    """Join the human labels with the stored postings."""
    labels_path = labels_path or DEFAULT_LABELS_PATH
    payload = json.loads(labels_path.read_text(encoding="utf-8"))
    labels: dict[str, dict] = payload["labels"]

    cases: list[BenchmarkCase] = []
    missing: list[str] = []

    for job_id, label_data in labels.items():
        job = session.get(Job, int(job_id))
        if job is None:
            missing.append(job_id)
            continue

        cases.append(
            BenchmarkCase(
                id=f"job-{job_id}",
                title=job.title,
                company=job.company_name_raw or (job.company.name if job.company else None),
                location_raw=job.location_raw,
                description=job.description,
                source_url=job.source_url,
                salary_raw=job.salary_raw,
                # The site's own tags. The live pipeline has these, so denying
                # them to the benchmark would measure a harder problem than the
                # one the system actually solves.
                level_raw=job.level_raw,
                experience_raw=job.experience_raw,
                work_mode_raw=job.work_mode_raw,
                languages_raw=list(job.languages_raw or []),
                tech_tags=list(job.tech_keywords or []),
                tags=list(label_data.get("tags", [])),
                label=CaseLabel.from_dict(label_data),
            )
        )

    if missing:
        raise MissingJobError(f"Labelled jobs not found in the database: {', '.join(missing)}")

    return Dataset(cases=cases, candidate_note=payload.get("_candidate", ""))


def build_and_save(session, *, labels_path: Path | None = None, out: Path | None = None) -> Path:
    dataset = build_dataset(session, labels_path)
    return save_dataset(dataset, out)


def _job_rows(session) -> list[Job]:
    return list(session.scalars(select(Job)).all())

"""Shared test fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from jobhunter.config import Settings
from jobhunter.db.base import Database
from jobhunter.domain.enums import Seniority
from jobhunter.domain.schemas import CandidateSnapshot, NormalizedJob, RawJob
from jobhunter.normalize.normalizer import normalize_job

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def listing_html() -> str:
    return (FIXTURES / "listing_varna_it.html").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def detail_internal_html() -> str:
    return (FIXTURES / "detail_internal.html").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def detail_external_html() -> str:
    return (FIXTURES / "detail_external.html").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def apply_form_html() -> str:
    return (FIXTURES / "apply_questionnaire.html").read_text(encoding="utf-8")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointed entirely at a temporary directory."""
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        screenshots_dir=tmp_path / "shots",
        browser_profile_dir=tmp_path / "profile",
        database_url="sqlite:///:memory:",
        notify_console=False,
        notify_dashboard=False,
    )


@pytest.fixture
def db() -> Iterator[Database]:
    database = Database("sqlite:///:memory:")
    database.create_all()
    yield database
    database.dispose()


@pytest.fixture
def session(db: Database) -> Iterator[Session]:
    with db.session() as active:
        yield active


@pytest.fixture
def candidate() -> CandidateSnapshot:
    """A junior PHP/Laravel candidate in Varna."""
    return CandidateSnapshot(
        full_name="Test Candidate",
        location="Varna",
        years_experience=1.0,
        desired_seniority=Seniority.JUNIOR_MID,
        preferred_locations=["Varna"],
        remote_ok=True,
        skills=["php", "javascript", "sql", "html", "css"],
        frameworks=["laravel", "livewire", "tailwind"],
        databases=["mysql"],
        tools=["git", "github"],
        languages=[
            {"name": "Bulgarian", "level": "native"},
            {"name": "English", "level": "professional"},
        ],
        summary="Junior developer with Laravel experience.",
    )


def make_job(**overrides: object) -> NormalizedJob:
    """Build a NormalizedJob for tests, with sensible defaults."""
    source_job_id = overrides.pop("source_job_id", "1000")
    default_url = (
        f"https://www.jobs.bg/job/{source_job_id}" if source_job_id else "https://www.jobs.bg/job/x"
    )
    raw = RawJob(
        source_job_id=str(source_job_id) if source_job_id else None,
        source_url=str(overrides.pop("source_url", default_url)),
        title=str(overrides.pop("title", "Junior PHP Developer")),
        company_name=str(overrides.pop("company_name", "Example Ltd")),
        location_raw=overrides.pop("location_raw", "Варна"),  # type: ignore[arg-type]
        description=overrides.pop("description", "We need a Junior PHP developer. " * 20),  # type: ignore[arg-type]
        level_raw=overrides.pop("level_raw", "Ниво Junior, Mid-level"),  # type: ignore[arg-type]
        experience_raw=overrides.pop("experience_raw", "Години опит от 1 до 3"),  # type: ignore[arg-type]
        tech_tags=list(overrides.pop("tech_tags", ["PHP", "Laravel", "MySQL"])),  # type: ignore[arg-type]
        **overrides,  # type: ignore[arg-type]
    )
    return normalize_job(raw)


@pytest.fixture
def job_factory():
    return make_job

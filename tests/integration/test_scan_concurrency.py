"""The database must stay writable while the model is thinking.

Inference takes minutes per listing and SQLite allows exactly one writer at a
time. Holding the scan's write transaction across a model call made every other
write fail — measured, a dashboard click during a scan failed in 5.0 seconds
with "database is locked". These tests pin the fix, because the failure only
appears under a combination (a long call, a file-backed database, a second
writer) that no single-threaded test would ever produce by accident.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from sqlalchemy import text

from jobhunter.ai.local_model import LocalModelConfig, LocalModelProvider
from jobhunter.db.base import Database
from jobhunter.db.models import Job
from jobhunter.domain.schemas import CandidateSnapshot, RawJob
from jobhunter.matching.evaluator import JobEvaluator
from jobhunter.normalize.normalizer import normalize_job

RESPONSE = json.dumps(
    {
        "is_it_role": True,
        "seniority": "junior",
        "seniority_reasoning": "Junior bar.",
        "location_fit": "exact_city",
        "location_reasoning": "Varna.",
        "experience_fit": "acceptable",
        "mandatory_requirements": [{"requirement": "PHP", "candidate_fit": "strong"}],
        "nice_to_have_requirements": [],
        "major_strengths": ["PHP"],
        "major_risks": ["Little experience"],
        "reasoning": "Good overlap.",
        "decision": "review",
        "confidence": 0.7,
        "recommendation": "Worth a look.",
    }
)


@pytest.fixture
def file_database(tmp_path):
    """A real file, because the whole point is cross-connection locking."""
    database = Database(f"sqlite:///{tmp_path / 'jobhunter.db'}")
    database.create_all()
    yield database, tmp_path / "jobhunter.db"
    database.dispose()


class WritingDuringInference(LocalModelProvider):
    """Stands in for a slow model, and writes from another connection mid-call.

    That second connection is the dashboard recording your feedback while a scan
    is running.
    """

    def __init__(self, db_path) -> None:
        super().__init__(LocalModelConfig(model="stub:test"))
        self.db_path = db_path
        self.other_writer_failed: Exception | None = None

    def _chat(self, system: str, user: str) -> tuple[str, int]:
        other = sqlite3.connect(self.db_path, timeout=3.0)
        try:
            other.execute("PRAGMA journal_mode=WAL")
            other.execute(
                "INSERT INTO user_feedback (job_id, action, created_at) "
                "VALUES (1, 'skip', datetime('now'))"
            )
            other.commit()
        except Exception as exc:  # recorded, not raised: the assertion belongs in the test
            self.other_writer_failed = exc
        finally:
            other.close()
        return RESPONSE, 10


def seed(session) -> tuple[int, object]:
    normalized = normalize_job(
        RawJob(
            source_url="https://www.jobs.bg/job/1",
            title="Junior PHP Developer",
            company_name="Acme",
            location_raw="Варна",
            description="Requirements: PHP, Laravel and MySQL. " * 30,
        )
    )
    job = Job(
        fingerprint=normalized.fingerprint,
        source=normalized.source,
        source_url=normalized.source_url,
        normalized_url=normalized.normalized_url,
        title=normalized.title,
        title_normalized=normalized.title_normalized,
        company_name_raw=normalized.company_name,
        description=normalized.description,
        city=normalized.city,
        seen_count=1,
    )
    session.add(job)
    session.flush()
    return job.id, normalized


def test_feedback_can_still_be_recorded_while_the_model_is_running(file_database):
    """The failure this was written for: a dashboard click during a scan."""
    database, db_path = file_database
    provider = WritingDuringInference(db_path)
    candidate = CandidateSnapshot(location="Varna", preferred_locations=["Varna"], skills=["php"])

    with database.session() as session:
        job_id, normalized = seed(session)
        # The scan has already written to this session, exactly as the pipeline
        # does with upsert_job and the CLASSIFIED transition.
        session.execute(Job.__table__.update().values(seen_count=2))

        JobEvaluator(provider).evaluate(session, job_id, normalized, candidate)

    assert provider.other_writer_failed is None, (
        f"a concurrent write failed during inference: {provider.other_writer_failed}"
    )


def test_the_evaluation_is_still_persisted(file_database):
    """Committing early must not lose the result."""
    database, db_path = file_database
    provider = WritingDuringInference(db_path)
    candidate = CandidateSnapshot(location="Varna", preferred_locations=["Varna"], skills=["php"])

    with database.session() as session:
        job_id, normalized = seed(session)
        evaluation = JobEvaluator(provider).evaluate(session, job_id, normalized, candidate)

    assert evaluation.decision.value == "review"

    with database.session() as session:
        stored = session.execute(Job.__table__.select().where(Job.__table__.c.id == job_id)).first()
        assert stored is not None

    rows = (
        sqlite3.connect(db_path)
        .execute("SELECT decision, is_current FROM job_evaluations WHERE job_id = ?", (job_id,))
        .fetchall()
    )
    assert rows == [("review", 1)]


def test_the_busy_timeout_is_long_enough_to_ride_out_a_commit(file_database):
    """30s, not the 5s default: a scan commits between model calls."""
    database, _ = file_database
    with database.session() as session:
        timeout = session.execute(text("PRAGMA busy_timeout")).scalar()
    assert timeout == 30000

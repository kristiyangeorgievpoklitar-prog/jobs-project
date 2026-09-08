"""Database behaviour: upserts, duplicate detection and cascades."""

from __future__ import annotations

from sqlalchemy import select

from jobhunter.applications.dedupe import find_existing_job, has_applied
from jobhunter.db.models import Application, Company, Job, JobMatch
from jobhunter.domain.enums import JobState, Recommendation
from jobhunter.domain.schemas import MatchResult
from jobhunter.pipeline.repository import record_match, upsert_company, upsert_job
from tests.conftest import make_job


class TestUpsertCompany:
    def test_creates_once_and_reuses(self, session) -> None:
        first = upsert_company(session, "Example EOOD")
        second = upsert_company(session, "Example ЕООД")
        assert first is not None and second is not None
        assert first.id == second.id
        assert session.scalar(select(Company).where(Company.id == first.id)) is not None

    def test_blank_name_is_ignored(self, session) -> None:
        assert upsert_company(session, "") is None
        assert upsert_company(session, None) is None


class TestUpsertJob:
    def test_inserts_new_job(self, session) -> None:
        job, is_new = upsert_job(session, make_job(source_job_id="1"))
        assert is_new is True
        assert job.id is not None
        assert job.state is JobState.DISCOVERED
        assert job.seen_count == 1

    def test_second_sighting_updates_rather_than_duplicates(self, session) -> None:
        first, _ = upsert_job(session, make_job(source_job_id="1"))
        second, is_new = upsert_job(session, make_job(source_job_id="1"))
        assert is_new is False
        assert first.id == second.id
        assert second.seen_count == 2
        assert session.scalar(select(Job).where(Job.id == first.id)) is not None
        assert len(session.scalars(select(Job)).all()) == 1

    def test_backfills_missing_description(self, session) -> None:
        upsert_job(session, make_job(source_job_id="1", description=None))
        job, _ = upsert_job(
            session, make_job(source_job_id="1", description="Now with detail. " * 20)
        )
        assert job.description is not None

    def test_url_variations_are_one_job(self, session) -> None:
        upsert_job(session, make_job(source_job_id=None, source_url="https://www.jobs.bg/job/55"))
        _, is_new = upsert_job(
            session, make_job(source_job_id=None, source_url="https://jobs.bg/job/55/?utm_source=x")
        )
        assert is_new is False

    def test_relisting_under_a_new_id_is_a_separate_row(self, session) -> None:
        """A fresh listing id is a fresh listing; re-applying is blocked elsewhere."""
        upsert_job(
            session,
            make_job(source_job_id="100", title="Junior PHP Developer", company_name="Acme EOOD"),
        )
        _, is_new = upsert_job(
            session,
            make_job(source_job_id="200", title="Junior PHP Developer", company_name="Acme ЕООД"),
        )
        assert is_new is True

    def test_company_and_title_identify_a_job_with_no_source_id(self, session) -> None:
        upsert_job(
            session,
            make_job(
                source_job_id=None,
                source_url="https://www.jobs.bg/x/1",
                title="Junior PHP Developer",
                company_name="Acme EOOD",
            ),
        )
        _, is_new = upsert_job(
            session,
            make_job(
                source_job_id=None,
                source_url="https://www.jobs.bg/x/2",
                title="Junior PHP Developer",
                company_name="Acme ЕООД",
            ),
        )
        assert is_new is False

    def test_different_jobs_stay_separate(self, session) -> None:
        upsert_job(session, make_job(source_job_id="1", title="Junior PHP Developer"))
        _, is_new = upsert_job(session, make_job(source_job_id="2", title="Senior Java Developer"))
        assert is_new is True
        assert len(session.scalars(select(Job)).all()) == 2


class TestFindExistingJob:
    def test_by_each_key(self, session) -> None:
        job, _ = upsert_job(
            session, make_job(source_job_id="42", source_url="https://www.jobs.bg/job/42")
        )
        assert find_existing_job(session, fingerprint=job.fingerprint) is not None
        assert find_existing_job(session, source_job_id="42") is not None
        assert find_existing_job(session, normalized_url="https://jobs.bg/job/42") is not None
        assert (
            find_existing_job(session, company_name="Example Ltd", title="Junior PHP Developer")
            is not None
        )

    def test_returns_none_when_absent(self, session) -> None:
        assert find_existing_job(session, source_job_id="does-not-exist") is None


class TestMatches:
    def test_append_only_history(self, session) -> None:
        job, _ = upsert_job(session, make_job())
        for score in (60, 75, 90):
            record_match(
                session, job, MatchResult(score=score, recommendation=Recommendation.REVIEW)
            )
        matches = session.scalars(select(JobMatch).where(JobMatch.job_id == job.id)).all()
        assert len(matches) == 3
        session.refresh(job)
        assert job.latest_match is not None and job.latest_match.score == 90

    def test_cascade_delete(self, session) -> None:
        job, _ = upsert_job(session, make_job())
        record_match(session, job, MatchResult(score=50))
        session.delete(job)
        session.flush()
        assert session.scalars(select(JobMatch)).all() == []


class TestDuplicateApplicationGuard:
    def test_clean_job_is_not_duplicate(self, session) -> None:
        job, _ = upsert_job(session, make_job())
        assert has_applied(session, job).is_duplicate is False

    def test_applied_state_blocks(self, session) -> None:
        job, _ = upsert_job(session, make_job())
        job.state = JobState.APPLIED
        session.flush()
        assert has_applied(session, job).is_duplicate is True

    def test_successful_application_blocks(self, session) -> None:
        job, _ = upsert_job(session, make_job())
        session.add(Application(job_id=job.id, success=True, state=JobState.APPLIED))
        session.flush()
        assert has_applied(session, job).is_duplicate is True

    def test_in_flight_application_blocks(self, session) -> None:
        job, _ = upsert_job(session, make_job())
        session.add(Application(job_id=job.id, state=JobState.APPLYING))
        session.flush()
        assert has_applied(session, job).is_duplicate is True

    def test_failed_application_does_not_block_retry(self, session) -> None:
        job, _ = upsert_job(session, make_job())
        session.add(Application(job_id=job.id, success=False, state=JobState.FAILED))
        session.flush()
        assert has_applied(session, job).is_duplicate is False

    def test_same_role_at_same_company_blocks(self, session) -> None:
        """Applying once to a role must block a duplicate listing of it."""
        first, _ = upsert_job(
            session,
            make_job(source_job_id="1", title="Junior PHP Developer", company_name="Acme Ltd"),
        )
        session.add(Application(job_id=first.id, success=True, state=JobState.APPLIED))
        session.flush()

        second, _ = upsert_job(
            session,
            make_job(
                source_job_id="2",
                title="Junior PHP Developer",
                company_name="Acme Ltd",
                source_url="https://www.jobs.bg/job/2",
            ),
        )
        check = has_applied(session, second)
        assert check.is_duplicate is True

    def test_one_application_row_per_job(self, session) -> None:
        import pytest
        from sqlalchemy.exc import IntegrityError

        job, _ = upsert_job(session, make_job())
        session.add(Application(job_id=job.id))
        session.flush()
        session.add(Application(job_id=job.id))
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

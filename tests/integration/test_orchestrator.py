"""Application orchestration: duplicate guards, state flow and outcome recording."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from sqlalchemy import select

from jobhunter.applications import orchestrator as orch_module
from jobhunter.applications.orchestrator import ApplicationOrchestrator, job_to_normalized
from jobhunter.context import AppContext
from jobhunter.db.models import Application, ApplicationEvent, CVFile, Job
from jobhunter.domain.enums import ApplicationMethod, JobState, Language
from jobhunter.domain.schemas import ApplicationOutcome
from jobhunter.pipeline.repository import upsert_job
from jobhunter.profile.profile_store import update_profile
from tests.conftest import make_job


class FakeBrowser:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.started = False

    def start(self) -> FakeBrowser:
        self.started = True
        return self

    def stop(self) -> None:
        self.started = False

    def __enter__(self) -> FakeBrowser:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


class FakeApplier:
    """Records how it was called and returns a scripted outcome."""

    last_call: ClassVar[dict[str, Any]] = {}

    def __init__(self, outcome: ApplicationOutcome) -> None:
        self.outcome = outcome

    def __call__(self, browser: Any) -> FakeApplier:
        return self

    def apply(self, job_url: str, **kwargs: Any) -> ApplicationOutcome:
        FakeApplier.last_call = {"url": job_url, **kwargs}
        return self.outcome


@pytest.fixture
def context(settings, monkeypatch) -> AppContext:
    ctx = AppContext(settings, configure_logs=False)
    ctx.db.create_all()
    with ctx.session() as session:
        update_profile(
            session,
            {
                "full_name": "Test Candidate",
                "email": "test@example.com",
                "phone": "+359888000000",
                "location": "Varna",
                "preferred_locations": ["Varna"],
                "skills": ["php"],
                "frameworks": ["laravel"],
            },
        )
    monkeypatch.setattr(orch_module, "BrowserManager", FakeBrowser)
    return ctx


def install_applier(monkeypatch, outcome: ApplicationOutcome) -> FakeApplier:
    applier = FakeApplier(outcome)
    monkeypatch.setattr(orch_module, "JobsBgApplier", applier)
    return applier


def seed_job(context: AppContext, **overrides: Any) -> int:
    with context.session() as session:
        job, _ = upsert_job(session, make_job(**overrides))
        job.state = JobState.APPROVED
        job.application_method = ApplicationMethod.JOBSBG_INTERNAL
        session.flush()
        return job.id


class TestDuplicatePrevention:
    def test_never_applies_twice_to_the_same_job(self, context, monkeypatch) -> None:
        install_applier(monkeypatch, ApplicationOutcome(success=True, evidence="confirmed"))
        job_id = seed_job(context)
        orchestrator = ApplicationOrchestrator(context)

        first = orchestrator.apply_to_job(job_id, submit=True)
        assert first.success is True

        second = orchestrator.apply_to_job(job_id, submit=True)
        assert second.success is False
        assert "duplicate" in (second.failure_reason or "").lower()

        with context.session() as session:
            assert session.get(Job, job_id).state is JobState.APPLIED

    def test_blocks_the_same_role_under_a_different_listing(self, context, monkeypatch) -> None:
        install_applier(monkeypatch, ApplicationOutcome(success=True, evidence="ok"))
        first_id = seed_job(
            context, source_job_id="100", title="Junior PHP Developer", company_name="Acme EOOD"
        )
        ApplicationOrchestrator(context).apply_to_job(first_id, submit=True)

        second_id = seed_job(
            context, source_job_id="200", title="Junior PHP Developer", company_name="Acme ЕООД"
        )
        outcome = ApplicationOrchestrator(context).apply_to_job(second_id, submit=True)
        assert outcome.success is False
        assert "duplicate" in (outcome.failure_reason or "").lower()

    def test_missing_job_is_reported(self, context, monkeypatch) -> None:
        install_applier(monkeypatch, ApplicationOutcome())
        outcome = ApplicationOrchestrator(context).apply_to_job(999999)
        assert "not found" in (outcome.failure_reason or "").lower()

    def test_refuses_a_job_in_a_non_applyable_state(self, context, monkeypatch) -> None:
        install_applier(monkeypatch, ApplicationOutcome())
        job_id = seed_job(context)
        with context.session() as session:
            session.get(Job, job_id).state = JobState.SKIPPED
        outcome = ApplicationOrchestrator(context).apply_to_job(job_id)
        assert "not applyable" in (outcome.failure_reason or "").lower()


class TestOutcomeRecording:
    def test_success_marks_applied_with_evidence(self, context, monkeypatch) -> None:
        install_applier(monkeypatch, ApplicationOutcome(success=True, evidence="thank you page"))
        job_id = seed_job(context)
        ApplicationOrchestrator(context).apply_to_job(job_id, submit=True)

        with context.session() as session:
            application = session.scalar(select(Application).where(Application.job_id == job_id))
            assert application.success is True
            assert application.submitted_at is not None
            assert application.confirmation_evidence == "thank you page"
            assert session.get(Job, job_id).state is JobState.APPLIED

    def test_blocked_outcome_marks_blocked_not_applied(self, context, monkeypatch) -> None:
        install_applier(
            monkeypatch,
            ApplicationOutcome(success=False, blocked=True, failure_reason="reCAPTCHA gate"),
        )
        job_id = seed_job(context)
        ApplicationOrchestrator(context).apply_to_job(job_id, submit=True)

        with context.session() as session:
            assert session.get(Job, job_id).state is JobState.BLOCKED
            application = session.scalar(select(Application).where(Application.job_id == job_id))
            assert application.success is False
            assert "reCAPTCHA" in application.failure_reason

    def test_manual_step_is_flagged_for_the_user(self, context, monkeypatch) -> None:
        install_applier(
            monkeypatch,
            ApplicationOutcome(
                success=False, requires_manual_step=True, failure_reason="questions"
            ),
        )
        job_id = seed_job(context)
        ApplicationOrchestrator(context).apply_to_job(job_id)

        with context.session() as session:
            application = session.scalar(select(Application).where(Application.job_id == job_id))
            assert application.is_manual is True
            assert session.get(Job, job_id).state is JobState.BLOCKED

    def test_failure_marks_failed_and_allows_retry(self, context, monkeypatch) -> None:
        install_applier(monkeypatch, ApplicationOutcome(success=False, failure_reason="boom"))
        job_id = seed_job(context)
        ApplicationOrchestrator(context).apply_to_job(job_id, submit=True)

        with context.session() as session:
            assert session.get(Job, job_id).state is JobState.FAILED

        install_applier(monkeypatch, ApplicationOutcome(success=True, evidence="ok"))
        retry = ApplicationOrchestrator(context).apply_to_job(job_id, submit=True)
        assert retry.success is True

    def test_writes_a_full_event_trail(self, context, monkeypatch) -> None:
        install_applier(monkeypatch, ApplicationOutcome(success=True, evidence="ok"))
        job_id = seed_job(context)
        ApplicationOrchestrator(context).apply_to_job(job_id, submit=True)

        with context.session() as session:
            events = [
                e.event
                for e in session.scalars(
                    select(ApplicationEvent).where(ApplicationEvent.job_id == job_id)
                ).all()
            ]
        assert "apply_started" in events
        assert "applied" in events

    def test_review_is_promoted_through_approved(self, context, monkeypatch) -> None:
        """An explicit apply is an approval; the state machine is not jumped."""
        install_applier(monkeypatch, ApplicationOutcome(success=True, evidence="ok"))
        job_id = seed_job(context)
        with context.session() as session:
            session.get(Job, job_id).state = JobState.REVIEW

        ApplicationOrchestrator(context).apply_to_job(job_id, submit=True)
        with context.session() as session:
            events = [
                e.event
                for e in session.scalars(
                    select(ApplicationEvent).where(ApplicationEvent.job_id == job_id)
                ).all()
            ]
        assert "approved_for_apply" in events


class TestPreparation:
    def test_passes_profile_details_and_a_cover_letter(self, context, monkeypatch) -> None:
        applier = install_applier(monkeypatch, ApplicationOutcome(requires_manual_step=True))
        job_id = seed_job(context)
        ApplicationOrchestrator(context).apply_to_job(job_id)

        call = applier.last_call
        assert call["full_name"] == "Test Candidate"
        assert call["email"] == "test@example.com"
        assert call["cover_letter"]
        assert call["submit"] is False

    def test_selects_a_cv_matching_the_job_language(self, context, monkeypatch, tmp_path) -> None:
        install_applier(monkeypatch, ApplicationOutcome(requires_manual_step=True))
        bg = tmp_path / "cv_bg.txt"
        bg.write_text("Българска автобиография", encoding="utf-8")
        with context.session() as session:
            from jobhunter.profile.cv import register_cv

            register_cv(session, bg, is_default=True, language=Language.BG)

        job_id = seed_job(
            context, title="Младши програмист", description="Търсим програмист. " * 20
        )
        with context.session() as session:
            session.get(Job, job_id).language = Language.BG

        ApplicationOrchestrator(context).apply_to_job(job_id)
        with context.session() as session:
            application = session.scalar(select(Application).where(Application.job_id == job_id))
            cv = session.get(CVFile, application.cv_file_id)
            assert cv.language is Language.BG

    def test_batch_apply_is_a_no_op_while_auto_apply_is_off(self, context, monkeypatch) -> None:
        install_applier(monkeypatch, ApplicationOutcome(success=True))
        seed_job(context)
        assert context.settings.auto_apply is False
        assert ApplicationOrchestrator(context).apply_to_approved() == []


class TestJobToNormalized:
    def test_round_trips_a_stored_job(self, context) -> None:
        job_id = seed_job(context)
        with context.session() as session:
            normalized = job_to_normalized(session.get(Job, job_id))
        assert normalized.title == "Junior PHP Developer"
        assert normalized.city == "Varna"


class TestUnattendedApplicationIsGated:
    """AUTO_APPLY on is not a licence to act on a weak or unverified evaluation."""

    def _approved_job(self, context, **evaluation_fields):
        from jobhunter.db.models import Job
        from jobhunter.db.models import JobEvaluation as EvaluationRow
        from jobhunter.domain.enums import ApplicationMethod, JobState

        with context.session() as session:
            job = Job(
                fingerprint="fp-auto",
                source="jobs.bg",
                source_url="https://www.jobs.bg/job/9001",
                normalized_url="https://www.jobs.bg/job/9001",
                title="Junior PHP Developer",
                title_normalized="junior php developer",
                description="Requirements: PHP and Laravel. " * 30,
                state=JobState.APPROVED,
                application_method=ApplicationMethod.JOBSBG_INTERNAL,
            )
            session.add(job)
            session.flush()
            fields = {
                "decision": "apply",
                "confidence": 0.9,
                "location_fit": "exact_city",
                "is_it_role": True,
                "is_current": True,
                **evaluation_fields,
            }
            session.add(EvaluationRow(job_id=job.id, **fields))
            session.flush()
            return job.id

    def test_a_job_with_no_evaluation_is_never_applied_to_unattended(self, context):
        from jobhunter.applications.orchestrator import ApplicationOrchestrator
        from jobhunter.db.models import Job
        from jobhunter.domain.enums import ApplicationMethod, JobState

        with context.session() as session:
            job = Job(
                fingerprint="fp-none",
                source="jobs.bg",
                source_url="https://www.jobs.bg/job/9002",
                normalized_url="https://www.jobs.bg/job/9002",
                title="Junior PHP Developer",
                title_normalized="junior php developer",
                description="Requirements: PHP. " * 30,
                state=JobState.APPROVED,
                application_method=ApplicationMethod.JOBSBG_INTERNAL,
            )
            session.add(job)
            session.flush()
            job_id = job.id

        orchestrator = ApplicationOrchestrator(context)
        with context.session() as session:
            blockers = orchestrator._auto_apply_blockers(session, session.get(Job, job_id))
        assert "job has no current evaluation" in blockers

    def test_a_degraded_evaluation_blocks_an_unattended_application(self, context):
        from jobhunter.applications.orchestrator import ApplicationOrchestrator
        from jobhunter.db.models import Job

        job_id = self._approved_job(context, degraded=True, degraded_reason="model timed out")
        orchestrator = ApplicationOrchestrator(context)
        with context.session() as session:
            blockers = orchestrator._auto_apply_blockers(session, session.get(Job, job_id))
        assert "evaluation was degraded" in blockers

    def test_a_review_recommendation_blocks_an_unattended_application(self, context):
        from jobhunter.applications.orchestrator import ApplicationOrchestrator
        from jobhunter.db.models import Job

        job_id = self._approved_job(context, decision="review")
        orchestrator = ApplicationOrchestrator(context)
        with context.session() as session:
            blockers = orchestrator._auto_apply_blockers(session, session.get(Job, job_id))
        assert any("not apply" in b for b in blockers)

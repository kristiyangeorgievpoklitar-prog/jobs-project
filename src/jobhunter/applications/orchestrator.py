"""Application orchestration: from an approved job to a recorded outcome."""

from __future__ import annotations

import traceback
from pathlib import Path

from sqlalchemy import desc, select

from jobhunter.applications.cover_letter import generate as generate_cover_letter
from jobhunter.applications.dedupe import has_applied
from jobhunter.applications.state_machine import record_event, transition_job
from jobhunter.browser.challenge import ChallengeDetectedError
from jobhunter.browser.manager import BrowserManager
from jobhunter.context import AppContext
from jobhunter.db.base import utcnow
from jobhunter.db.models import Application, Job
from jobhunter.domain.enums import JobState, Language
from jobhunter.domain.schemas import ApplicationOutcome, NormalizedJob
from jobhunter.logging_setup import get_logger
from jobhunter.pipeline.repository import record_error
from jobhunter.profile.cv import select_cv_for_job
from jobhunter.profile.profile_store import get_active_profile, to_snapshot
from jobhunter.sources.jobsbg.applier import JobsBgApplier

log = get_logger(__name__)

APPLYABLE_STATES = {
    JobState.APPROVED,
    JobState.REVIEW,
    JobState.FAILED,
    JobState.BLOCKED,
    JobState.MATCHED,
}


def job_to_normalized(job: Job) -> NormalizedJob:
    """Rebuild the DTO the AI layer expects from a stored job."""
    from jobhunter.domain.schemas import SalaryInfo

    return NormalizedJob(
        fingerprint=job.fingerprint,
        source=job.source,
        source_job_id=job.source_job_id,
        source_url=job.source_url,
        normalized_url=job.normalized_url,
        title=job.title,
        title_normalized=job.title_normalized,
        company_name=job.company_display,
        description=job.description,
        description_hash=job.description_hash,
        location_raw=job.location_raw,
        city=job.city,
        work_mode=job.work_mode,
        salary=SalaryInfo(
            minimum=job.salary_min,
            maximum=job.salary_max,
            currency=job.salary_currency,
            period=job.salary_period,
            raw=job.salary_raw,
        ),
        employment_type=job.employment_type,
        posted_at=job.posted_at,
        posted_at_raw=job.posted_at_raw,
        tech_keywords=list(job.tech_keywords or []),
        years_experience_required=job.years_experience_required,
        application_method=job.application_method,
        application_url=job.application_url,
        language=job.language,
    )


class ApplicationOrchestrator:
    """Coordinates one application attempt end to end."""

    def __init__(self, context: AppContext) -> None:
        self.context = context
        self.settings = context.settings

    def apply_to_job(
        self, job_id: int, *, submit: bool = False, browser: BrowserManager | None = None
    ) -> ApplicationOutcome:
        """Attempt an application, honouring duplicate and safety checks."""
        owns_browser = browser is None

        with self.context.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                return ApplicationOutcome(success=False, failure_reason=f"Job {job_id} not found")

            duplicate = has_applied(session, job)
            if duplicate.is_duplicate:
                log.info("apply_skipped_duplicate", job_id=job_id, reason=duplicate.reason)
                record_event(
                    session,
                    event="duplicate_skipped",
                    job=job,
                    detail={"reason": duplicate.reason},
                )
                return ApplicationOutcome(
                    success=False,
                    failure_reason=f"Duplicate: {duplicate.reason}",
                )

            state = JobState(str(job.state))
            if state not in APPLYABLE_STATES:
                return ApplicationOutcome(
                    success=False,
                    failure_reason=f"Job is in state {state.value}; not applyable.",
                )

            if submit and not self.settings.auto_apply:
                # --submit on the CLI is an explicit, per-job human decision, so
                # it is allowed; the AUTO_APPLY switch only governs unattended runs.
                log.info("manual_submit_while_auto_apply_disabled", job_id=job_id)

            profile = get_active_profile(session)
            candidate = to_snapshot(profile)
            normalized = job_to_normalized(job)

            cv_record = select_cv_for_job(session, job_language=job.language)
            cv_path = Path(cv_record.path) if cv_record and cv_record.is_available else None

            cover_letter_text: str | None = None
            cover_language = Language.UNKNOWN
            if self.settings.cover_letter_enabled:
                cover_letter_text, cover_language = generate_cover_letter(
                    self.context.provider,
                    normalized,
                    candidate,
                    max_words=self.settings.cover_letter_max_words,
                )

            application = session.scalar(select(Application).where(Application.job_id == job.id))
            if application is None:
                application = Application(job_id=job.id)
                session.add(application)
            application.cv_file_id = cv_record.id if cv_record else None
            application.cover_letter = cover_letter_text
            application.cover_letter_language = cover_language
            application.method = job.application_method
            application.attempt_count = (application.attempt_count or 0) + 1
            session.flush()

            # Running an apply command is itself an approval, so a job sitting in
            # REVIEW is moved through APPROVED rather than jumping the state machine.
            if JobState(str(job.state)) in {JobState.REVIEW, JobState.MATCHED}:
                transition_job(
                    session,
                    job,
                    JobState.APPROVED,
                    event="approved_for_apply",
                    application=application,
                    strict=False,
                )

            transition_job(
                session,
                job,
                JobState.APPLYING,
                event="apply_started",
                application=application,
                strict=False,
                detail={"submit": submit, "cv": cv_record.filename if cv_record else None},
            )

            job_url = job.source_url
            application_id = application.id
            full_name = profile.full_name if profile else None
            email = profile.email if profile else None
            phone = profile.phone if profile else None
            job_title = job.title
            company = job.company_display

        # ---- browser work happens outside the session ----------------------
        manager = browser or BrowserManager(self.settings)
        if owns_browser:
            manager.start()

        try:
            applier = JobsBgApplier(manager)
            outcome = applier.apply(
                job_url,
                submit=submit,
                full_name=full_name,
                email=email,
                phone=phone,
                cover_letter=cover_letter_text,
                cv_path=cv_path,
                screenshots_dir=self.settings.screenshots_dir,
            )
        except ChallengeDetectedError as exc:
            outcome = ApplicationOutcome(
                success=False,
                blocked=True,
                failure_reason=f"Blocked by {exc.result.type.value}: {exc.result.detail}",
            )
            self.context.notifier.challenge_detected(
                challenge_type=exc.result.type.value, url=exc.url
            )
        except Exception as exc:
            log.error("apply_failed", job_id=job_id, error=str(exc))
            outcome = ApplicationOutcome(
                success=False, failure_reason=f"{type(exc).__name__}: {exc}"
            )
            with self.context.session() as session:
                record_error(
                    session,
                    category="application",
                    message=str(exc),
                    job_id=job_id,
                    traceback_text=traceback.format_exc()[:6000],
                )
        finally:
            if owns_browser:
                manager.stop()

        self._record_outcome(job_id, application_id, outcome, job_title, company)
        return outcome

    def _record_outcome(
        self,
        job_id: int,
        application_id: int,
        outcome: ApplicationOutcome,
        job_title: str,
        company: str,
    ) -> None:
        """Persist the result and move the job to its resulting state."""
        with self.context.session() as session:
            job = session.get(Job, job_id)
            application = session.get(Application, application_id)
            if job is None or application is None:
                return

            application.success = outcome.success
            application.confirmation_evidence = outcome.evidence
            application.failure_reason = outcome.failure_reason
            application.screenshot_path = outcome.screenshot_path

            if outcome.success:
                application.submitted_at = utcnow()
                application.confirmed_at = utcnow()
                target = JobState.APPLIED
                event = "applied"
            elif outcome.blocked:
                target = JobState.BLOCKED
                event = "blocked"
            elif outcome.requires_manual_step:
                application.is_manual = True
                target = JobState.BLOCKED
                event = "manual_step_required"
            else:
                target = JobState.FAILED
                event = "apply_failed"

            transition_job(
                session,
                job,
                target,
                event=event,
                application=application,
                strict=False,
                detail={
                    "success": outcome.success,
                    "reason": outcome.failure_reason,
                    "evidence": outcome.evidence,
                },
            )

        if outcome.success:
            self.context.notifier.application_success(
                title=job_title, company=company, job_id=job_id, evidence=outcome.evidence
            )
        elif outcome.blocked or outcome.requires_manual_step:
            self.context.notifier.blocked(
                reason=outcome.failure_reason or "manual step required", job_id=job_id
            )
        else:
            self.context.notifier.application_failed(
                title=job_title,
                company=company,
                job_id=job_id,
                reason=outcome.failure_reason or "unknown",
            )

    # ------------------------------------------------------------ batch ---

    def _auto_apply_blockers(self, session, job: Job) -> list[str]:
        """Everything standing between this job and an unattended application."""
        from jobhunter.db.models import JobEvaluation as EvaluationRow
        from jobhunter.matching.policy import may_auto_apply
        from jobhunter.pipeline.evaluation_store import to_domain

        row = session.scalar(
            select(EvaluationRow).where(
                EvaluationRow.job_id == job.id, EvaluationRow.is_current.is_(True)
            )
        )
        if row is None:
            return ["job has no current evaluation"]

        cv_record = select_cv_for_job(session, job_language=job.language)
        check = may_auto_apply(
            to_domain(row),
            job_to_normalized(job),
            has_valid_cv=cv_record is not None and cv_record.is_available,
            application_route_known=job.application_method.is_automatable,
            already_applied=has_applied(session, job).is_duplicate,
            policy=self.context.decision_policy,
        )
        return check.blockers

    def apply_to_approved(self, *, limit: int | None = None) -> list[ApplicationOutcome]:
        """Submit applications for APPROVED jobs. Only runs when AUTO_APPLY is on."""
        if not self.settings.auto_apply:
            log.info("auto_apply_disabled")
            return []

        cap = limit if limit is not None else self.settings.max_auto_applications_per_run
        with self.context.session() as session:
            candidates = session.scalars(
                select(Job).where(Job.state == JobState.APPROVED).order_by(desc(Job.id))
            ).all()

            # Being APPROVED is not sufficient to act unattended. Every condition
            # is re-checked here against the current evaluation, because the
            # state was set at scan time and the world may have moved since.
            job_ids: list[int] = []
            for job in candidates:
                if len(job_ids) >= cap:
                    break
                blockers = self._auto_apply_blockers(session, job)
                if blockers:
                    log.info("auto_apply_blocked", job_id=job.id, blockers=blockers)
                    record_event(
                        session,
                        event="auto_apply_blocked",
                        job=job,
                        detail={"blockers": blockers},
                    )
                    continue
                job_ids.append(job.id)

        if not job_ids:
            return []

        outcomes: list[ApplicationOutcome] = []
        with BrowserManager(self.settings) as browser:
            for job_id in job_ids:
                outcomes.append(self.apply_to_job(job_id, submit=True, browser=browser))
        return outcomes

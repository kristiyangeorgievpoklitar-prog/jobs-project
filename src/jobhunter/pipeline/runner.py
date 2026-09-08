"""The scan pipeline: discover -> normalize -> classify -> score -> decide.

Each job is processed independently; a failure on one is recorded and the run
continues. A detected anti-bot challenge stops the run cleanly and marks it
BLOCKED rather than retrying against the site.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass

from sqlalchemy import select

from jobhunter.applications.state_machine import transition_job
from jobhunter.browser.challenge import ChallengeDetectedError
from jobhunter.browser.manager import BrowserManager
from jobhunter.context import AppContext
from jobhunter.db.base import utcnow
from jobhunter.db.models import AutomationRun, Job
from jobhunter.domain.enums import JobState, RunStatus, RunTrigger
from jobhunter.domain.evaluation import Decision
from jobhunter.domain.schemas import CandidateSnapshot, RawJob, ScanStats
from jobhunter.logging_setup import get_logger
from jobhunter.matching.evaluator import EvaluationStats
from jobhunter.normalize.normalizer import normalize_job
from jobhunter.pipeline.repository import (
    apply_classification,
    record_error,
    upsert_job,
)
from jobhunter.profile.profile_store import get_active_profile, to_snapshot
from jobhunter.sources.jobsbg.discovery import JobsBgDiscovery, merge_detail

log = get_logger(__name__)


@dataclass
class ScanOptions:
    location: str | None = None
    keywords: str | None = None
    entry_level_only: bool = False
    max_results: int | None = None
    enrich_limit: int | None = None
    trigger: RunTrigger = RunTrigger.MANUAL
    dry_run: bool = False


class ScanPipeline:
    """Runs one end-to-end discovery + evaluation pass."""

    def __init__(self, context: AppContext) -> None:
        self.context = context
        self.settings = context.settings

    # ------------------------------------------------------------- helpers

    def _candidate(self) -> CandidateSnapshot:
        with self.context.session() as session:
            return to_snapshot(get_active_profile(session))

    def _start_run(self, options: ScanOptions) -> int:
        with self.context.session() as session:
            run = AutomationRun(trigger=options.trigger, status=RunStatus.RUNNING)
            session.add(run)
            session.flush()
            return run.id

    def _finish_run(
        self, run_id: int, stats: ScanStats, status: RunStatus, notes: str | None = None
    ) -> None:
        with self.context.session() as session:
            run = session.get(AutomationRun, run_id)
            if run is None:
                return
            run.status = status
            run.finished_at = utcnow()
            run.jobs_seen = stats.jobs_seen
            run.jobs_new = stats.jobs_new
            run.jobs_updated = stats.jobs_updated
            run.jobs_classified = stats.jobs_classified
            run.jobs_matched = stats.jobs_matched
            run.applications_submitted = stats.applications_submitted
            run.errors_count = stats.errors_count
            run.notes = notes or ("\n".join(stats.notes) if stats.notes else None)
            run.stats = stats.model_dump()

    # ------------------------------------------------------- pre-filtering

    def _prioritise_for_enrichment(
        self, cards: list[RawJob], candidate: CandidateSnapshot
    ) -> list[RawJob]:
        """Drop what the gate rejects; keep the rest in the order the site gave.

        Every detail page is an extra request, so the enrichment budget goes to
        listings that could still turn out to be worth applying to. It is
        deliberately *not* ordered by a provisional match score: the card alone
        has no requirements text, so any such ordering ranks listings by how
        little is known about them, which is how the previous version spent its
        budget on the jobs it understood least.
        """
        rejected = self._rejected_fingerprints()
        keep: list[RawJob] = []
        for raw in cards:
            normalized = normalize_job(raw)
            verdict = self.context.evaluator.gate.check(
                normalized, candidate, rejected_fingerprints=rejected
            )
            if verdict.passed:
                keep.append(raw)
        return keep

    def _rejected_fingerprints(self) -> set[str]:
        """Listings the candidate has already turned down."""
        from jobhunter.db.models import UserFeedback

        with self.context.session() as session:
            rows = (
                session.query(Job.fingerprint)
                .join(UserFeedback, UserFeedback.job_id == Job.id)
                .filter(UserFeedback.action == "skip")
                .all()
            )
        return {row[0] for row in rows}

    def _cv_text(self) -> str | None:
        """The CV the local model reads, when sending it locally is enabled."""
        if not self.settings.send_cv_text_to_local_model:
            return None
        from jobhunter.db.models import CVFile

        with self.context.session() as session:
            cv = (
                session.query(CVFile)
                .filter(CVFile.is_available.is_(True), CVFile.extracted_text.isnot(None))
                .order_by(CVFile.is_default.desc(), CVFile.id)
                .first()
            )
            return cv.extracted_text if cv else None

    # ------------------------------------------------------------- the run

    def run(self, options: ScanOptions | None = None) -> ScanStats:
        options = options or ScanOptions()
        stats = ScanStats()
        run_id = self._start_run(options)
        candidate = self._candidate()

        location = options.location or self.settings.search_location
        max_results = options.max_results or self.settings.max_jobs_per_scan
        enrich_limit = (
            options.enrich_limit
            if options.enrich_limit is not None
            else self.settings.max_jobs_per_scan
        )

        log.info(
            "scan_start",
            run_id=run_id,
            location=location,
            max_results=max_results,
            provider=self.context.provider.name,
        )

        status = RunStatus.SUCCEEDED
        notes: str | None = None
        evaluation_stats = EvaluationStats()

        try:
            with BrowserManager(self.settings) as browser:
                discovery = JobsBgDiscovery(browser, self.settings)

                cards = discovery.search(
                    location=location,
                    keywords=options.keywords
                    or (
                        ",".join(self.settings.search_keywords)
                        if self.settings.search_keywords
                        else None
                    ),
                    entry_level_only=options.entry_level_only,
                    max_results=max_results,
                )
                stats.jobs_seen = len(cards)

                shortlist = self._prioritise_for_enrichment(cards, candidate)
                log.info("scan_shortlisted", total=len(cards), shortlisted=len(shortlist))

                details = discovery.enrich(shortlist, limit=min(enrich_limit, len(shortlist)))

                # Gathered once per run rather than per job: both are the same
                # for every listing, and the CV read in particular is not free.
                cv_text = self._cv_text()
                rejected = self._rejected_fingerprints()

                for card in cards:
                    merged = merge_detail(card, details.get(card.source_url))
                    try:
                        self._process_job(
                            merged,
                            candidate,
                            stats,
                            run_id,
                            cv_text=cv_text,
                            rejected=rejected,
                            evaluation_stats=evaluation_stats,
                        )
                    except Exception as exc:
                        stats.errors_count += 1
                        log.warning("job_processing_failed", url=card.source_url, error=str(exc))
                        with self.context.session() as session:
                            record_error(
                                session,
                                category="job_processing",
                                message=str(exc),
                                run_id=run_id,
                                detail={"url": card.source_url},
                                traceback_text=traceback.format_exc()[:6000],
                            )

        except ChallengeDetectedError as exc:
            stats.blocked = True
            status = RunStatus.BLOCKED
            notes = f"Blocked by {exc.result.type.value}: {exc.result.detail}"
            stats.notes.append(notes)
            log.warning("scan_blocked", type=exc.result.type.value, url=exc.url)
            self.context.notifier.challenge_detected(
                challenge_type=exc.result.type.value, url=exc.url
            )
            with self.context.session() as session:
                record_error(
                    session,
                    category="challenge",
                    message=notes,
                    run_id=run_id,
                    detail={"url": exc.url, "type": exc.result.type.value},
                )

        except Exception as exc:
            status = RunStatus.FAILED
            notes = f"{type(exc).__name__}: {exc}"
            stats.errors_count += 1
            stats.notes.append(notes)
            log.error("scan_failed", error=notes)
            self.context.notifier.scan_failed(error=notes)
            with self.context.session() as session:
                record_error(
                    session,
                    category="scan",
                    message=notes,
                    run_id=run_id,
                    traceback_text=traceback.format_exc()[:6000],
                )

        self._finish_run(run_id, stats, status, notes)

        if status is RunStatus.SUCCEEDED:
            self.context.notifier.scan_completed(
                stats={
                    "jobs_seen": stats.jobs_seen,
                    "jobs_new": stats.jobs_new,
                    "jobs_matched": stats.jobs_matched,
                    "pending_review": self._pending_review_count(),
                }
            )

        log.info(
            "scan_done",
            run_id=run_id,
            status=status.value,
            gated=evaluation_stats.gated,
            cached=evaluation_stats.cached,
            model_calls=evaluation_stats.evaluated,
            model_calls_avoided=evaluation_stats.model_calls_avoided,
            degraded=evaluation_stats.degraded,
            downgraded=evaluation_stats.downgraded,
            **stats.model_dump(exclude={"notes"}),
        )
        return stats

    def _pending_review_count(self) -> int:
        with self.context.session() as session:
            return len(session.scalars(select(Job).where(Job.state == JobState.REVIEW)).all())

    # --------------------------------------------------------- single job

    def _process_job(
        self,
        raw: RawJob,
        candidate: CandidateSnapshot,
        stats: ScanStats,
        run_id: int,
        *,
        cv_text: str | None = None,
        rejected: set[str] | None = None,
        evaluation_stats: EvaluationStats | None = None,
    ) -> None:
        normalized = normalize_job(raw)

        with self.context.session() as session:
            job, is_new = upsert_job(session, normalized)
            if is_new:
                stats.jobs_new += 1
            else:
                stats.jobs_updated += 1

            # An already-applied job is never re-evaluated or re-applied.
            if job.state == JobState.APPLIED:
                log.info("job_skipped_already_applied", job_id=job.id)
                return

            # The deterministic classifier still runs: its structured fields
            # (seniority tag, language, extracted requirement bullets) are useful
            # on the job page and cost nothing. It no longer decides anything.
            classification = self.context.engine.classify(normalized, candidate)
            apply_classification(job, classification)
            stats.jobs_classified += 1

            transition_job(
                session,
                job,
                JobState.CLASSIFIED,
                event="classified",
                strict=False,
                detail={"seniority": classification.seniority.value, "is_it": classification.is_it},
            )

            evaluation = self.context.evaluator.evaluate(
                session,
                job.id,
                normalized,
                candidate,
                cv_text=cv_text,
                rejected_fingerprints=rejected,
                stats=evaluation_stats,
            )
            stats.jobs_matched += 1

            transition_job(
                session,
                job,
                JobState.MATCHED,
                event="matched",
                strict=False,
                detail={
                    "decision": evaluation.decision.value,
                    "confidence": evaluation.confidence,
                    "source": evaluation.source,
                },
            )

            target_state = {
                Decision.APPLY: JobState.APPROVED,
                Decision.REVIEW: JobState.REVIEW,
                Decision.SKIP: JobState.SKIPPED,
            }[evaluation.decision]

            # In REVIEW mode nothing is auto-approved; a human decides.
            if target_state is JobState.APPROVED and not self.settings.auto_apply:
                target_state = JobState.REVIEW

            transition_job(
                session,
                job,
                target_state,
                event="decision",
                strict=False,
                detail={
                    "decision": evaluation.decision.value,
                    "confidence": evaluation.confidence,
                    "auto_apply": self.settings.auto_apply,
                },
            )

            should_notify = is_new and evaluation.decision in (Decision.APPLY, Decision.REVIEW)
            job_id, title, company, city, score, rec = (
                job.id,
                job.title,
                job.company_display,
                job.city or job.location_raw or "n/a",
                int(evaluation.confidence * 100),
                evaluation.decision.value,
            )

        if should_notify:
            self.context.notifier.high_match_job(
                title=title,
                company=company,
                location=city,
                score=score,
                recommendation=rec,
                job_id=job_id,
            )

"""POST actions: scans, job decisions and application attempts.

Long-running work (a scan, an application) runs in a background thread so the
request returns immediately; progress is visible in the run list and logs.
"""

from __future__ import annotations

import threading

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import update

from jobhunter.applications.state_machine import transition_job
from jobhunter.db.base import utcnow
from jobhunter.db.models import Job, Notification
from jobhunter.domain.enums import JobState, RunTrigger
from jobhunter.logging_setup import get_logger
from jobhunter.web import deps
from jobhunter.web.routes._common import redirect

log = get_logger(__name__)
router = APIRouter(prefix="/actions")

_running = threading.Lock()


def _run_background(target, *args, **kwargs) -> bool:
    """Start work in a thread unless a job is already running."""
    if not _running.acquire(blocking=False):
        return False

    def wrapper() -> None:
        try:
            target(*args, **kwargs)
        except Exception as exc:
            log.error("background_action_failed", error=str(exc))
        finally:
            _running.release()

    threading.Thread(target=wrapper, daemon=True).start()
    return True


@router.post("/scan")
async def trigger_scan(request: Request) -> RedirectResponse:
    from jobhunter.pipeline.runner import ScanOptions, ScanPipeline

    context = deps.get_context()

    def work() -> None:
        ScanPipeline(context).run(ScanOptions(trigger=RunTrigger.MANUAL))

    if not _run_background(work):
        return redirect("/", "A scan is already running.", "warning")
    return redirect(
        "/",
        "Scan started. A browser window will open; results appear here as they are processed.",
        "info",
    )


@router.post("/today-scan")
async def trigger_today_scan(request: Request) -> RedirectResponse:
    from jobhunter.today import run_today_scan

    context = deps.get_context()

    def work() -> None:
        run_today_scan(context)

    if not _run_background(work):
        return redirect("/today", "A scan is already running.", "warning")
    return redirect(
        "/today",
        "Scanning today's listings. A browser window will open; results appear here as they land.",
        "info",
    )


@router.post("/jobs/{job_id}/approve")
async def approve_job(request: Request, job_id: int) -> RedirectResponse:
    context = deps.get_context()
    with context.session() as session:
        job = session.get(Job, job_id)
        if job is None:
            return redirect("/jobs", "Job not found.", "error")
        ok = transition_job(session, job, JobState.APPROVED, event="approved_by_user", strict=False)
    message = "Job approved." if ok else "Cannot approve a job in this state."
    return redirect(f"/jobs/{job_id}", message, "success" if ok else "warning")


@router.post("/jobs/{job_id}/skip")
async def skip_job(request: Request, job_id: int) -> RedirectResponse:
    context = deps.get_context()
    with context.session() as session:
        job = session.get(Job, job_id)
        if job is None:
            return redirect("/jobs", "Job not found.", "error")
        ok = transition_job(session, job, JobState.SKIPPED, event="skipped_by_user", strict=False)
    return redirect(f"/jobs/{job_id}", "Job skipped." if ok else "Cannot skip this job.", "info")


@router.post("/jobs/{job_id}/rescore")
async def rescore_job(request: Request, job_id: int) -> RedirectResponse:
    from jobhunter.applications.orchestrator import job_to_normalized
    from jobhunter.pipeline.repository import apply_classification, record_match
    from jobhunter.profile.profile_store import get_active_profile, to_snapshot

    context = deps.get_context()
    with context.session() as session:
        job = session.get(Job, job_id)
        if job is None:
            return redirect("/jobs", "Job not found.", "error")
        candidate = to_snapshot(get_active_profile(session))
        normalized = job_to_normalized(job)
        classification, match = context.engine.evaluate(normalized, candidate)
        apply_classification(job, classification)
        record_match(session, job, match, profile_version=candidate.version)
        score = match.score
    return redirect(f"/jobs/{job_id}", f"Re-scored: {score}/100.", "success")


def _apply(job_id: int, submit: bool) -> RedirectResponse:
    from jobhunter.applications.orchestrator import ApplicationOrchestrator

    context = deps.get_context()

    def work() -> None:
        ApplicationOrchestrator(context).apply_to_job(job_id, submit=submit)

    if not _run_background(work):
        return redirect(f"/jobs/{job_id}", "Another task is already running.", "warning")

    verb = "Submitting" if submit else "Preparing"
    return redirect(
        f"/jobs/{job_id}",
        f"{verb} the application. A browser window will open; the result is recorded here.",
        "info",
    )


@router.post("/jobs/{job_id}/prepare")
async def prepare_application(request: Request, job_id: int) -> RedirectResponse:
    return _apply(job_id, submit=False)


@router.post("/jobs/{job_id}/submit")
async def submit_application(request: Request, job_id: int) -> RedirectResponse:
    context = deps.get_context()
    if not context.settings.auto_apply:
        return redirect(
            f"/jobs/{job_id}",
            "Submitting is disabled. Enable auto-apply in Settings first.",
            "warning",
        )
    return _apply(job_id, submit=True)


@router.post("/notifications/read-all")
async def mark_all_read(request: Request) -> RedirectResponse:
    context = deps.get_context()
    with context.session() as session:
        session.execute(
            update(Notification).where(Notification.read_at.is_(None)).values(read_at=utcnow())
        )
    return redirect("/notifications", "All notifications marked read.", "success")

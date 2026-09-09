"""Scan scheduling.

Defaults to one run per day. Jobs.bg is never polled aggressively: the interval
is measured in hours and the browser layer additionally paces every request.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from jobhunter.context import AppContext
from jobhunter.domain.enums import RunTrigger
from jobhunter.logging_setup import get_logger
from jobhunter.pipeline.runner import ScanOptions, ScanPipeline

log = get_logger(__name__)

SCAN_JOB_ID = "scheduled_scan"
TODAY_SCAN_JOB_ID = "scheduled_today_scan"

# What the scheduled task is. Both run on the same scheduler with the same
# guards; they differ only in how much of the site they read.
ScheduleMode = Literal["full", "today"]

# One scan at a time, and a late trigger is dropped rather than queued.
JOB_DEFAULTS = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600}


def build_trigger(interval_hours: float, at_hour: int) -> CronTrigger | IntervalTrigger:
    """Daily runs use a fixed hour; sub-daily runs use a plain interval."""
    if interval_hours >= 24:
        days = max(1, round(interval_hours / 24))
        return CronTrigger(hour=at_hour, minute=0, day=f"*/{days}" if days > 1 else "*")
    return IntervalTrigger(hours=interval_hours)


def run_scan(context: AppContext) -> None:
    """The scheduled task body."""
    log.info("scheduled_scan_starting")
    try:
        pipeline = ScanPipeline(context)
        stats = pipeline.run(ScanOptions(trigger=RunTrigger.SCHEDULED))
        log.info("scheduled_scan_finished", **stats.model_dump(exclude={"notes"}))

        if context.settings.auto_apply:
            from jobhunter.applications.orchestrator import ApplicationOrchestrator

            outcomes = ApplicationOrchestrator(context).apply_to_approved()
            log.info(
                "scheduled_applications_finished",
                attempted=len(outcomes),
                submitted=sum(1 for o in outcomes if o.success),
            )
    except Exception as exc:
        log.error("scheduled_scan_failed", error=str(exc))
        context.notifier.scan_failed(error=str(exc))


def run_today_scan(context: AppContext) -> None:
    """The scheduled daily task body: today's listings only.

    Deliberately does not apply to anything, even with auto-apply enabled. A
    scan that reads is safe to leave running unattended; one that submits on a
    timer is not.
    """
    from jobhunter.today import run_today_scan as scan_today

    log.info("scheduled_today_scan_starting")
    try:
        result = scan_today(context)
        log.info(
            "scheduled_today_scan_finished",
            published_today=len(result.outcomes),
            new=len(result.new),
            notified=result.notified,
        )
    except Exception as exc:
        log.error("scheduled_today_scan_failed", error=str(exc))
        context.notifier.scan_failed(error=str(exc))


def create_scheduler(context: AppContext, *, blocking: bool = True, mode: ScheduleMode = "full"):
    """Build a scheduler with the chosen scan job registered."""
    settings = context.settings
    scheduler = (
        BlockingScheduler(job_defaults=JOB_DEFAULTS)
        if blocking
        else BackgroundScheduler(job_defaults=JOB_DEFAULTS)
    )
    if mode == "today":
        # Once a day at the configured hour: a "published today" scan run more
        # often than that would re-read the same day for nothing.
        scheduler.add_job(
            run_today_scan,
            trigger=build_trigger(24, settings.scan_at_hour),
            args=[context],
            id=TODAY_SCAN_JOB_ID,
            name="Jobs.bg today-scan",
            replace_existing=True,
        )
        return scheduler

    scheduler.add_job(
        run_scan,
        trigger=build_trigger(settings.scan_interval_hours, settings.scan_at_hour),
        args=[context],
        id=SCAN_JOB_ID,
        name="Jobs.bg scan",
        replace_existing=True,
    )
    return scheduler


def next_run_time(context: AppContext) -> datetime | None:
    """Compute the next fire time without starting a scheduler."""
    trigger = build_trigger(context.settings.scan_interval_hours, context.settings.scan_at_hour)
    from datetime import UTC

    from apscheduler.util import astimezone

    return trigger.get_next_fire_time(None, datetime.now(astimezone(None) or UTC))


def run_forever(context: AppContext, *, mode: ScheduleMode = "full") -> None:
    """Run the scheduler in the foreground until interrupted."""
    settings = context.settings
    scheduler = create_scheduler(context, blocking=True, mode=mode)
    log.info(
        "scheduler_starting",
        mode=mode,
        interval_hours=settings.scan_interval_hours,
        at_hour=settings.scan_at_hour,
        auto_apply=settings.auto_apply,
    )
    if mode == "today":
        print(
            f"Scheduler running: today-scan daily at {settings.scan_at_hour:02d}:00. "
            "Ctrl-C to stop."
        )
    else:
        print(
            f"Scheduler running: every {settings.scan_interval_hours}h "
            f"(daily at {settings.scan_at_hour:02d}:00). Ctrl-C to stop."
        )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler_stopped")
        scheduler.shutdown(wait=False)

"""Today's Jobs: the daily "what appeared on Jobs.bg today" pass.

This is the workflow the candidate was doing by hand — open Jobs.bg, filter to
IT in Varna, look at what was published today — expressed in the pieces the
system already has. Nothing here decides anything: discovery, the gate, the
evaluation cache, the local model and the policy all do exactly what they do in
a full scan. What this module adds is the *bound*: only listings carrying
today's publication date are looked at, and only listings the database has never
seen before are announced.

That bound is the whole point. A full scan reads up to five pages of history and
can spend hours of local inference on listings that were already judged weeks
ago; a real day brings four to nine new ones.

Two questions are answered here and they are not the same:

* **published today** — the site printed today's date on the card. This is what
  makes a listing part of today's set.
* **newly discovered** — this database had never recorded it before. This is
  what makes it worth a notification. A listing published today and seen in an
  earlier scan today is still shown, marked as already seen, and never
  announced twice.

Note that the two readers of "new" ask slightly different questions, and the
difference is deliberate. :class:`TodayScanResult` asks whether *this run*
inserted the row, because a digest must not repeat what an earlier run already
announced. :class:`TodayJobs`, which backs the page, asks whether the listing
was first seen at any point today — a page that outlives the run would
otherwise badge everything "already seen" from the second scan onward.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from jobhunter.db.models import Job
from jobhunter.db.models import JobEvaluation as JobEvaluationRow
from jobhunter.domain.enums import RunStatus
from jobhunter.domain.evaluation import Decision, JobEvaluation, RequirementAssessment
from jobhunter.domain.schemas import ScanStats
from jobhunter.logging_setup import get_logger
from jobhunter.pipeline.evaluation_store import to_domain

if TYPE_CHECKING:  # pragma: no cover - import kept out of the dashboard's path
    from jobhunter.context import AppContext
    from jobhunter.pipeline.runner import JobOutcome

log = get_logger(__name__)

# How many listings the digest names before it stops being a summary.
TOP_MATCHES_IN_DIGEST = 3


def local_today() -> date:
    """The calendar day as Jobs.bg prints it.

    The site is Bulgarian and stamps cards in local time, so the machine's local
    date is the one that agrees with what the candidate sees on the page. Using
    the UTC date instead would call a listing "yesterday's" for the two hours
    after local midnight.
    """
    return datetime.now().date()


def posted_on_bounds(day: date) -> tuple[datetime, datetime]:
    """The half-open range matching a card's printed date.

    Card dates carry no time, so :func:`~jobhunter.sources.jobsbg.parser.parse_posted_date`
    stores them as midnight UTC. These bounds follow that convention rather than
    the clock: they select a printed date, not an interval of real time.
    """
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return start, start + timedelta(days=1)


def discovered_on_bounds(day: date) -> tuple[datetime, datetime]:
    """The half-open range of real time covered by one local calendar day.

    ``first_seen_at`` is a real timestamp, so "discovered today" is the local
    day converted to UTC — not midnight UTC, which in Bulgaria falls in the
    middle of the previous evening.
    """
    start = datetime(day.year, day.month, day.day).astimezone(UTC)
    return start, start + timedelta(days=1)


def _naive_utc(value: datetime) -> datetime:
    """Comparable form of a stored timestamp.

    SQLite drops the timezone and returns a naive value that is nonetheless UTC,
    so both sides of a Python-side comparison have to be put in the same shape.
    """
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


# APPLY first, then REVIEW, then SKIP: the order the candidate reads in.
_DECISION_ORDER = {Decision.APPLY: 0, Decision.REVIEW: 1, Decision.SKIP: 2}


@dataclass
class TodayJob:
    """One listing published today, with what the pipeline made of it."""

    job: Job
    evaluation: JobEvaluation
    is_new: bool
    # Technologies the evaluation credits the candidate with that their profile
    # does not list. Filled by the presentation layer; shown, never removed.
    unverified_claims: list[str] = field(default_factory=list)

    @property
    def decision(self) -> Decision:
        return self.evaluation.decision

    @property
    def mandatory_requirements(self) -> list[RequirementAssessment]:
        return self.evaluation.mandatory_requirements

    @property
    def warnings(self) -> list[str]:
        """What the candidate needs to see before acting on this one.

        The evaluation's own risks, plus the two things the system knows about
        its own limits: a verdict reached without reading the posting, and a
        listing the model could not evaluate at all.
        """
        notes: list[str] = []
        if self.evaluation.degraded:
            notes.append(
                self.evaluation.degraded_reason
                or "Could not be evaluated automatically - read it yourself."
            )
        notes.extend(self.evaluation.major_risks)
        return notes


@dataclass
class TodayJobs:
    """Everything published on one day, ordered for reading."""

    day: date
    jobs: list[TodayJob] = field(default_factory=list)

    def with_decision(self, decision: Decision) -> list[TodayJob]:
        return [item for item in self.jobs if item.decision is decision]

    @property
    def total(self) -> int:
        return len(self.jobs)

    @property
    def new_jobs(self) -> list[TodayJob]:
        return [item for item in self.jobs if item.is_new]

    @property
    def new_count(self) -> int:
        return len(self.new_jobs)

    @property
    def seen_count(self) -> int:
        return self.total - self.new_count

    @property
    def apply_count(self) -> int:
        return len(self.with_decision(Decision.APPLY))

    @property
    def review_count(self) -> int:
        return len(self.with_decision(Decision.REVIEW))

    @property
    def skip_count(self) -> int:
        return len(self.with_decision(Decision.SKIP))


def build_today_jobs(session: Session, day: date | None = None) -> TodayJobs:
    """Read back everything published on ``day`` and its current verdict.

    Assembled from stored evaluations rather than recomputed, exactly as the
    briefing is, so opening the page costs nothing and always agrees with what
    the last scan decided.
    """
    day = day or local_today()
    posted_start, posted_end = posted_on_bounds(day)
    discovered_start, _ = discovered_on_bounds(day)
    discovered_start_naive = _naive_utc(discovered_start)

    stmt = (
        select(Job, JobEvaluationRow)
        .join(JobEvaluationRow, JobEvaluationRow.job_id == Job.id)
        .where(
            JobEvaluationRow.is_current.is_(True),
            Job.is_archived.is_(False),
            Job.posted_at >= posted_start,
            Job.posted_at < posted_end,
        )
        # The company is eagerly loaded because the dashboard renders it after
        # the session has closed; a lazy load there raises DetachedInstanceError.
        .options(joinedload(Job.company))
    )

    items = [
        TodayJob(
            job=job,
            evaluation=to_domain(row),
            is_new=_naive_utc(job.first_seen_at) >= discovered_start_naive,
        )
        for job, row in session.execute(stmt).all()
    ]
    items.sort(
        key=lambda item: (
            _DECISION_ORDER.get(item.decision, 3),
            -item.evaluation.confidence,
            item.job.id,
        )
    )
    return TodayJobs(day=day, jobs=items)


@dataclass
class TodayScanResult:
    """What one ``today-scan`` did, in the terms the caller reports."""

    day: date
    location: str
    stats: ScanStats
    status: RunStatus = RunStatus.SUCCEEDED
    outcomes: list[JobOutcome] = field(default_factory=list)
    notified: bool = False

    @property
    def blocked(self) -> bool:
        return self.status is RunStatus.BLOCKED

    @property
    def completed(self) -> bool:
        """Whether the counts below mean anything.

        A blocked or failed run may have read nothing at all, and reporting
        "0 new jobs today" for it would be a lie the candidate acts on.
        """
        return self.status is RunStatus.SUCCEEDED

    @property
    def new(self) -> list[JobOutcome]:
        return [outcome for outcome in self.outcomes if outcome.is_new]

    @property
    def already_seen(self) -> list[JobOutcome]:
        return [outcome for outcome in self.outcomes if not outcome.is_new]

    def with_decision(self, decision: Decision) -> list[JobOutcome]:
        return [outcome for outcome in self.outcomes if outcome.decision is decision]

    @property
    def apply_count(self) -> int:
        return len(self.with_decision(Decision.APPLY))

    @property
    def review_count(self) -> int:
        return len(self.with_decision(Decision.REVIEW))

    @property
    def skip_count(self) -> int:
        return len(self.with_decision(Decision.SKIP))


def top_matches(
    outcomes: list[JobOutcome], limit: int = TOP_MATCHES_IN_DIGEST
) -> list[tuple[str, str]]:
    """The listings a one-glance summary should name, best first."""
    ranked = sorted(
        (o for o in outcomes if o.decision is not Decision.SKIP),
        key=lambda o: (_DECISION_ORDER.get(o.decision, 3), -o.evaluation.confidence),
    )
    return [(o.title, o.company) for o in ranked[:limit]]


def render_today_scan(result: TodayScanResult) -> str:
    """The plain-text header the command and the digest lead with."""
    lines = [f"Today's Jobs - {result.day.isoformat()}, IT in {result.location}", ""]

    if not result.completed:
        lines.append(
            "The scan was blocked by the site."
            if result.blocked
            else "The scan did not finish, so nothing can be concluded about today."
        )
        lines.extend(result.stats.notes)
        return "\n".join(lines)

    if not result.outcomes:
        # A quiet day is a real answer, and the one most days give.
        lines.append(
            "Nothing has been published today yet."
            if result.day == local_today()
            else f"Nothing was published on {result.day.isoformat()}."
        )
        return "\n".join(lines)

    total = len(result.outcomes)
    when = "today" if result.day == local_today() else f"on {result.day.isoformat()}"
    lines.append(
        f"{total} listing{'' if total == 1 else 's'} published {when}: "
        f"{len(result.new)} new, {len(result.already_seen)} already seen"
    )
    lines.append("")
    lines.append(f"  {result.apply_count} worth applying")
    lines.append(f"  {result.review_count} worth reviewing")
    lines.append(f"  {result.skip_count} filtered out")
    return "\n".join(lines)


def run_today_scan(
    context: AppContext,
    *,
    day: date | None = None,
    location: str | None = None,
    limit: int | None = None,
    notify: bool = True,
) -> TodayScanResult:
    """Discover today's listings, evaluate the unseen ones, and summarise.

    Never submits an application and never solves a challenge: it discovers,
    evaluates and reports. Deciding to apply stays a human action, taken later
    through ``jobhunter apply``.
    """
    from jobhunter.domain.enums import RunTrigger
    from jobhunter.pipeline.runner import ScanOptions, ScanPipeline

    day = day or local_today()
    location = location or context.settings.search_location

    pipeline = ScanPipeline(context)
    stats = pipeline.run(
        ScanOptions(
            location=location,
            posted_on=day,
            max_results=limit or context.settings.max_jobs_per_scan,
            trigger=RunTrigger.MANUAL,
            # One digest for the day, sent below, instead of one buzz per job.
            suppress_notifications=True,
        )
    )

    result = TodayScanResult(
        day=day,
        location=location,
        stats=stats,
        status=pipeline.status,
        outcomes=pipeline.outcomes,
    )

    # Silence is the correct output for a day with nothing new. Announcing "0
    # new jobs" every morning is how a notification stops being read, and
    # re-running the scan an hour later must not repeat the morning's message.
    if notify and result.new:
        context.notifier.today_digest(
            new_count=len(result.new),
            apply_count=len([o for o in result.new if o.decision is Decision.APPLY]),
            review_count=len([o for o in result.new if o.decision is Decision.REVIEW]),
            skip_count=len([o for o in result.new if o.decision is Decision.SKIP]),
            top_matches=top_matches(result.new),
            location=location,
        )
        result.notified = True

    log.info(
        "today_scan_done",
        day=day.isoformat(),
        location=location,
        published_today=len(result.outcomes),
        new=len(result.new),
        already_seen=len(result.already_seen),
        apply=result.apply_count,
        review=result.review_count,
        skip=result.skip_count,
        notified=result.notified,
    )
    return result

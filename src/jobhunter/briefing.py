"""The daily answer to "what should I apply to today?".

Assembled from the current evaluations rather than recomputed, so opening the
dashboard or running the command costs nothing and always agrees with what the
last scan decided.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select

from jobhunter.db.base import utcnow
from jobhunter.db.models import Job
from jobhunter.db.models import JobEvaluation as JobEvaluationRow
from jobhunter.domain.evaluation import Decision, _first_sentence
from jobhunter.domain.schemas import CandidateSnapshot
from jobhunter.matching.verification import unverified_technologies, verification_warning
from jobhunter.personalization.learner import load_preferences
from jobhunter.personalization.ranker import RankedJob, explain_preferences, rank_jobs
from jobhunter.pipeline.evaluation_store import to_domain
from jobhunter.profile.profile_store import get_active_profile, to_snapshot


@dataclass
class Briefing:
    """Everything the morning summary needs, already ordered."""

    apply: list[RankedJob] = field(default_factory=list)
    review: list[RankedJob] = field(default_factory=list)
    skipped_count: int = 0
    new_since: datetime | None = None
    degraded_count: int = 0
    # Evaluations made against an older version of the profile or CV. They are
    # still shown — an outdated verdict beats an empty dashboard — but the fact
    # that they are outdated has to be visible, not silent.
    stale_count: int = 0

    @property
    def total_worth_attention(self) -> int:
        return len(self.apply) + len(self.review)

    @property
    def top_pick(self) -> RankedJob | None:
        return self.apply[0] if self.apply else (self.review[0] if self.review else None)


def build_briefing(
    session,
    *,
    limit: int = 12,
    since_hours: int | None = None,
    candidate_fingerprint: str | None = None,
    candidate: CandidateSnapshot | None = None,
) -> Briefing:
    """Collect the current decisions, ranked by the candidate's own history.

    ``candidate_fingerprint`` is the hash of the profile as it stands now; any
    evaluation carrying a different one was made about an older version of the
    candidate and is counted as stale.
    """
    stmt = (
        select(Job, JobEvaluationRow)
        .join(JobEvaluationRow, JobEvaluationRow.job_id == Job.id)
        .where(JobEvaluationRow.is_current.is_(True), Job.is_archived.is_(False))
    )
    if since_hours is not None:
        stmt = stmt.where(Job.first_seen_at >= utcnow() - timedelta(hours=since_hours))

    rows = session.execute(stmt).all()
    preferences = load_preferences(session)
    candidate = candidate or to_snapshot(get_active_profile(session))

    stale = (
        sum(
            1
            for _, row in rows
            if row.candidate_fingerprint and row.candidate_fingerprint != candidate_fingerprint
        )
        if candidate_fingerprint
        else 0
    )

    pairs = [(job, to_domain(row)) for job, row in rows]
    ranked = rank_jobs(pairs, preferences)

    briefing = Briefing(
        skipped_count=sum(1 for r in ranked if r.evaluation.decision is Decision.SKIP),
        degraded_count=sum(1 for r in ranked if r.evaluation.degraded),
        new_since=utcnow() - timedelta(hours=since_hours) if since_hours else None,
        stale_count=stale,
    )
    briefing.apply = [r for r in ranked if r.evaluation.decision is Decision.APPLY][:limit]
    briefing.review = [r for r in ranked if r.evaluation.decision is Decision.REVIEW][:limit]

    # Only for what is actually shown: the check is cheap but pointless on the
    # listings nobody will read.
    for shown in (*briefing.apply, *briefing.review):
        shown.unverified_claims = unverified_technologies(shown.evaluation, candidate)

    return briefing


def render_briefing(briefing: Briefing) -> str:
    """The plain-text morning summary."""
    lines: list[str] = []

    if briefing.total_worth_attention == 0:
        lines.append("Nothing worth your attention right now.")
        lines.append(f"{briefing.skipped_count} listings were filtered out.")
        if briefing.degraded_count:
            lines.append(
                f"{briefing.degraded_count} could not be evaluated and need a manual look."
            )
        return "\n".join(lines)

    lines.append(f"Today I found {briefing.total_worth_attention} jobs worth your attention.")
    lines.append("")
    if briefing.apply:
        lines.append(f"  {len(briefing.apply)} strong match(es)")
    if briefing.review:
        lines.append(f"  {len(briefing.review)} worth reviewing")
    lines.append(f"  {briefing.skipped_count} filtered out")

    top = briefing.top_pick
    if top is not None:
        evaluation = top.evaluation
        lines.append("")
        lines.append("Top pick:")
        lines.append(f"  {top.job.title} - {top.job.company_display}")
        lines.append(f"  {top.job.city or top.job.location_raw or 'location unknown'}")
        lines.append(f"  {evaluation.headline()}")
        # One per line, trimmed. The model writes full sentences, and joining
        # three of them with commas produces a paragraph nobody reads.
        for strength in evaluation.major_strengths[:3]:
            lines.append(f"  + {_first_sentence(strength, limit=100)}")
        for risk in evaluation.major_risks[:2]:
            lines.append(f"  - {_first_sentence(risk, limit=100)}")
        if top.unverified_claims:
            lines.append(f"  Check: {verification_warning(top.unverified_claims)}")
        if top.personalised:
            explanation = explain_preferences(top.matched_preferences)
            if explanation:
                lines.append(f"  Ranked up because {explanation}.")

    if briefing.degraded_count:
        lines.append("")
        lines.append(f"{briefing.degraded_count} listing(s) could not be evaluated automatically.")

    if briefing.stale_count:
        lines.append("")
        lines.append(
            f"{briefing.stale_count} listing(s) were judged against an older version of your "
            "profile or CV. Re-run `jobhunter evaluate --force` to refresh them."
        )

    return "\n".join(lines)

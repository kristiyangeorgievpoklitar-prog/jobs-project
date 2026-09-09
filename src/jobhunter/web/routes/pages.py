"""Overview and notification pages."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from jobhunter.db.models import CVFile
from jobhunter.profile.profile_store import get_active_profile
from jobhunter.web import deps
from jobhunter.web.routes._common import render

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def today(request: Request) -> HTMLResponse:
    """The home page answers one question: what is worth applying to now."""
    from jobhunter.briefing import build_briefing
    from jobhunter.personalization.ranker import explain_preferences

    context = deps.get_context()
    with context.session() as session:
        briefing = build_briefing(
            session, limit=12, candidate_fingerprint=deps.current_candidate_fingerprint(session)
        )
        runs = deps.recent_runs(session)

        # Templates cannot call the explain helper, so precompute the phrase.
        for ranked in (*briefing.apply, *briefing.review):
            ranked.preference_explanation = explain_preferences(ranked.matched_preferences)

    return render(
        request,
        "today.html",
        {
            "briefing": briefing,
            "runs": runs,
            "degraded_count": briefing.degraded_count,
            "model_label": (
                f"{context.settings.local_model} (local)"
                if context.settings.ai_provider == "local"
                else context.provider.describe()
            ),
        },
        active="overview",
    )


@router.get("/today", response_class=HTMLResponse)
async def today_jobs(request: Request) -> HTMLResponse:
    """Only what Jobs.bg published today, and what the pipeline made of it."""
    from datetime import date as date_type

    from jobhunter.matching.verification import unverified_technologies
    from jobhunter.profile.profile_store import to_snapshot
    from jobhunter.today import build_today_jobs, local_today

    context = deps.get_context()
    requested = request.query_params.get("date")
    try:
        day = date_type.fromisoformat(requested) if requested else local_today()
    except ValueError:
        day = local_today()

    with context.session() as session:
        today = build_today_jobs(session, day)
        candidate = to_snapshot(get_active_profile(session))
        last_run = next(iter(deps.recent_runs(session, limit=1)), None)

        # Templates cannot call the checker, so the claims are resolved here.
        for item in today.jobs:
            item.unverified_claims = unverified_technologies(item.evaluation, candidate)

    return render(
        request,
        "today_jobs.html",
        {"today": today, "last_run": last_run, "is_today": day == local_today()},
        active="today",
    )


@router.get("/overview", response_class=HTMLResponse)
async def overview(request: Request) -> HTMLResponse:
    """The older statistics view, kept for the numbers it still answers."""
    context = deps.get_context()
    with context.session() as session:
        stats = deps.overview_stats(session)
        top_jobs = deps.job_rows(session, min_score=1, limit=10)
        runs = deps.recent_runs(session)
        profile = get_active_profile(session)
        cv_count = session.scalar(select(func.count()).select_from(CVFile)) or 0

    return render(
        request,
        "overview.html",
        {
            "stats": stats,
            "top_jobs": top_jobs,
            "runs": runs,
            "profile": profile,
            "cv_count": cv_count,
            "provider": context.provider.describe(),
            "auto_apply": context.settings.auto_apply,
            "auto_threshold": context.settings.auto_apply_threshold,
            "review_threshold": context.settings.review_threshold,
        },
        active="stats",
    )


@router.get("/notifications", response_class=HTMLResponse)
async def notifications(request: Request) -> HTMLResponse:
    context = deps.get_context()
    with context.session() as session:
        items = deps.recent_notifications(session, limit=100)
    return render(request, "notifications.html", {"items": items}, active="notifications")

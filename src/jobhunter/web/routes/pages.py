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
async def overview(request: Request) -> HTMLResponse:
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
        active="overview",
    )


@router.get("/notifications", response_class=HTMLResponse)
async def notifications(request: Request) -> HTMLResponse:
    context = deps.get_context()
    with context.session() as session:
        items = deps.recent_notifications(session, limit=100)
    return render(request, "notifications.html", {"items": items}, active="notifications")

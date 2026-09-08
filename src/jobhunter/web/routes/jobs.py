"""Job list and detail pages."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import desc, select

from jobhunter.db.models import ApplicationEvent
from jobhunter.domain.enums import JobState
from jobhunter.web import deps
from jobhunter.web.routes._common import render

router = APIRouter()


@router.get("/jobs", response_class=HTMLResponse)
async def list_jobs(
    request: Request,
    state: str = "",
    min_score: int = 0,
    search: str = "",
    only_it: int = 0,
    order: str = "score",
    limit: int = 200,
) -> HTMLResponse:
    context = deps.get_context()
    with context.session() as session:
        rows = deps.job_rows(
            session,
            state=state or None,
            min_score=min_score,
            search=search or None,
            only_it=bool(only_it),
            order=order,
            limit=limit,
        )
    return render(
        request,
        "jobs.html",
        {
            "rows": rows,
            "state": state,
            "min_score": min_score,
            "search": search,
            "only_it": bool(only_it),
            "order": order,
            "states": [s.value for s in JobState],
        },
        active="jobs",
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail(request: Request, job_id: int) -> HTMLResponse:
    context = deps.get_context()
    with context.session() as session:
        detail = deps.job_detail(session, job_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Job not found")
        events = list(
            session.scalars(
                select(ApplicationEvent)
                .where(ApplicationEvent.job_id == job_id)
                .order_by(desc(ApplicationEvent.id))
                .limit(25)
            ).all()
        )
        # Touch the relationship while the session is open.
        if detail["application"] is not None:
            _ = detail["application"].cv_file

    return render(
        request,
        "job_detail.html",
        {
            "job": detail["job"],
            "match": detail["match"],
            "application": detail["application"],
            "events": events,
            "auto_apply": context.settings.auto_apply,
        },
        active="jobs",
    )

"""Applications page."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import desc, select
from sqlalchemy.orm import joinedload

from jobhunter.db.models import Application, Job
from jobhunter.web import deps
from jobhunter.web.routes._common import render

router = APIRouter()


@router.get("/applications", response_class=HTMLResponse)
async def list_applications(request: Request) -> HTMLResponse:
    context = deps.get_context()
    with context.session() as session:
        records = list(
            session.scalars(select(Application).order_by(desc(Application.id)).limit(200)).all()
        )
        rows = []
        for record in records:
            _ = record.cv_file  # load inside the session
            job = session.scalar(
                select(Job).where(Job.id == record.job_id).options(joinedload(Job.company))
            )
            rows.append({"application": record, "job": job})
    return render(request, "applications.html", {"rows": rows}, active="applications")

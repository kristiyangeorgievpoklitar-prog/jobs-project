"""Recording the candidate's own decision on a job."""

from __future__ import annotations

from fastapi import APIRouter, Form
from fastapi.responses import RedirectResponse

from jobhunter.logging_setup import get_logger
from jobhunter.pipeline.feedback_store import record_feedback
from jobhunter.web import deps
from jobhunter.web.routes._common import redirect

log = get_logger(__name__)
router = APIRouter()


@router.post("/feedback/{job_id}")
async def submit_feedback(
    job_id: int,
    action: str = Form(...),
    reason: str | None = Form(None),
    note: str | None = Form(None),
    next_url: str = Form("/"),
) -> RedirectResponse:
    """Store a decision, then send the candidate back where they were."""
    context = deps.get_context()
    with context.session() as session:
        try:
            record_feedback(session, job_id, action=action, reason=reason or None, note=note)
        except ValueError as exc:
            return redirect(next_url, str(exc), level="error")

    label = {"apply": "Marked as applied", "skip": "Skipped", "not_sure": "Marked unsure"}.get(
        action, "Recorded"
    )
    return redirect(next_url, f"{label}. Future rankings will use this.", level="success")

"""Settings page: runtime configuration, candidate profile and CVs."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from jobhunter.domain.enums import Seniority
from jobhunter.profile.cv import list_cvs, register_cv, set_default_cv
from jobhunter.profile.profile_store import get_or_create_profile, update_profile
from jobhunter.settings_store import EDITABLE_KEYS, load_overrides, save_overrides
from jobhunter.web import deps
from jobhunter.web.routes._common import redirect, render

router = APIRouter()

LIST_FIELDS = ("skills", "frameworks", "databases", "tools", "soft_skills", "preferred_locations")


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    context = deps.get_context()
    with context.session() as session:
        profile = get_or_create_profile(session)
        cvs = list_cvs(session)
        overrides = load_overrides(session)
        profile_data = {
            "full_name": profile.full_name,
            "email": profile.email,
            "phone": profile.phone,
            "location": profile.location,
            "years_experience": profile.years_experience,
            "desired_seniority": profile.desired_seniority,
            "remote_ok": profile.remote_ok,
            "summary": profile.summary,
            "github_url": profile.github_url,
            "linkedin_url": profile.linkedin_url,
            "portfolio_url": profile.portfolio_url,
            "version": profile.version,
            **{field: ", ".join(getattr(profile, field) or []) for field in LIST_FIELDS},
            "languages": json.dumps(profile.languages or [], ensure_ascii=False),
        }

    return render(
        request,
        "settings.html",
        {
            "profile": profile_data,
            "cvs": cvs,
            "overrides": overrides,
            "seniorities": [s.value for s in Seniority if s is not Seniority.UNKNOWN],
            "editable_keys": sorted(EDITABLE_KEYS),
            "provider": context.provider.describe(),
        },
        active="settings",
    )


@router.post("/settings/profile")
async def save_profile(
    request: Request,
    full_name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    location: str = Form(""),
    years_experience: float = Form(0.0),
    desired_seniority: str = Form("junior_mid"),
    remote_ok: str = Form(""),
    summary: str = Form(""),
    skills: str = Form(""),
    frameworks: str = Form(""),
    databases: str = Form(""),
    tools: str = Form(""),
    soft_skills: str = Form(""),
    preferred_locations: str = Form(""),
    languages: str = Form("[]"),
    github_url: str = Form(""),
    linkedin_url: str = Form(""),
    portfolio_url: str = Form(""),
) -> RedirectResponse:
    """Persist the candidate profile."""

    def split(value: str) -> list[str]:
        return [item.strip() for item in value.split(",") if item.strip()]

    try:
        parsed_languages = json.loads(languages) if languages.strip() else []
        if not isinstance(parsed_languages, list):
            parsed_languages = []
    except json.JSONDecodeError:
        return redirect("/settings", "Languages must be valid JSON.", "error")

    values = {
        "full_name": full_name.strip(),
        "email": email.strip() or None,
        "phone": phone.strip() or None,
        "location": location.strip(),
        "years_experience": float(years_experience),
        "desired_seniority": Seniority(desired_seniority),
        "remote_ok": remote_ok == "on",
        "summary": summary.strip() or None,
        "skills": split(skills),
        "frameworks": split(frameworks),
        "databases": split(databases),
        "tools": split(tools),
        "soft_skills": split(soft_skills),
        "preferred_locations": split(preferred_locations),
        "languages": parsed_languages,
        "github_url": github_url.strip() or None,
        "linkedin_url": linkedin_url.strip() or None,
        "portfolio_url": portfolio_url.strip() or None,
    }

    context = deps.get_context()
    with context.session() as session:
        update_profile(session, values)
    return redirect("/settings", "Profile saved.", "success")


@router.post("/settings/runtime")
async def save_runtime(request: Request) -> RedirectResponse:
    """Persist runtime settings from the form."""
    form = await request.form()
    values: dict[str, object] = {}
    for key in EDITABLE_KEYS:
        if EDITABLE_KEYS[key] is bool:
            values[key] = key in form
        elif key in form:
            values[key] = form[key]

    context = deps.get_context()
    with context.session() as session:
        save_overrides(session, values)
    context.reload_settings()

    warning = ""
    if not context.settings.thresholds_are_sane():
        warning = " Warning: the apply threshold is below the review threshold."
    return redirect("/settings", f"Settings saved.{warning}", "warning" if warning else "success")


@router.post("/settings/cv")
async def add_cv(
    request: Request, cv_path: str = Form(...), make_default: str = Form("")
) -> RedirectResponse:
    """Register a CV by local path."""
    path = Path(cv_path.strip()).expanduser()
    if not path.exists():
        return redirect("/settings", f"File not found: {path}", "error")

    context = deps.get_context()
    try:
        with context.session() as session:
            record = register_cv(session, path, is_default=(make_default == "on"))
            name, language = record.filename, record.language.value
    except Exception as exc:
        return redirect("/settings", f"Could not register CV: {exc}", "error")
    return redirect("/settings", f"Registered {name} ({language}).", "success")


@router.post("/settings/cv/{cv_id}/default")
async def make_default_cv(request: Request, cv_id: int) -> RedirectResponse:
    context = deps.get_context()
    with context.session() as session:
        set_default_cv(session, cv_id)
    return redirect("/settings", "Default CV updated.", "success")

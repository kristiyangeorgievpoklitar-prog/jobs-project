"""FastAPI application factory for the local dashboard."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from jobhunter.context import AppContext
from jobhunter.logging_setup import get_logger
from jobhunter.web import deps

log = get_logger(__name__)

WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _score_class(score: int | None) -> str:
    if score is None:
        return "score-none"
    if score >= 90:
        return "score-high"
    if score >= 75:
        return "score-good"
    if score >= 50:
        return "score-mid"
    return "score-low"


def _state_class(state: object) -> str:
    return f"state-{str(state).replace('_', '-')}"


def _format_dt(value: object, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if value is None:
        return "-"
    try:
        return value.strftime(fmt)  # type: ignore[attr-defined]
    except Exception:
        return str(value)


templates.env.filters["score_class"] = _score_class
templates.env.filters["state_class"] = _state_class
templates.env.filters["dt"] = _format_dt


def create_app(context: AppContext | None = None) -> FastAPI:
    context = context or AppContext()
    deps.set_context(context)

    app = FastAPI(title="JobHunter", docs_url="/api/docs", redoc_url=None)
    app.state.context = context

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from jobhunter.web.routes import actions, applications, jobs, pages, settings_routes

    app.include_router(pages.router)
    app.include_router(jobs.router)
    app.include_router(applications.router)
    app.include_router(settings_routes.router)
    app.include_router(actions.router)

    @app.exception_handler(404)
    async def not_found(request: Request, exc: object) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={"code": 404, "message": "Page not found"},
            status_code=404,
        )

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    log.info("web_app_created", provider=context.provider.name)
    return app

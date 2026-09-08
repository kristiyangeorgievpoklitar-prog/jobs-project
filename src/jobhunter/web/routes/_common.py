"""Shared rendering helper for the dashboard routes."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

from jobhunter.web import deps


def render(
    request: Request, template: str, context: dict[str, Any], *, active: str = ""
) -> HTMLResponse:
    """Render a template with the globals every page needs."""
    from jobhunter.web.app import templates

    app_context = deps.get_context()
    with app_context.session() as session:
        unread = deps.unread_notification_count(session)

    payload = {
        "active": active,
        "unread": unread,
        "settings": app_context.settings,
        **context,
    }
    return templates.TemplateResponse(request=request, name=template, context=payload)


def redirect(path: str, message: str | None = None, level: str = "info") -> RedirectResponse:
    """Redirect after a POST, carrying a flash message in the query string."""
    if message:
        path = f"{path}{'&' if '?' in path else '?'}{urlencode({'msg': message, 'level': level})}"
    return RedirectResponse(path, status_code=303)

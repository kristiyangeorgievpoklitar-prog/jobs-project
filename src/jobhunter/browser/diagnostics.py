"""Failure diagnostics: screenshots and page dumps."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jobhunter.logging_setup import get_logger

log = get_logger(__name__)

_SAFE = re.compile(r"[^a-zA-Z0-9_.-]+")
# Never persist a session cookie or CSRF token into a debug artifact.
_SECRET_IN_HTML = re.compile(r'(csrf_token"?\s*[:=]\s*"?)([A-Za-z0-9._\-]{8,})', re.IGNORECASE)


def _slug(value: str, limit: int = 60) -> str:
    return _SAFE.sub("-", value)[:limit].strip("-") or "page"


def timestamped_name(tag: str, suffix: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}_{_slug(tag)}{suffix}"


def capture(page: Any, directory: Path, tag: str, *, full_page: bool = False) -> dict[str, str]:
    """Save a screenshot and a scrubbed HTML dump. Never raises."""
    directory.mkdir(parents=True, exist_ok=True)
    artifacts: dict[str, str] = {}

    try:
        shot = directory / timestamped_name(tag, ".png")
        page.screenshot(path=str(shot), full_page=full_page)
        artifacts["screenshot"] = str(shot)
    except Exception as exc:
        log.warning("screenshot_failed", tag=tag, error=str(exc))

    try:
        dump = directory / timestamped_name(tag, ".html")
        html = page.content()
        dump.write_text(_SECRET_IN_HTML.sub(r"\1REDACTED", html), encoding="utf-8")
        artifacts["html"] = str(dump)
    except Exception as exc:
        log.warning("html_dump_failed", tag=tag, error=str(exc))

    return artifacts

"""Runtime settings stored in the database.

These override values from ``.env`` so the dashboard can change behaviour
without editing files or restarting. Secrets are deliberately not editable here.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobhunter.config import Settings
from jobhunter.db.models import Setting
from jobhunter.logging_setup import get_logger

log = get_logger(__name__)

# Only these may be changed from the UI. API keys and paths are excluded.
EDITABLE_KEYS: dict[str, type] = {
    "search_location": str,
    "max_pages_per_scan": int,
    "max_jobs_per_scan": int,
    "auto_apply": bool,
    "auto_apply_threshold": int,
    "review_threshold": int,
    "max_seniority": str,
    "max_auto_applications_per_run": int,
    "browser_headless": bool,
    "request_delay_seconds": float,
    "scheduler_enabled": bool,
    "scan_interval_hours": float,
    "scan_at_hour": int,
    "cover_letter_enabled": bool,
    "cover_letter_max_words": int,
    "notify_console": bool,
    "notify_dashboard": bool,
    "ai_provider": str,
}


def load_overrides(session: Session) -> dict[str, Any]:
    """Read stored overrides, ignoring unknown or malformed keys."""
    overrides: dict[str, Any] = {}
    for row in session.scalars(select(Setting)).all():
        if row.key not in EDITABLE_KEYS:
            continue
        overrides[row.key] = row.value
    return overrides


def save_overrides(session: Session, values: dict[str, Any]) -> dict[str, Any]:
    """Persist overrides after coercing and validating each value."""
    from jobhunter.config import get_settings

    base_settings = get_settings()
    saved: dict[str, Any] = {}
    for key, raw in values.items():
        expected = EDITABLE_KEYS.get(key)
        if expected is None:
            continue
        try:
            value = _coerce(raw, expected)
        except (TypeError, ValueError):
            log.warning("setting_rejected", key=key)
            continue

        if not is_valid_override(base_settings, key, value):
            log.warning("setting_out_of_range", key=key)
            continue

        row = session.get(Setting, key)
        if row is None:
            session.add(Setting(key=key, value=value))
        else:
            row.value = value
        saved[key] = value
    session.flush()
    return saved


def _coerce(raw: Any, expected: type) -> Any:
    if expected is bool:
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    if expected is int:
        return int(float(raw))
    if expected is float:
        return float(raw)
    return str(raw).strip()


def is_valid_override(settings: Settings, key: str, value: Any) -> bool:
    """Whether a single override passes the field's own validation rules."""
    if key not in EDITABLE_KEYS:
        return False
    try:
        Settings.model_validate({**settings.model_dump(), key: value})
    except ValidationError:
        return False
    return True


def apply_overrides(settings: Settings, overrides: dict[str, Any]) -> Settings:
    """Return a settings copy with overrides applied.

    ``model_copy`` does not re-run validators, so each override is validated
    explicitly; anything out of range is dropped rather than silently accepted.
    """
    if not overrides:
        return settings

    clean = {k: v for k, v in overrides.items() if k in EDITABLE_KEYS}
    if not clean:
        return settings

    base = settings.model_dump()
    try:
        return Settings.model_validate({**base, **clean})
    except ValidationError:
        pass

    valid: dict[str, Any] = {}
    for key, value in clean.items():
        if is_valid_override(settings, key, value):
            valid[key] = value
        else:
            log.warning("settings_override_invalid", key=key)

    if not valid:
        return settings
    try:
        return Settings.model_validate({**base, **valid})
    except ValidationError as exc:
        log.warning("settings_override_failed", error=str(exc))
        return settings

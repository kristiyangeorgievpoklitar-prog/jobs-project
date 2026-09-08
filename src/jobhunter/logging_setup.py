"""Structured logging with automatic redaction of sensitive values.

Passwords, cookies and API tokens must never reach the log files, so redaction
happens in a processor rather than at every call site.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path
from typing import Any

import structlog

SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "authorization",
        "auth",
        "cookie",
        "cookies",
        "session",
        "session_id",
        "set-cookie",
        "csrf",
        "bearer",
        "credentials",
        "anthropic_api_key",
        "openai_api_key",
        "telegram_bot_token",
    }
)

REDACTED = "***REDACTED***"

# Catches "password=hunter2", "token: abc", "Authorization: Bearer xyz" in free text.
_INLINE_SECRET = re.compile(
    r"(?i)\b(password|passwd|pwd|token|api[-_]?key|authorization|bearer|cookie)\b"
    r"\s*[:=]\s*[\"']?([^\s\"',;}]{3,})"
)


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: (REDACTED if _is_sensitive(k) else _redact_value(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_redact_value(v) for v in value)
    if isinstance(value, str):
        return _INLINE_SECRET.sub(lambda m: f"{m.group(1)}={REDACTED}", value)
    return value


def _is_sensitive(key: str) -> bool:
    return key.lower().replace("-", "_") in SENSITIVE_KEYS


def redact_processor(
    _logger: Any, _method: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """structlog processor that scrubs secrets from every logged event."""
    for key in list(event_dict.keys()):
        if _is_sensitive(key):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = _redact_value(event_dict[key])
    return event_dict


def configure_logging(
    level: str = "INFO",
    *,
    json_logs: bool = False,
    logs_dir: Path | None = None,
) -> None:
    """Configure structlog + stdlib logging. Safe to call more than once."""
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
        redact_processor,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            logs_dir / "jobhunter.log",
            maxBytes=5_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                foreign_pre_chain=shared_processors,
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.processors.JSONRenderer(),
                ],
            )
        )
        root.addHandler(file_handler)

    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio", "apscheduler.executors"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger."""
    return structlog.stdlib.get_logger(name)  # type: ignore[no-any-return]

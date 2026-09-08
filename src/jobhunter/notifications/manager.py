"""Fans one notification out to every enabled channel."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager

from sqlalchemy.orm import Session

from jobhunter.config import Settings
from jobhunter.domain.enums import NotificationKind, NotificationLevel
from jobhunter.logging_setup import get_logger
from jobhunter.notifications.base import Notification, NotificationChannel
from jobhunter.notifications.console import ConsoleChannel
from jobhunter.notifications.dashboard import DashboardChannel
from jobhunter.notifications.telegram import TelegramChannel

log = get_logger(__name__)


class NotificationManager:
    def __init__(self, channels: list[NotificationChannel] | None = None) -> None:
        self.channels = channels or []

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        session_factory: Callable[[], AbstractContextManager[Session]] | None = None,
    ) -> NotificationManager:
        channels: list[NotificationChannel] = [ConsoleChannel(settings.notify_console)]
        if session_factory is not None and settings.notify_dashboard:
            channels.append(DashboardChannel(session_factory, True))
        if settings.telegram_enabled:
            token = (
                settings.telegram_bot_token.get_secret_value()
                if settings.telegram_bot_token
                else None
            )
            channels.append(TelegramChannel(token, settings.telegram_chat_id))
        return cls(channels)

    def notify(self, notification: Notification) -> int:
        """Send to all enabled channels; returns how many succeeded."""
        delivered = 0
        for channel in self.channels:
            if not channel.is_enabled():
                continue
            try:
                if channel.send(notification):
                    delivered += 1
            except Exception as exc:
                log.warning("notification_channel_failed", channel=channel.name, error=str(exc))
        return delivered

    # ------------------------------------------------------ convenience API

    def high_match_job(
        self,
        *,
        title: str,
        company: str,
        location: str,
        score: int,
        recommendation: str,
        job_id: int,
        reason: str | None = None,
        risk: str | None = None,
    ) -> None:
        """Announce a listing worth the candidate's attention.

        Led by the decision rather than a percentage: "APPLY, because the stack
        matches" is actionable in a phone notification, "87%" is not. ``score``
        is retained as the model's confidence, for the record.
        """
        headline = "WORTH APPLYING" if recommendation == "apply" else "WORTH A LOOK"
        lines = [title, f"Company: {company}", f"Location: {location}"]
        if reason:
            lines.append(f"\nWhy: {reason}")
        if risk:
            lines.append(f"Risk: {risk}")

        self.notify(
            Notification(
                kind=NotificationKind.HIGH_MATCH_JOB,
                level=NotificationLevel.SUCCESS,
                title=f"{headline} — {title[:60]}",
                body="\n".join(lines),
                job_id=job_id,
                detail={
                    "recommendation": recommendation,
                    "confidence": score,
                    "reason": reason,
                    "risk": risk,
                },
            )
        )

    def application_success(
        self, *, title: str, company: str, job_id: int, evidence: str | None = None
    ) -> None:
        self.notify(
            Notification(
                kind=NotificationKind.APPLICATION_SUCCESS,
                level=NotificationLevel.SUCCESS,
                title="Application submitted",
                body=f"{title}\nCompany: {company}"
                + (f"\nEvidence: {evidence}" if evidence else ""),
                job_id=job_id,
            )
        )

    def application_failed(self, *, title: str, company: str, job_id: int, reason: str) -> None:
        self.notify(
            Notification(
                kind=NotificationKind.APPLICATION_FAILED,
                level=NotificationLevel.ERROR,
                title="Application failed",
                body=f"{title}\nCompany: {company}\nReason: {reason}",
                job_id=job_id,
            )
        )

    def blocked(self, *, reason: str, job_id: int | None = None, url: str | None = None) -> None:
        self.notify(
            Notification(
                kind=NotificationKind.AUTOMATION_BLOCKED,
                level=NotificationLevel.WARNING,
                title="Automation blocked — manual step required",
                body=f"{reason}" + (f"\nURL: {url}" if url else ""),
                job_id=job_id,
                detail={"url": url} if url else {},
            )
        )

    def challenge_detected(self, *, challenge_type: str, url: str | None = None) -> None:
        self.notify(
            Notification(
                kind=NotificationKind.CHALLENGE_DETECTED,
                level=NotificationLevel.WARNING,
                title="Anti-bot challenge detected",
                body=(
                    f"Jobs.bg presented a {challenge_type} challenge. "
                    "The workflow was paused. Complete it in the open browser window, "
                    "then run the scan again." + (f"\nURL: {url}" if url else "")
                ),
                detail={"challenge": challenge_type, "url": url},
            )
        )

    def auth_expired(self) -> None:
        self.notify(
            Notification(
                kind=NotificationKind.AUTH_EXPIRED,
                level=NotificationLevel.WARNING,
                title="Jobs.bg session expired",
                body="Log in again in the browser window, then re-run the scan.",
            )
        )

    def scan_completed(self, *, stats: dict[str, int]) -> None:
        body = "\n".join(f"{k.replace('_', ' ').title()}: {v}" for k, v in stats.items())
        self.notify(
            Notification(
                kind=NotificationKind.SCAN_COMPLETED,
                level=NotificationLevel.INFO,
                title="Scan completed",
                body=body,
                detail=dict(stats),
            )
        )

    def scan_failed(self, *, error: str) -> None:
        self.notify(
            Notification(
                kind=NotificationKind.SCAN_FAILED,
                level=NotificationLevel.ERROR,
                title="Scan failed",
                body=error,
            )
        )

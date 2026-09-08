"""Dashboard notification channel: persists notifications for the web UI."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager

from sqlalchemy.orm import Session

from jobhunter.db.models import Notification as NotificationRow
from jobhunter.logging_setup import get_logger
from jobhunter.notifications.base import Notification, NotificationChannel

log = get_logger(__name__)


class DashboardChannel(NotificationChannel):
    name = "dashboard"

    def __init__(
        self,
        session_factory: Callable[[], AbstractContextManager[Session]],
        enabled: bool = True,
    ) -> None:
        self.session_factory = session_factory
        self.enabled = enabled

    def is_enabled(self) -> bool:
        return self.enabled

    def send(self, notification: Notification) -> bool:
        if not self.enabled:
            return False
        try:
            with self.session_factory() as session:
                session.add(
                    NotificationRow(
                        kind=notification.kind,
                        level=notification.level,
                        title=notification.title,
                        body=notification.body or None,
                        job_id=notification.job_id,
                        detail=notification.detail,
                    )
                )
            return True
        except Exception as exc:
            log.warning("dashboard_notification_failed", error=str(exc))
            return False

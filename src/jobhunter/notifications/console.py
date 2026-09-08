"""Console notification channel."""

from __future__ import annotations

from jobhunter.domain.enums import NotificationLevel
from jobhunter.notifications.base import Notification, NotificationChannel

_PREFIX = {
    NotificationLevel.INFO: "[i]",
    NotificationLevel.SUCCESS: "[+]",
    NotificationLevel.WARNING: "[!]",
    NotificationLevel.ERROR: "[x]",
}


class ConsoleChannel(NotificationChannel):
    name = "console"

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def is_enabled(self) -> bool:
        return self.enabled

    def send(self, notification: Notification) -> bool:
        if not self.enabled:
            return False
        prefix = _PREFIX.get(notification.level, "[i]")
        border = "-" * 58
        print(f"\n{border}\n{prefix} {notification.title}")
        if notification.body:
            print(notification.body)
        print(border)
        return True

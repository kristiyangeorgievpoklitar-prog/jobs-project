"""Notification abstraction."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from jobhunter.domain.enums import NotificationKind, NotificationLevel


@dataclass
class Notification:
    kind: NotificationKind
    title: str
    body: str = ""
    level: NotificationLevel = NotificationLevel.INFO
    job_id: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_text(self) -> str:
        lines = [self.title]
        if self.body:
            lines.append(self.body)
        return "\n".join(lines)


class NotificationChannel(ABC):
    name: str = "base"

    @abstractmethod
    def send(self, notification: Notification) -> bool:
        """Deliver a notification. Returns True on success."""

    def is_enabled(self) -> bool:
        return True

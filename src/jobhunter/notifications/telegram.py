"""Telegram notification channel (optional)."""

from __future__ import annotations

import httpx

from jobhunter.logging_setup import get_logger
from jobhunter.notifications.base import Notification, NotificationChannel

log = get_logger(__name__)

API_TEMPLATE = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramChannel(NotificationChannel):
    name = "telegram"

    def __init__(self, token: str | None, chat_id: str | None, timeout: float = 10.0) -> None:
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout

    def is_enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, notification: Notification) -> bool:
        if not self.is_enabled():
            return False
        try:
            response = httpx.post(
                API_TEMPLATE.format(token=self.token),
                json={
                    "chat_id": self.chat_id,
                    "text": notification.as_text()[:4000],
                    "disable_web_page_preview": True,
                },
                timeout=self.timeout,
            )
            if response.status_code != 200:
                # Never log the response body: it echoes the bot token.
                log.warning("telegram_send_failed", status=response.status_code)
                return False
            return True
        except Exception as exc:
            log.warning("telegram_send_error", error=type(exc).__name__)
            return False

"""Anti-automation challenge detection.

The system never attempts to solve or evade a challenge. It detects one,
stops the affected workflow, and surfaces it to the user so they can complete
it themselves in the visible browser.

Detection deliberately looks at *visible* page state rather than searching the
HTML for words like "captcha": Jobs.bg loads a reCAPTCHA script on its normal
application form, so a substring match would produce constant false positives.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from jobhunter.sources.jobsbg.selectors import (
    CHALLENGE_BODY_MARKERS,
    CHALLENGE_TITLE_MARKERS,
)


class ChallengeType(StrEnum):
    NONE = "none"
    CLOUDFLARE_INTERSTITIAL = "cloudflare_interstitial"
    CAPTCHA = "captcha"
    LOGIN_REQUIRED = "login_required"
    RATE_LIMITED = "rate_limited"
    ACCESS_DENIED = "access_denied"


@dataclass(frozen=True)
class ChallengeResult:
    type: ChallengeType = ChallengeType.NONE
    detail: str | None = None
    signals: list[str] = field(default_factory=list)

    @property
    def detected(self) -> bool:
        return self.type is not ChallengeType.NONE

    @property
    def is_blocking(self) -> bool:
        """Whether the workflow must stop and hand control back to the user."""
        return self.type in {
            ChallengeType.CLOUDFLARE_INTERSTITIAL,
            ChallengeType.CAPTCHA,
            ChallengeType.ACCESS_DENIED,
            ChallengeType.RATE_LIMITED,
        }


# A challenge interstitial is a nearly-empty page; a real page is not.
INTERSTITIAL_MAX_VISIBLE_CHARS = 900

LOGIN_MARKERS = (
    "please log in",
    "моля, влезте",
    "необходимо е да влезете",
    "sign in to continue",
    "сесията ви изтече",
    "session expired",
)

RATE_LIMIT_MARKERS = (
    "too many requests",
    "прекалено много заявки",
    "rate limit",
    "429",
)

DENIED_MARKERS = (
    "access denied",
    "достъпът е отказан",
    "you have been blocked",
    "forbidden",
)


def detect_challenge(
    *,
    title: str = "",
    visible_text: str = "",
    html: str = "",
    status: int | None = None,
    visible_captcha_widget: bool = False,
) -> ChallengeResult:
    """Classify the current page state.

    ``visible_captcha_widget`` is supplied by the browser layer after checking
    whether a challenge iframe actually has a bounding box on screen.
    """
    title_l = (title or "").lower()
    text_l = (visible_text or "").lower()
    html_l = (html or "").lower()
    signals: list[str] = []

    for marker in CHALLENGE_TITLE_MARKERS:
        if marker in title_l:
            signals.append(f"title:{marker}")
            return ChallengeResult(
                ChallengeType.CLOUDFLARE_INTERSTITIAL,
                "Cloudflare interstitial detected via page title",
                signals,
            )

    for marker in CHALLENGE_BODY_MARKERS:
        if marker in text_l:
            signals.append(f"body:{marker}")
            return ChallengeResult(
                ChallengeType.CLOUDFLARE_INTERSTITIAL,
                "Cloudflare interstitial detected in visible text",
                signals,
            )

    # A Cloudflare challenge script on an otherwise empty page is an
    # interstitial; the same script on a full page is just Cloudflare running.
    if "cf_chl_opt" in html_l and len(text_l.strip()) < INTERSTITIAL_MAX_VISIBLE_CHARS:
        signals.append("html:cf_chl_opt+empty-body")
        return ChallengeResult(
            ChallengeType.CLOUDFLARE_INTERSTITIAL,
            "Challenge script present on an otherwise empty page",
            signals,
        )

    if visible_captcha_widget:
        signals.append("widget:visible-captcha")
        return ChallengeResult(
            ChallengeType.CAPTCHA,
            "A CAPTCHA widget is visible and must be completed by a human",
            signals,
        )

    # A status code alone is not proof of a block. Cloudflare answers the first
    # request from a cold browser profile with 403 and a challenge page, which
    # then clears itself; by the time we look, the real page is rendered. So an
    # error status only counts when the page is also devoid of content, while
    # explicit text markers count on their own.
    page_is_empty = len(text_l.strip()) < INTERSTITIAL_MAX_VISIBLE_CHARS

    if any(m in text_l for m in RATE_LIMIT_MARKERS) or (status == 429 and page_is_empty):
        signals.append("rate-limit")
        return ChallengeResult(
            ChallengeType.RATE_LIMITED, "The site is rate limiting requests", signals
        )

    if any(m in text_l for m in DENIED_MARKERS) or (status == 403 and page_is_empty):
        signals.append("denied")
        return ChallengeResult(ChallengeType.ACCESS_DENIED, "Access denied by the site", signals)

    if any(m in text_l for m in LOGIN_MARKERS):
        signals.append("login")
        return ChallengeResult(
            ChallengeType.LOGIN_REQUIRED, "The site is asking for authentication", signals
        )

    return ChallengeResult()


class ChallengeDetectedError(RuntimeError):
    """Raised to abort a workflow when a challenge blocks progress."""

    def __init__(self, result: ChallengeResult, url: str | None = None) -> None:
        super().__init__(result.detail or f"{result.type.value} detected")
        self.result = result
        self.url = url

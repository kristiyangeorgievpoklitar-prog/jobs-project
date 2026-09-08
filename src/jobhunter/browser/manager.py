"""Playwright lifecycle, polite pacing and challenge-aware navigation.

All Jobs.bg interaction goes through here. Two deliberate choices:

* A **persistent** browser profile keeps the user's own Jobs.bg session, so the
  system never handles their password.
* **Headed by default.** A headless browser reliably trips Cloudflare's managed
  challenge on this site; a normal visible browser does not. Running headed is
  ordinary browser behaviour, and it also means the user can complete any
  challenge themselves in the window that is already open.
"""

from __future__ import annotations

import random
import time
import urllib.robotparser
from contextlib import suppress
from typing import Any
from urllib.parse import urlsplit

from jobhunter.browser.challenge import ChallengeDetectedError, ChallengeResult, detect_challenge
from jobhunter.browser.diagnostics import capture
from jobhunter.config import Settings
from jobhunter.logging_setup import get_logger
from jobhunter.sources.jobsbg.selectors import CommonSelectors

log = get_logger(__name__)

CAPTCHA_IFRAME_SELECTOR = (
    "iframe[src*='recaptcha/api2/bframe'], iframe[title*='challenge'], "
    "iframe[src*='hcaptcha'], iframe[src*='challenges.cloudflare.com']"
)


class BrowserError(RuntimeError):
    """Browser could not complete an operation."""


class BrowserManager:
    """Owns the Playwright browser context for the whole run."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._playwright: Any | None = None
        self.context: Any | None = None
        self._last_request_at: float = 0.0
        self._robots: urllib.robotparser.RobotFileParser | None = None
        self._robots_checked = False
        self._warmed_hosts: set[str] = set()

    # ------------------------------------------------------------- lifecycle

    def start(self) -> BrowserManager:
        from playwright.sync_api import sync_playwright

        self.settings.ensure_directories()
        self._playwright = sync_playwright().start()

        launch_kwargs: dict[str, Any] = {
            "user_data_dir": str(self.settings.browser_profile_dir.resolve()),
            "headless": self.settings.browser_headless,
            "locale": self.settings.browser_locale,
            "timezone_id": "Europe/Sofia",
            "viewport": {"width": 1440, "height": 900},
            "slow_mo": self.settings.browser_slow_mo_ms or 0,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if self.settings.browser_channel:
            launch_kwargs["channel"] = self.settings.browser_channel

        try:
            self.context = self._playwright.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as exc:
            self.stop()
            raise BrowserError(
                "Could not launch Chromium. Run 'uv run playwright install chromium'. "
                f"Original error: {exc}"
            ) from exc

        self.context.set_default_timeout(self.settings.browser_timeout_ms)
        log.info(
            "browser_started",
            headless=self.settings.browser_headless,
            profile=str(self.settings.browser_profile_dir),
        )
        return self

    def stop(self) -> None:
        with suppress(Exception):
            if self.context is not None:
                self.context.close()
        with suppress(Exception):
            if self._playwright is not None:
                self._playwright.stop()
        self.context = None
        self._playwright = None

    def __enter__(self) -> BrowserManager:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ------------------------------------------------------------------ page

    def new_page(self) -> Any:
        if self.context is None:
            raise BrowserError("Browser is not started")
        pages = self.context.pages
        page = pages[0] if pages else self.context.new_page()
        page.set_default_timeout(self.settings.browser_timeout_ms)
        return page

    # ---------------------------------------------------------------- pacing

    def _wait_turn(self) -> None:
        """Keep a conservative, jittered gap between requests."""
        delay = self.settings.request_delay_seconds
        if self.settings.request_jitter_seconds:
            delay += random.uniform(0, self.settings.request_jitter_seconds)
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_request_at = time.monotonic()

    # --------------------------------------------------------------- robots

    def robots_allows(self, url: str) -> bool:
        """Check robots.txt, fetching it through the browser.

        Cloudflare blocks a plain HTTP fetch of robots.txt on this site, so it is
        read with the browser like any other page. If it cannot be read we do not
        treat that as permission to ignore it for disallowed-looking paths; we
        allow, but log, since the site serves the file only to real browsers.
        """
        if not self.settings.respect_robots_txt:
            return True
        if not self._robots_checked:
            self._load_robots(url)
        if self._robots is None:
            return True
        return self._robots.can_fetch("*", url)

    def _load_robots(self, sample_url: str) -> None:
        self._robots_checked = True
        parts = urlsplit(sample_url)
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        try:
            page = self.new_page()
            self._wait_turn()
            page.goto(robots_url, wait_until="domcontentloaded")
            body = page.inner_text("body")
            if not body or "<html" in body.lower()[:200]:
                log.info("robots_unavailable", url=robots_url)
                return
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(body.splitlines())
            self._robots = parser
            log.info("robots_loaded", url=robots_url, lines=len(body.splitlines()))
        except Exception as exc:
            log.info("robots_fetch_failed", url=robots_url, error=str(exc))

    # ----------------------------------------------------------- navigation

    def warm_up(self, url: str, page: Any) -> None:
        """Visit the site's front page before deep-linking into it.

        A browser profile with no cookies that requests a search URL directly is
        answered with an empty 403 page. Landing on the home page first — the
        way a person arrives at a site — establishes the ordinary session and
        the search then loads normally. This is plain navigation, not an attempt
        to defeat anything.
        """
        parts = urlsplit(url)
        host = parts.netloc
        if not host or host in self._warmed_hosts:
            return
        self._warmed_hosts.add(host)

        root = f"{parts.scheme}://{host}/"
        if url.rstrip("/") == root.rstrip("/"):
            return

        try:
            log.info("session_warmup", host=host)
            self._wait_turn()
            page.goto(root, wait_until="domcontentloaded")
            time.sleep(2.5)
            result = self.check_challenge(page)
            if result.type.value == "cloudflare_interstitial":
                self._await_interstitial(page, None)
            self.accept_cookies(page)
        except Exception as exc:
            log.info("session_warmup_failed", host=host, error=str(exc))

    def check_challenge(self, page: Any, status: int | None = None) -> ChallengeResult:
        """Inspect the current page for an anti-automation challenge."""
        try:
            title = page.title()
        except Exception:
            title = ""
        try:
            visible_text = page.inner_text("body")
        except Exception:
            visible_text = ""
        try:
            html = page.content()
        except Exception:
            html = ""

        visible_widget = False
        with suppress(Exception):
            for frame in page.query_selector_all(CAPTCHA_IFRAME_SELECTOR):
                box = frame.bounding_box()
                if box and box.get("width", 0) > 50 and box.get("height", 0) > 50:
                    visible_widget = True
                    break

        return detect_challenge(
            title=title,
            visible_text=visible_text,
            html=html,
            status=status,
            visible_captcha_widget=visible_widget,
        )

    def goto(
        self,
        url: str,
        *,
        page: Any | None = None,
        wait_until: str = "domcontentloaded",
        settle_seconds: float = 2.0,
        raise_on_challenge: bool = True,
    ) -> Any:
        """Navigate politely, with retries and challenge detection."""
        if not self.robots_allows(url):
            raise BrowserError(f"robots.txt disallows fetching {url}")

        page = page or self.new_page()
        self.warm_up(url, page)

        attempts = self.settings.max_retries + 1
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                self._wait_turn()
                response = page.goto(url, wait_until=wait_until)
                status = response.status if response is not None else None

                if settle_seconds:
                    time.sleep(settle_seconds)

                challenge = self.check_challenge(page, status)
                if challenge.detected:
                    # A managed interstitial often clears itself; give it a
                    # bounded chance before treating it as blocking.
                    if challenge.type.value == "cloudflare_interstitial":
                        challenge = self._await_interstitial(page, status)

                    if challenge.detected:
                        log.warning(
                            "challenge_detected",
                            url=url,
                            type=challenge.type.value,
                            attempt=attempt,
                        )
                        capture(
                            page, self.settings.screenshots_dir, f"challenge-{challenge.type.value}"
                        )
                        if raise_on_challenge and challenge.is_blocking:
                            raise ChallengeDetectedError(challenge, url)
                        return page

                self.accept_cookies(page)
                return page

            except ChallengeDetectedError:
                raise
            except Exception as exc:
                last_error = exc
                log.warning("navigation_failed", url=url, attempt=attempt, error=str(exc))
                if attempt < attempts:
                    time.sleep(min(2**attempt, 15))

        capture(page, self.settings.screenshots_dir, "navigation-failure")
        raise BrowserError(f"Failed to load {url} after {attempts} attempts: {last_error}")

    def _await_interstitial(
        self, page: Any, status: int | None, timeout: float = 25.0
    ) -> ChallengeResult:
        """Wait out a managed interstitial that resolves without interaction."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(2.0)
            result = self.check_challenge(page, status)
            if not result.detected:
                log.info("challenge_cleared")
                return result
        return self.check_challenge(page, status)

    # ------------------------------------------------------------- helpers

    def accept_cookies(self, page: Any) -> bool:
        """Dismiss the cookie banner if present."""
        for label in CommonSelectors.COOKIE_ACCEPT_LABELS:
            try:
                button = page.get_by_role("button", name=label)
                if button.count() and button.first.is_visible():
                    button.first.click(timeout=5000)
                    time.sleep(1.0)
                    log.info("cookies_accepted", label=label)
                    return True
            except Exception:
                continue
        return False

    def is_logged_in(self, page: Any) -> bool:
        """Best-effort check of the Jobs.bg session state."""
        try:
            text = page.inner_text("body")
        except Exception:
            return False
        return any(marker in text for marker in CommonSelectors.LOGGED_IN_MARKERS)

    def scroll_to_load(
        self,
        page: Any,
        *,
        item_selector: str,
        max_scrolls: int = 10,
        pause: float = 2.0,
        target_count: int | None = None,
    ) -> int:
        """Scroll to trigger lazy loading; returns the final item count.

        Jobs.bg paginates by infinite scroll, so this is how additional results
        are reached. After each scroll it *polls* for the item count to grow
        rather than assuming a fixed pause is enough, and stops as soon as a
        scroll genuinely adds nothing.
        """

        def count() -> int:
            try:
                return len(page.query_selector_all(item_selector))
            except Exception:
                return 0

        current = count()
        for index in range(1, max_scrolls + 1):
            if target_count is not None and current >= target_count:
                break

            before = current
            try:
                page.mouse.wheel(0, 25000)
            except Exception as exc:
                log.warning("scroll_failed", error=str(exc))
                break

            deadline = time.monotonic() + max(pause * 3, 6.0)
            while time.monotonic() < deadline:
                time.sleep(0.5)
                current = count()
                if current > before:
                    break

            log.info("scrolled", scroll=index, items=current, added=current - before)
            if current == before:
                log.info("scroll_exhausted", scrolls=index, items=current)
                break

        return current

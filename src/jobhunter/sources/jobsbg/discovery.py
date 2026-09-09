"""Jobs.bg discovery: drive the search UI and collect listings.

Pagination on Jobs.bg is infinite-scroll (the ``frompage`` parameter is
ignored), so additional results are reached by scrolling, with a conservative
pause between scrolls.
"""

from __future__ import annotations

import time
from datetime import date, datetime
from typing import Any

from jobhunter.browser.challenge import ChallengeDetectedError
from jobhunter.browser.manager import BrowserManager
from jobhunter.config import Settings
from jobhunter.domain.schemas import RawJob
from jobhunter.logging_setup import get_logger
from jobhunter.sources.jobsbg import selectors as S
from jobhunter.sources.jobsbg.parser import (
    parse_detail_page,
    parse_listing_page,
    parse_posted_date,
    parse_total_results,
)
from jobhunter.sources.jobsbg.urls import (
    CATEGORY_IT,
    PUBLISHED_LAST_3_DAYS,
    PUBLISHED_LAST_7_DAYS,
    PUBLISHED_LAST_14_DAYS,
    PUBLISHED_TODAY,
    PUBLISHED_YESTERDAY,
    build_search_url,
)

log = get_logger(__name__)


def published_on(job: RawJob, day: date) -> bool:
    """Whether a listing card carries the given publication date.

    The card prints a calendar day and nothing finer — as ``DD.MM.YY``, or as
    ``днес``/``вчера`` for the last two days — so this is a day comparison.
    Those words are resolved against the real current date, never against
    ``day``: the site wrote them when the page was fetched, so asking for an
    earlier day must still read ``вчера`` as yesterday rather than as that day.
    A card whose date could not be read is *not* claimed for the day; guessing
    would let history through as "new today".
    """
    parsed = parse_posted_date(job.posted_at_raw)
    return parsed is not None and parsed.date() == day


# Each option of the site's "Публикувани" filter, as the oldest day it still
# includes. Ordered narrowest first so the smallest covering window wins.
_WINDOWS: tuple[tuple[int, int], ...] = (
    (0, PUBLISHED_TODAY),
    (1, PUBLISHED_YESTERDAY),
    (2, PUBLISHED_LAST_3_DAYS),
    (6, PUBLISHED_LAST_7_DAYS),
    (13, PUBLISHED_LAST_14_DAYS),
)


def published_window(day: date, today: date | None = None) -> int | None:
    """The narrowest publication filter that still contains ``day``.

    Returns ``None`` for a day the site cannot express — anything older than a
    fortnight, or in the future — and the caller then falls back to reading the
    unfiltered results and sieving them by card date.

    ``PUBLISHED_YESTERDAY`` is a single day like ``PUBLISHED_TODAY``; the wider
    options are cumulative windows ending today, so a day picked out of one of
    those still has to be filtered by its card date afterwards.
    """
    age = ((today or datetime.now().date()) - day).days
    if age < 0:
        return None
    for oldest, value in _WINDOWS:
        if age <= oldest:
            return value
    return None


class JobsBgDiscovery:
    """Discovers listings on Jobs.bg using a real browser."""

    name = "jobs.bg"

    def __init__(self, browser: BrowserManager, settings: Settings) -> None:
        self.browser = browser
        self.settings = settings

    def build_url(
        self,
        *,
        location: str | None = None,
        keywords: str | None = None,
        entry_level_only: bool = False,
        category: int | None = CATEGORY_IT,
        page: int = 1,
        posted_within: int | None = None,
    ) -> str:
        return build_search_url(
            location=location,
            category=category,
            keywords=keywords,
            entry_level_only=entry_level_only,
            page=page,
            posted_within=posted_within,
        )

    def search(
        self,
        *,
        location: str | None = None,
        keywords: str | None = None,
        entry_level_only: bool = False,
        max_results: int = 100,
        category: int | None = CATEGORY_IT,
        posted_on: date | None = None,
        page: Any | None = None,
    ) -> list[RawJob]:
        """Run a paginated search and return every listing found.

        Jobs.bg serves 20 results per page and paginates with ``page=N``.
        Pages are fetched sequentially, with the browser layer enforcing a
        polite delay between requests, and stop early once a page repeats
        results or the caller's limit is reached.

        ``posted_on`` narrows the crawl to one publication date. It is applied
        by the site itself wherever possible — the ``last`` parameter behind the
        "Публикувани" filter, which is the control the candidate clicks by hand
        — so a search for today returns today's listings and nothing else.

        Results are *not* ordered by date, which is why the day cannot be found
        by paging until the dates run out: measured live, an unfiltered IT
        search over Varna opened with 02.09, ran down to 18.08, then began a
        second block whose first card was the newest on the page. The one
        listing published that day was not on page 1 at all. Asking the site for
        the window is both cheaper and correct; the card date is then re-checked
        locally, because the wider windows are cumulative.
        """
        browser_page = page or self.browser.new_page()
        collected: dict[str, RawJob] = {}
        # Every listing id served so far, matching or not, to detect the end.
        seen_ids: set[str] = set()
        total: int | None = None
        pages_fetched = 0
        # None when the day is older than the site's widest window; the crawl
        # then reads unfiltered pages and relies on the card-date sieve below.
        window = published_window(posted_on) if posted_on is not None else None

        for page_number in range(1, self.settings.max_pages_per_scan + 1):
            url = self.build_url(
                location=location,
                keywords=keywords,
                entry_level_only=entry_level_only,
                category=category,
                page=page_number,
                posted_within=window,
            )
            log.info("discovery_page_start", page=page_number, url=url)

            browser_page = self.browser.goto(url, page=browser_page)
            html = browser_page.content()
            pages_fetched += 1

            if total is None:
                total = parse_total_results(html)
                log.info("discovery_total_reported", total=total)

            listings = parse_listing_page(html)
            if not listings:
                log.info("discovery_page_empty", page=page_number)
                break

            wanted = (
                [job for job in listings if published_on(job, posted_on)]
                if posted_on is not None
                else listings
            )

            new_on_page = 0
            for job in wanted:
                key = job.source_job_id or job.source_url
                if key not in collected:
                    collected[key] = job
                    new_on_page += 1

            # Exhaustion is judged on the page as served, not on what survived
            # the date sieve. A cumulative window can hand back a page holding
            # nothing from the target day while later pages still do, so
            # counting only matches here would stop the crawl early — the very
            # mistake that hid the day's one listing before.
            fresh_ids = {job.source_job_id or job.source_url for job in listings} - seen_ids
            seen_ids |= fresh_ids

            log.info(
                "discovery_page_done",
                page=page_number,
                on_page=len(listings),
                matching=len(wanted),
                new=new_on_page,
                total_collected=len(collected),
            )

            # A page repeating what we already read means there is no more.
            if not fresh_ids:
                break
            if len(collected) >= max_results:
                break
            if total is not None and len(collected) >= total:
                break

        result = list(collected.values())[:max_results]
        log.info(
            "discovery_search_done",
            found=len(result),
            reported_total=total,
            pages_fetched=pages_fetched,
            posted_on=posted_on.isoformat() if posted_on else None,
            window=window,
        )
        return result

    def read_description_frame(self, page: Any) -> str | None:
        """Read the posting body out of the sandboxed description iframe.

        Jobs.bg renders the description in an iframe rather than the page DOM, so
        this is the only place the requirements text actually exists.
        """
        marker = S.DetailSelectors.DESCRIPTION_IFRAME_URL_MARKER
        try:
            # The iframe is attached after the shell renders, so wait for it
            # rather than racing the page load.
            page.wait_for_selector(S.DetailSelectors.DESCRIPTION_IFRAME, timeout=8000)
        except Exception:
            log.warning("description_frame_absent")
            return None

        for _ in range(3):
            try:
                for frame in page.frames:
                    if marker in (frame.url or ""):
                        text = frame.locator("body").inner_text(timeout=5000)
                        if text.strip():
                            return text.strip()
            except Exception as exc:
                log.warning("description_frame_read_failed", error=str(exc))
            page.wait_for_timeout(700)
        return None

    def fetch_detail(self, url: str, *, page: Any | None = None) -> RawJob | None:
        """Load one job page and parse it fully."""
        try:
            page = self.browser.goto(url, page=page)
        except ChallengeDetectedError:
            raise
        except Exception as exc:
            log.warning("detail_fetch_failed", url=url, error=str(exc))
            return None

        description_text = self.read_description_frame(page)
        if not description_text:
            log.warning("description_frame_missing", url=url)

        try:
            job = parse_detail_page(
                page.content(), source_url=url, description_text=description_text
            )
        except Exception as exc:
            log.warning("detail_parse_failed", url=url, error=str(exc))
            return None

        if job is None:
            log.warning("detail_parse_empty", url=url)
        return job

    def enrich(
        self, jobs: list[RawJob], *, limit: int, page: Any | None = None
    ) -> dict[str, RawJob]:
        """Fetch detail pages for a bounded subset of listings.

        Listing cards omit the description and often the location, so detail
        pages are what make scoring accurate. The number fetched is capped to
        keep the request volume low.
        """
        enriched: dict[str, RawJob] = {}
        page = page or self.browser.new_page()

        for index, job in enumerate(jobs[:limit], start=1):
            try:
                detail = self.fetch_detail(job.source_url, page=page)
            except ChallengeDetectedError:
                log.warning("enrich_blocked_by_challenge", fetched=len(enriched))
                raise
            if detail is not None:
                enriched[job.source_url] = detail
            log.info("detail_fetched", index=index, of=min(limit, len(jobs)), url=job.source_url)
            time.sleep(0.2)

        return enriched


def merge_detail(card: RawJob, detail: RawJob | None) -> RawJob:
    """Overlay detail-page data onto a listing card.

    The card is authoritative for the company (the detail page sometimes omits
    it); the detail page is authoritative for everything it provides.
    """
    if detail is None:
        return card

    merged = card.model_copy(deep=True)
    for field in (
        "description",
        "location_raw",
        "level_raw",
        "experience_raw",
        "employment_raw",
        "work_mode_raw",
        "salary_raw",
        "application_method",
        "application_url",
        "raw_text",
        "posted_at_raw",
    ):
        value = getattr(detail, field, None)
        if value:
            setattr(merged, field, value)

    if detail.title and len(detail.title) > len(merged.title):
        merged.title = detail.title
    if not merged.company_name and detail.company_name:
        merged.company_name = detail.company_name
    if detail.tech_tags:
        merged.tech_tags = list(dict.fromkeys([*merged.tech_tags, *detail.tech_tags]))
    if detail.languages_raw:
        merged.languages_raw = detail.languages_raw
    if not merged.source_job_id and detail.source_job_id:
        merged.source_job_id = detail.source_job_id

    return merged

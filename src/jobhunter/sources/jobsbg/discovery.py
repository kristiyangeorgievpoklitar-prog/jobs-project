"""Jobs.bg discovery: drive the search UI and collect listings.

Pagination on Jobs.bg is infinite-scroll (the ``frompage`` parameter is
ignored), so additional results are reached by scrolling, with a conservative
pause between scrolls.
"""

from __future__ import annotations

import time
from typing import Any

from jobhunter.browser.challenge import ChallengeDetectedError
from jobhunter.browser.manager import BrowserManager
from jobhunter.config import Settings
from jobhunter.domain.schemas import RawJob
from jobhunter.logging_setup import get_logger
from jobhunter.sources.jobsbg.parser import (
    parse_detail_page,
    parse_listing_page,
    parse_total_results,
)
from jobhunter.sources.jobsbg.urls import CATEGORY_IT, build_search_url

log = get_logger(__name__)


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
    ) -> str:
        return build_search_url(
            location=location,
            category=category,
            keywords=keywords,
            entry_level_only=entry_level_only,
            page=page,
        )

    def search(
        self,
        *,
        location: str | None = None,
        keywords: str | None = None,
        entry_level_only: bool = False,
        max_results: int = 100,
        category: int | None = CATEGORY_IT,
        page: Any | None = None,
    ) -> list[RawJob]:
        """Run a paginated search and return every listing found.

        Jobs.bg serves 20 results per page and paginates with ``page=N``.
        Pages are fetched sequentially, with the browser layer enforcing a
        polite delay between requests, and stop early once a page repeats
        results or the caller's limit is reached.
        """
        browser_page = page or self.browser.new_page()
        collected: dict[str, RawJob] = {}
        total: int | None = None
        pages_fetched = 0

        for page_number in range(1, self.settings.max_pages_per_scan + 1):
            url = self.build_url(
                location=location,
                keywords=keywords,
                entry_level_only=entry_level_only,
                category=category,
                page=page_number,
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

            new_on_page = 0
            for job in listings:
                key = job.source_job_id or job.source_url
                if key not in collected:
                    collected[key] = job
                    new_on_page += 1

            log.info(
                "discovery_page_done",
                page=page_number,
                on_page=len(listings),
                new=new_on_page,
                total_collected=len(collected),
            )

            # A page that adds nothing means we have run past the last page.
            if new_on_page == 0:
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
        )
        return result

    def fetch_detail(self, url: str, *, page: Any | None = None) -> RawJob | None:
        """Load one job page and parse it fully."""
        try:
            page = self.browser.goto(url, page=page)
        except ChallengeDetectedError:
            raise
        except Exception as exc:
            log.warning("detail_fetch_failed", url=url, error=str(exc))
            return None

        try:
            job = parse_detail_page(page.content(), source_url=url)
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

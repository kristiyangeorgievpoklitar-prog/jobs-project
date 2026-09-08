"""Discovery pagination and detail merging, driven by a fake browser."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jobhunter.browser.challenge import ChallengeDetectedError, ChallengeResult, ChallengeType
from jobhunter.config import Settings
from jobhunter.domain.enums import ApplicationMethod
from jobhunter.domain.schemas import RawJob
from jobhunter.sources.jobsbg.discovery import JobsBgDiscovery, merge_detail

FIXTURES = Path(__file__).parent.parent / "fixtures"


class FakePage:
    def __init__(self) -> None:
        self.html = ""

    def content(self) -> str:
        return self.html


class PagingBrowser:
    """Serves a different page of results per `page=` value."""

    def __init__(self, pages: dict[int, str], challenge_on: str | None = None) -> None:
        self.pages = pages
        self.challenge_on = challenge_on
        self.requested: list[str] = []
        self.page = FakePage()

    def new_page(self) -> FakePage:
        return self.page

    def goto(self, url: str, page: Any = None, **kwargs: Any) -> FakePage:
        self.requested.append(url)
        if self.challenge_on and self.challenge_on in url:
            raise ChallengeDetectedError(
                ChallengeResult(ChallengeType.CLOUDFLARE_INTERSTITIAL, "blocked"), url
            )
        number = 1
        if "page=" in url:
            number = int(url.split("page=")[1].split("&")[0])
        target = page or self.page
        target.html = self.pages.get(number, "<html><body></body></html>")
        return target


def listing_with(ids: list[int], total: int = 100) -> str:
    """Minimal markup matching the real card structure."""
    cards = "".join(
        f'''
        <div class="mdc-layout-grid__inner">
          <div class="card-date">0{i % 9 + 1}.09.26</div>
          <div class="scroll-area" data-id="{i}">
            <a class="black-link-b" href="https://www.jobs.bg/job/{i}" title="Junior Developer {i}">
              <div class="card-title"><span>Junior Developer {i}</span></div>
              <div class="card-info card__subtitle">Варна; Ниво Junior; Години опит от 0 до 2</div>
            </a>
          </div>
        </div>'''
        for i in ids
    )
    return f"<html><head><title>IT JOBS - {total} Обяви за работа за Варна</title></head><body>{cards}</body></html>"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        screenshots_dir=tmp_path / "shots",
        browser_profile_dir=tmp_path / "profile",
        max_pages_per_scan=5,
    )


class TestPagination:
    def test_collects_across_pages(self, settings) -> None:
        browser = PagingBrowser(
            {
                1: listing_with([1, 2, 3]),
                2: listing_with([4, 5, 6]),
                3: listing_with([7, 8]),
            }
        )
        jobs = JobsBgDiscovery(browser, settings).search(location="Varna", max_results=50)
        assert [j.source_job_id for j in jobs] == ["1", "2", "3", "4", "5", "6", "7", "8"]

    def test_uses_the_page_parameter(self, settings) -> None:
        browser = PagingBrowser({1: listing_with([1]), 2: listing_with([2])})
        JobsBgDiscovery(browser, settings).search(location="Varna", max_results=50)
        assert any("page=2" in url for url in browser.requested)

    def test_stops_when_a_page_repeats_results(self, settings) -> None:
        """Past the last page the site repeats page 1; that must end the crawl."""
        repeated = listing_with([1, 2])
        browser = PagingBrowser({1: repeated, 2: repeated, 3: repeated})
        jobs = JobsBgDiscovery(browser, settings).search(location="Varna", max_results=50)
        assert len(jobs) == 2
        assert len(browser.requested) == 2  # stopped after the repeat

    def test_stops_at_max_results(self, settings) -> None:
        browser = PagingBrowser({1: listing_with([1, 2, 3]), 2: listing_with([4, 5, 6])})
        jobs = JobsBgDiscovery(browser, settings).search(location="Varna", max_results=2)
        assert len(jobs) == 2

    def test_stops_on_an_empty_page(self, settings) -> None:
        browser = PagingBrowser({1: listing_with([1, 2])})
        jobs = JobsBgDiscovery(browser, settings).search(location="Varna", max_results=50)
        assert len(jobs) == 2

    def test_respects_the_page_budget(self, settings) -> None:
        settings.max_pages_per_scan = 2
        browser = PagingBrowser({n: listing_with([n * 10 + 1, n * 10 + 2]) for n in range(1, 6)})
        JobsBgDiscovery(browser, settings).search(location="Varna", max_results=100)
        assert len(browser.requested) == 2

    def test_challenge_propagates(self, settings) -> None:
        browser = PagingBrowser({1: listing_with([1])}, challenge_on="front_job_search")
        with pytest.raises(ChallengeDetectedError):
            JobsBgDiscovery(browser, settings).search(location="Varna")


class TestEnrichment:
    def test_fetches_up_to_the_limit(self, settings) -> None:
        detail = (FIXTURES / "detail_internal.html").read_text(encoding="utf-8")
        browser = PagingBrowser({1: listing_with([1, 2, 3])})
        discovery = JobsBgDiscovery(browser, settings)
        cards = discovery.search(location="Varna", max_results=10)

        browser.page.html = detail
        enriched = discovery.enrich(cards, limit=2)
        assert len(enriched) == 2

    def test_detail_failure_is_skipped_not_fatal(self, settings) -> None:
        class BrokenBrowser(PagingBrowser):
            def goto(self, url: str, page: Any = None, **kwargs: Any) -> FakePage:
                if "/job/" in url:
                    raise RuntimeError("timeout")
                return super().goto(url, page, **kwargs)

        browser = BrokenBrowser({1: listing_with([1, 2])})
        discovery = JobsBgDiscovery(browser, settings)
        cards = discovery.search(location="Varna", max_results=10)
        assert discovery.enrich(cards, limit=2) == {}


class TestMergeDetail:
    def _card(self) -> RawJob:
        return RawJob(
            source_job_id="1",
            source_url="https://www.jobs.bg/job/1",
            title="Junior Dev",
            company_name="Card Company",
            location_raw="Варна",
            tech_tags=["PHP"],
        )

    def test_detail_fields_win(self) -> None:
        detail = RawJob(
            source_job_id="1",
            source_url="https://www.jobs.bg/job/1",
            title="Junior Developer (Full Stack)",
            location_raw="Варна; бул. Сливница",
            description="A long description.",
            application_method=ApplicationMethod.JOBSBG_INTERNAL,
            tech_tags=["Laravel", "MySQL"],
        )
        merged = merge_detail(self._card(), detail)
        assert merged.description == "A long description."
        assert merged.application_method is ApplicationMethod.JOBSBG_INTERNAL
        assert merged.title == "Junior Developer (Full Stack)"  # longer title wins
        assert merged.tech_tags == ["PHP", "Laravel", "MySQL"]  # union, card order first

    def test_card_company_survives_a_detail_without_one(self) -> None:
        detail = RawJob(source_job_id="1", source_url="u", title="Junior Dev", company_name=None)
        assert merge_detail(self._card(), detail).company_name == "Card Company"

    def test_missing_detail_returns_the_card(self) -> None:
        card = self._card()
        assert merge_detail(card, None) is card

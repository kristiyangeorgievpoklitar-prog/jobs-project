"""Parsing of real Jobs.bg HTML, using saved fixtures."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from jobhunter.domain.enums import ApplicationMethod
from jobhunter.sources.jobsbg.parser import (
    clean_text,
    parse_card_info,
    parse_detail_page,
    parse_listing_page,
    parse_posted_date,
    parse_total_results,
    parse_years_experience,
)


class TestCleanText:
    def test_strips_material_icon_ligatures(self) -> None:
        assert clean_text("location_on София") == "София"

    def test_collapses_whitespace(self) -> None:
        assert clean_text("  a   b \n c ") == "a b c"

    def test_handles_none(self) -> None:
        assert clean_text(None) == ""


class TestParseCardInfo:
    def test_explicit_location_prefix(self) -> None:
        info = parse_card_info("Месторабота: София ; Ниво Mid-level; Години опит от 3 до 6")
        assert info["location"] == "София"
        assert info["level"] == "Mid-level"
        assert info["experience"] == "от 3 до 6"

    def test_bare_city_without_prefix(self) -> None:
        """In-city listings omit the 'Месторабота:' label."""
        info = parse_card_info("Варна; Ниво Senior-level; Години опит от 2 до 10+")
        assert info["location"] == "Варна"
        assert info["level"] == "Senior-level"

    def test_field_label_is_not_mistaken_for_a_city(self) -> None:
        info = parse_card_info("Ниво Entry-level; Години опит от 0 до 5")
        assert info["location"] is None

    def test_detects_home_office(self) -> None:
        info = parse_card_info("Варна; Възможност за работа от вкъщи; Ниво Mid-level")
        assert info["work_mode"] is not None

    def test_empty_input(self) -> None:
        assert parse_card_info("")["location"] is None


class TestParseYearsExperience:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("от 3 до 6", 3.0),
            ("от 4 до 10+", 4.0),
            ("Години опит от 1 до 3", 1.0),
            ("над 5 години", 5.0),
            ("3+ years", 3.0),
            ("0", 0.0),
        ],
    )
    def test_takes_the_lower_bound(self, text: str, expected: float) -> None:
        assert parse_years_experience(text) == expected

    def test_none_when_absent(self) -> None:
        assert parse_years_experience(None) is None
        assert parse_years_experience("no numbers here") is None


class TestParsePostedDate:
    def test_two_digit_year(self) -> None:
        assert parse_posted_date("02.09.26") == datetime(2026, 9, 2, tzinfo=UTC)

    def test_four_digit_year(self) -> None:
        assert parse_posted_date("02.09.2026") == datetime(2026, 9, 2, tzinfo=UTC)

    def test_ignores_trailing_reference_number(self) -> None:
        assert parse_posted_date("02.09.2026, Ref.No:51587723") == datetime(2026, 9, 2, tzinfo=UTC)

    def test_invalid_returns_none(self) -> None:
        assert parse_posted_date("not a date") is None
        assert parse_posted_date("45.99.26") is None

    def test_reads_the_words_the_site_prints_for_recent_listings(self) -> None:
        """Verified live: a busy IT search returns only ``днес`` and ``вчера``.

        Every listing published in the last two days is dated in words, so a
        parser that only understands ``DD.MM.YY`` cannot see today's jobs at
        all — which is the entire point of the daily scan.
        """
        reference = date(2026, 9, 9)
        assert parse_posted_date("днес", today=reference) == datetime(2026, 9, 9, tzinfo=UTC)
        assert parse_posted_date("вчера", today=reference) == datetime(2026, 9, 8, tzinfo=UTC)
        assert parse_posted_date("онзи ден", today=reference) == datetime(2026, 9, 7, tzinfo=UTC)

    def test_reads_the_english_sites_wording_too(self) -> None:
        reference = date(2026, 9, 9)
        assert parse_posted_date("Today", today=reference) == datetime(2026, 9, 9, tzinfo=UTC)
        assert parse_posted_date("Yesterday", today=reference) == datetime(2026, 9, 8, tzinfo=UTC)

    def test_a_relative_word_crossing_a_month_boundary(self) -> None:
        assert parse_posted_date("вчера", today=date(2026, 9, 1)) == datetime(
            2026, 8, 31, tzinfo=UTC
        )

    def test_a_numeric_date_ignores_the_reference(self) -> None:
        assert parse_posted_date("02.09.26", today=date(2020, 1, 1)) == datetime(
            2026, 9, 2, tzinfo=UTC
        )

    def test_the_reference_defaults_to_the_local_date(self) -> None:
        from datetime import datetime as real_datetime

        assert parse_posted_date("днес").date() == real_datetime.now().date()


class TestParseListingPage:
    def test_finds_every_card(self, listing_html: str) -> None:
        jobs = parse_listing_page(listing_html)
        assert len(jobs) == 20

    def test_extracts_core_fields(self, listing_html: str) -> None:
        job = parse_listing_page(listing_html)[0]
        assert job.source_job_id == "8596475"
        assert job.title == "Snowflake Data Engineer"
        assert job.company_name == "DXC Technology / DXC Bulgaria EOOD"
        assert job.location_raw == "София"
        assert job.level_raw == "Mid-level, Senior-level"
        assert job.posted_at_raw == "02.09.26"

    def test_urls_are_unique(self, listing_html: str) -> None:
        jobs = parse_listing_page(listing_html)
        assert len({j.source_url for j in jobs}) == len(jobs)

    def test_reads_total_from_title(self, listing_html: str) -> None:
        assert parse_total_results(listing_html) == 94

    def test_malformed_html_yields_nothing(self) -> None:
        assert parse_listing_page("<html><body><p>nope</p></body></html>") == []


class TestParseDetailPage:
    def test_internal_listing(self, detail_internal_html: str) -> None:
        job = parse_detail_page(detail_internal_html, source_url="https://www.jobs.bg/job/8598792")
        assert job is not None
        assert job.title == "Design Verification Engineer (PCIe, CXL, UVM, System Verilog)"
        assert job.company_name == "ПЛДА ЕООД"
        assert job.location_raw == "София"
        assert job.application_method is ApplicationMethod.JOBSBG_INTERNAL
        assert "Verilog" in job.tech_tags
        assert job.description and len(job.description) > 1000

    def test_external_listing_links_out(self, detail_external_html: str) -> None:
        job = parse_detail_page(detail_external_html, source_url="https://www.jobs.bg/job/8596475")
        assert job is not None
        assert job.application_method is ApplicationMethod.EXTERNAL_URL
        assert job.application_url and "jobs.bg" not in job.application_url
        assert job.languages_raw == ["Английски"]

    def test_description_keeps_line_structure(self, detail_internal_html: str) -> None:
        job = parse_detail_page(detail_internal_html, source_url="https://www.jobs.bg/job/8598792")
        assert job is not None and job.description is not None
        assert len(job.description.splitlines()) > 20

    def test_empty_html_returns_none(self) -> None:
        assert parse_detail_page("", source_url="https://www.jobs.bg/job/1") is None

"""Jobs.bg URL construction and canonicalisation."""

from __future__ import annotations

import pytest

from jobhunter.sources.jobsbg.urls import (
    CATEGORY_IT,
    build_search_url,
    extract_job_id,
    normalize_url,
    resolve_location_sid,
)


class TestLocationResolution:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Varna", 3),
            ("варна", 3),
            ("  VARNA  ", 3),
            ("Sofia", 1),
            ("София", 1),
            ("Пловдив", 2),
        ],
    )
    def test_known_cities(self, name: str, expected: int) -> None:
        assert resolve_location_sid(name) == expected

    def test_unknown_city_is_none_not_a_guess(self) -> None:
        assert resolve_location_sid("Atlantis") is None


class TestSearchUrl:
    def test_varna_it_matches_the_verified_url(self) -> None:
        url = build_search_url(location="Varna")
        assert url == (
            "https://www.jobs.bg/front_job_search.php?subm=1&categories[]=56&location_sid=3"
        )

    def test_entry_level_filter(self) -> None:
        assert "is_entry_level=1" in build_search_url(location="Varna", entry_level_only=True)

    def test_pagination_uses_page_param(self) -> None:
        assert "page=3" in build_search_url(location="Varna", page=3)

    def test_first_page_omits_page_param(self) -> None:
        assert "page=" not in build_search_url(location="Varna", page=1)

    def test_unknown_location_is_omitted(self) -> None:
        url = build_search_url(location="Atlantis")
        assert "location_sid" not in url
        assert f"categories[]={CATEGORY_IT}" in url


class TestJobId:
    def test_extracts_numeric_id(self) -> None:
        assert extract_job_id("https://www.jobs.bg/job/8598792") == "8598792"

    def test_returns_none_for_non_job_url(self) -> None:
        assert extract_job_id("https://www.jobs.bg/front_job_search.php") is None


class TestNormalizeUrl:
    def test_strips_tracking_and_www_and_fragment(self) -> None:
        assert (
            normalize_url("https://WWW.Jobs.BG/job/123/?utm_source=x&b=2&a=1#frag")
            == "https://jobs.bg/job/123?a=1&b=2"
        )

    def test_variants_collapse_to_one_key(self) -> None:
        variants = [
            "https://www.jobs.bg/job/8598792",
            "https://jobs.bg/job/8598792/",
            "https://www.jobs.bg/job/8598792?utm_campaign=abc",
        ]
        assert len({normalize_url(v) for v in variants}) == 1

    def test_empty_is_safe(self) -> None:
        assert normalize_url("") == ""

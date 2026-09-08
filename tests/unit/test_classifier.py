"""Location, industry and seniority classification."""

from __future__ import annotations

import pytest

from jobhunter.classify.classifier import (
    classify_it,
    classify_job,
    classify_location,
    classify_seniority,
    extract_requirements,
    years_to_seniority,
)
from jobhunter.domain.enums import Seniority
from tests.conftest import make_job


class TestYearsToSeniority:
    @pytest.mark.parametrize(
        ("years", "expected"),
        [
            (0, Seniority.ENTRY),
            (1, Seniority.JUNIOR),
            (2, Seniority.JUNIOR_MID),
            (3, Seniority.MID),
            (4, Seniority.MID_SENIOR),
            (7, Seniority.SENIOR),
        ],
    )
    def test_bands(self, years: float, expected: Seniority) -> None:
        assert years_to_seniority(years) is expected

    def test_none(self) -> None:
        assert years_to_seniority(None) is None


class TestSeniority:
    def test_site_level_range_uses_the_entry_bar(self) -> None:
        """'Mid-level, Senior-level' means you must be at least mid."""
        job = make_job(level_raw="Ниво Mid-level, Senior-level", experience_raw=None)
        seniority, confidence, _ = classify_seniority(job)
        assert seniority is Seniority.MID
        assert confidence >= 0.85

    def test_entry_level_site_tag(self) -> None:
        job = make_job(level_raw="Ниво Entry-level", experience_raw=None)
        assert classify_seniority(job)[0] is Seniority.ENTRY

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Junior PHP Developer", Seniority.JUNIOR),
            ("Младши програмист", Seniority.JUNIOR),
            ("Senior Java Engineer", Seniority.SENIOR),
            ("Старши разработчик", Seniority.SENIOR),
            ("Team Lead Backend", Seniority.LEAD),
            ("Стажант разработчик", Seniority.INTERNSHIP),
        ],
    )
    def test_title_wording_bg_and_en(self, title: str, expected: Seniority) -> None:
        job = make_job(title=title, level_raw=None, experience_raw=None, description=None)
        assert classify_seniority(job)[0] is expected

    def test_senior_title_is_not_softened_by_low_years(self) -> None:
        job = make_job(
            title="Senior Backend Developer",
            level_raw=None,
            experience_raw="Години опит от 1 до 2",
            description=None,
        )
        assert classify_seniority(job)[0].rank >= Seniority.SENIOR.rank

    def test_senior_title_overrides_a_lower_site_tag(self) -> None:
        """The conservative guard: never call a senior posting junior."""
        job = make_job(
            title="Senior Backend Developer",
            level_raw="Ниво Junior",
            experience_raw=None,
            description=None,
        )
        seniority, _, signals = classify_seniority(job)
        assert seniority.rank >= Seniority.SENIOR.rank
        assert any("conservative" in s for s in signals)

    def test_high_years_requirement_overrides_a_low_tag(self) -> None:
        job = make_job(
            title="Backend Developer",
            level_raw="Ниво Junior",
            experience_raw="Години опит от 8 до 12",
            description=None,
        )
        seniority, _, signals = classify_seniority(job)
        assert seniority.rank >= Seniority.MID_SENIOR.rank
        assert any("conservative" in s for s in signals)

    def test_unknown_when_no_signal(self) -> None:
        job = make_job(title="Specialist", level_raw=None, experience_raw=None, description=None)
        seniority, confidence, _ = classify_seniority(job)
        assert seniority is Seniority.UNKNOWN
        assert confidence == 0.0


class TestIndustry:
    def test_developer_role_is_it(self) -> None:
        job = make_job(title="Junior PHP Developer", tech_tags=["PHP", "Laravel"])
        is_it, confidence, _ = classify_it(job)
        assert is_it is True
        assert confidence > 0.5

    def test_plural_role_titles(self) -> None:
        job = make_job(title="Software Developers and Electronics Engineers", tech_tags=[])
        assert classify_it(job)[0] is True

    def test_non_it_role_rejected(self) -> None:
        job = make_job(title="Шофьор на камион", tech_tags=[], description="Шофиране на камион.")
        assert classify_it(job)[0] is False

    def test_customer_support_is_not_it(self) -> None:
        job = make_job(
            title="Customer Support Representative",
            tech_tags=[],
            description="Answer customer calls and emails.",
        )
        assert classify_it(job)[0] is not True


class TestLocation:
    def test_matching_city(self) -> None:
        job = make_job(location_raw="Варна")
        relevant, _ = classify_location(job, target_locations=["Varna"])
        assert relevant is True

    def test_other_city_rejected(self) -> None:
        job = make_job(location_raw="София")
        relevant, _ = classify_location(job, target_locations=["Varna"])
        assert relevant is False

    def test_remote_accepted_when_candidate_allows(self) -> None:
        job = make_job(location_raw="София", description="Fully remote work from anywhere.")
        relevant, _ = classify_location(job, target_locations=["Varna"], remote_ok=True)
        assert relevant is True

    def test_remote_rejected_when_candidate_refuses(self) -> None:
        job = make_job(location_raw="София", description="Fully remote work from anywhere.")
        relevant, _ = classify_location(job, target_locations=["Varna"], remote_ok=False)
        assert relevant is False

    def test_hybrid_needs_the_right_city(self) -> None:
        job = make_job(location_raw="София", description="Възможност за работа от вкъщи")
        assert classify_location(job, target_locations=["Varna"])[0] is False

    def test_unknown_city_is_undetermined(self) -> None:
        job = make_job(location_raw=None, description="A job.")
        assert classify_location(job, target_locations=["Varna"])[0] is None


class TestRequirements:
    def test_splits_required_and_preferred(self) -> None:
        description = """
About the role
Отговорности:
- Build features

Изисквания:
- 1 година опит с PHP
- Познания по SQL

Ще се счита за предимство:
- Опит с Docker
- Опит с Kubernetes
"""
        required, preferred = extract_requirements(description)
        assert any("PHP" in r for r in required)
        assert any("Docker" in p for p in preferred)
        assert not any("Docker" in r for r in required)

    def test_empty_description(self) -> None:
        assert extract_requirements(None) == ([], [])


class TestClassifyJob:
    def test_combines_everything(self) -> None:
        job = make_job(title="Junior PHP Developer", location_raw="Варна")
        result = classify_job(job, target_locations=["Varna"])
        assert result.is_it is True
        assert result.location_relevant is True
        assert result.seniority in (Seniority.JUNIOR, Seniority.JUNIOR_MID)

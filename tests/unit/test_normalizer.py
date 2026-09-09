"""Normalization, fingerprinting and salary parsing."""

from __future__ import annotations

import pytest

from jobhunter.domain.enums import EmploymentType, Language, WorkMode
from jobhunter.domain.schemas import RawJob
from jobhunter.normalize.normalizer import (
    compute_fingerprint,
    content_fingerprint,
    detect_employment_type,
    detect_language,
    detect_work_mode,
    extract_city,
    normalize_company,
    normalize_job,
    normalize_text,
    parse_salary,
)


class TestNormalizeText:
    def test_lowercases_and_strips_punctuation(self) -> None:
        assert normalize_text("Junior PHP Developer!") == "junior php developer"

    def test_handles_none_and_empty(self) -> None:
        assert normalize_text(None) == ""
        assert normalize_text("   ") == ""


class TestNormalizeCompany:
    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("Example EOOD", "Example ЕООД"),
            ("Acme Ltd.", "Acme"),
            ("СиСофт АД", "СиСофт"),
        ],
    )
    def test_legal_suffixes_collapse(self, a: str, b: str) -> None:
        assert normalize_company(a) == normalize_company(b)

    def test_distinct_companies_stay_distinct(self) -> None:
        assert normalize_company("Acme Ltd") != normalize_company("Globex Ltd")


class TestExtractCity:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Варна", "Varna"),
            ("Varna", "Varna"),
            ("Варна; бул. Сливница 12", "Varna"),
            ("София; Бизнес парк, сграда 8", "Sofia"),
            ("Пловдив", "Plovdiv"),
        ],
    )
    def test_canonicalises(self, raw: str, expected: str) -> None:
        assert extract_city(raw) == expected

    def test_none_input(self) -> None:
        assert extract_city(None) is None


class TestDetectLanguage:
    def test_english(self) -> None:
        assert detect_language("Junior PHP Developer") is Language.EN

    def test_bulgarian(self) -> None:
        assert detect_language("Младши програмист") is Language.BG

    def test_unknown_when_empty(self) -> None:
        assert detect_language(None, "") is Language.UNKNOWN


class TestWorkModeAndEmployment:
    def test_home_office_is_hybrid(self) -> None:
        assert detect_work_mode("Възможност за работа от вкъщи") is WorkMode.HYBRID

    def test_fully_remote(self) -> None:
        assert detect_work_mode("Fully remote position") is WorkMode.REMOTE

    def test_permanent_full_time(self) -> None:
        assert detect_employment_type("Постоянна работа") is EmploymentType.FULL_TIME

    def test_internship(self) -> None:
        assert detect_employment_type("Стаж") is EmploymentType.INTERNSHIP

    def test_unknown(self) -> None:
        assert detect_employment_type(None) is EmploymentType.UNKNOWN


class TestParseSalary:
    @pytest.mark.parametrize(
        ("text", "minimum", "maximum", "currency"),
        [
            ("от 2000 до 3000 лв.", 2000.0, 3000.0, "BGN"),
            ("2500 EUR месечно", 2500.0, None, "EUR"),
            ("до 4000 лв", None, 4000.0, "BGN"),
            ("1 800 лв.", 1800.0, None, "BGN"),
        ],
    )
    def test_ranges_and_currency(self, text, minimum, maximum, currency) -> None:
        salary = parse_salary(text)
        assert salary.minimum == minimum
        assert salary.maximum == maximum
        assert salary.currency == currency

    def test_period_detection(self) -> None:
        assert parse_salary("2500 EUR месечно").period == "month"
        assert parse_salary("45000 EUR годишно").period == "year"

    def test_absent_salary(self) -> None:
        assert parse_salary(None).is_present is False


class TestFingerprint:
    def test_stable_for_same_source_id(self) -> None:
        args = {
            "source": "jobs.bg",
            "normalized_url": "https://jobs.bg/job/1",
            "company": "A",
            "title": "B",
        }
        assert compute_fingerprint(source_job_id="123", **args) == compute_fingerprint(
            source_job_id="123", **args
        )

    def test_source_id_wins_over_url_variation(self) -> None:
        a = compute_fingerprint(
            source="jobs.bg",
            source_job_id="123",
            normalized_url="https://jobs.bg/job/1",
            company="A",
            title="B",
        )
        b = compute_fingerprint(
            source="jobs.bg",
            source_job_id="123",
            normalized_url="https://jobs.bg/job/1?x=2",
            company="A",
            title="B",
        )
        assert a == b

    def test_falls_back_to_url_then_company_title(self) -> None:
        by_url = compute_fingerprint(
            source="jobs.bg",
            source_job_id=None,
            normalized_url="https://jobs.bg/job/9",
            company=None,
            title="T",
        )
        by_content = compute_fingerprint(
            source="jobs.bg",
            source_job_id=None,
            normalized_url="",
            company="Acme",
            title="T",
        )
        assert by_url != by_content
        assert len(by_url) == 32

    def test_content_fingerprint_ignores_legal_suffix(self) -> None:
        assert content_fingerprint("Acme EOOD", "Junior Dev") == content_fingerprint(
            "Acme ЕООД", "junior dev"
        )


class TestNormalizeJob:
    def test_end_to_end(self) -> None:
        raw = RawJob(
            source_job_id="777",
            source_url="https://www.jobs.bg/job/777",
            title="  Junior PHP Developer  ",
            company_name="Example EOOD",
            location_raw="Варна",
            description="Some description",
            level_raw="Ниво Junior",
            experience_raw="Години опит от 1 до 3",
            posted_at_raw="02.09.26",
            tech_tags=["PHP", "Laravel", "PHP"],
        )
        job = normalize_job(raw)
        assert job.title == "Junior PHP Developer"
        assert job.city == "Varna"
        assert job.years_experience_required == 1.0
        assert job.tech_keywords == ["PHP", "Laravel"]  # de-duplicated, order kept
        assert job.posted_at is not None
        assert job.normalized_url == "https://jobs.bg/job/777"


class TestEmploymentTypeWordBoundaries:
    """Substring matching mislabelled multinationals as internships."""

    def test_international_is_not_an_internship(self) -> None:
        assert (
            detect_employment_type(None, "Work with international clients from Silicon Valley")
            is EmploymentType.UNKNOWN
        )

    def test_a_real_internship_is_still_detected(self) -> None:
        assert (
            detect_employment_type(None, "Paid internship programme") is EmploymentType.INTERNSHIP
        )
        assert detect_employment_type(None, "Стаж за студенти") is EmploymentType.INTERNSHIP

    def test_other_contract_types_survive_the_change(self) -> None:
        assert detect_employment_type(None, "part-time role") is EmploymentType.PART_TIME
        assert detect_employment_type(None, "граждански договор") is EmploymentType.CONTRACT
        assert detect_employment_type(None, "Пълно работно време") is EmploymentType.FULL_TIME

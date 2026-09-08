"""Getting the posting text and the candidate text right.

Both were silently wrong before: the description that reached the matcher was
navigation chrome rather than the job, and the candidate was a bag of skill
tokens with the experience stripped out. Everything downstream depends on these,
so they are pinned here.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from jobhunter.domain.schemas import CandidateSnapshot, RawJob
from jobhunter.matching.job_context import (
    has_usable_description,
    job_content_hash,
    render_job,
)
from jobhunter.normalize.normalizer import detect_work_mode, extract_city, normalize_job
from jobhunter.profile.context import (
    candidate_fingerprint,
    cv_has_placeholders,
    extract_cv_sections,
    render_candidate,
)
from jobhunter.sources.jobsbg.parser import extract_description

# The shape of a Jobs.bg detail page that renders the body inline.
INLINE_PAGE = """
<div id="jobViewContent">
  <div class="center-content">Получавайте новите IT Обяви Абонирай ме</div>
  <div>
    <div class="margin-medium job-view-left-column">
      <div class="bg-white">
        <table><tr><td>
          Junior PHP Developer<br>
          Requirements:<br>
          - Good knowledge of PHP and Laravel<br>
          - Basic SQL<br>
          Advantages: Docker experience is a plus.
        </td></tr></table>
        <div class="no-print apply-actions">Kандидатствай Запази email Препрати</div>
      </div>
    </div>
    <div class="view-extra">Ниво Entry-level 01.09.2026</div>
  </div>
</div>
"""


def content_node(html: str):
    return BeautifulSoup(html, "lxml").select_one("#jobViewContent")


# ------------------------------------------------------- description recovery


def test_the_iframe_body_is_preferred_when_present():
    """Most listings render the posting in a sandboxed iframe, not the page DOM."""
    text = extract_description(content_node(INLINE_PAGE), "Real body from the iframe.")
    assert text == "Real body from the iframe."


def test_an_inline_body_is_picked_out_of_the_page_furniture():
    text = extract_description(content_node(INLINE_PAGE), None)
    assert "Good knowledge of PHP and Laravel" in text
    assert "Абонирай ме" not in text, "subscription chrome must not reach the matcher"
    assert "Kандидатствай" not in text, "apply buttons are not part of the posting"


def test_an_empty_iframe_body_falls_back_rather_than_losing_the_posting():
    text = extract_description(content_node(INLINE_PAGE), "   ")
    assert "PHP and Laravel" in text


# --------------------------------------------------------------- job rendering


def make_job(**overrides):
    payload = {
        "source_url": "https://www.jobs.bg/job/1",
        "title": "Junior PHP Developer",
        "company_name": "Acme",
        "location_raw": "Варна",
        "description": "Requirements: PHP, Laravel. " * 40,
    }
    payload.update(overrides)
    return normalize_job(RawJob(**payload))


def test_a_rendered_job_marks_site_metadata_as_the_site_s_claim():
    """The model is told what the site asserts so it can disagree with it."""
    rendered = render_job(make_job())
    assert "Location stated by the site" in rendered
    assert "Junior PHP Developer" in rendered


def test_a_missing_description_is_stated_outright_not_left_blank():
    rendered = render_job(make_job(description=None))
    assert "NOT AVAILABLE" in rendered


def test_the_content_hash_ignores_truncation_but_tracks_real_edits():
    job = make_job()
    assert job_content_hash(job) == job_content_hash(make_job())
    edited = make_job(description=job.description + " Docker required.")
    assert job_content_hash(job) != job_content_hash(edited)


def test_a_card_only_listing_is_not_treated_as_readable():
    assert not has_usable_description(make_job(description="Varna. Full time."))
    assert has_usable_description(make_job())


# --------------------------------------------------------- normalisation fixes


def test_a_remote_label_in_the_location_field_is_not_a_city():
    """The site puts "Дистанционна работа" where a city name goes."""
    assert extract_city("public Дистанционна работа") is None


def test_a_city_is_still_found_alongside_a_remote_label():
    assert extract_city("Дистанционна работа - Варна") == "Varna"


def test_an_address_after_the_city_is_ignored():
    assert extract_city("Варна; бул. Генерал Колев 113, ет.5") == "Varna"


def test_work_mode_comes_from_the_site_field_not_the_posting_body():
    """A perk mentioned in the body previously turned office jobs into remote ones."""
    job = make_job(
        description="We offer flexible hours and дистанционна работа on Fridays. " * 20,
        work_mode_raw=None,
    )
    assert job.work_mode.value == "onsite"


def test_the_site_field_still_drives_the_work_mode():
    assert detect_work_mode("Дистанционна работа", "Варна").value == "remote"
    assert detect_work_mode("Възможност за работа от вкъщи", "Варна").value == "hybrid"


# ------------------------------------------------------------- candidate text


CV_TEXT = """Kristiyan Poklitar
SUMMARY
Computer Science student.
EDUCATION
University of Economics - Varna
2025 - Present
EXPERIENCE
Summer Intern July 2026 - Present
NULA Varna, Bulgaria
- Contributed to a Laravel-based accounting application.
- Migrated UI components using Tailwind CSS and Livewire.
PROJECTS
Bus Radar - live transport tracker
- Leaflet map with live API data.
LANGUAGES
- Bulgarian native
"""

TEMPLATE_CV = """CV - [Име Фамилия]
[Град, България] | [Телефон]
ОБРАЗОВАНИЕ
[Име на университет] - [Град]
"""


def test_the_cv_experience_and_projects_reach_the_matcher():
    sections = extract_cv_sections(CV_TEXT)
    assert "Laravel-based accounting application" in sections["experience"]
    assert "Bus Radar" in sections["projects"]


def test_a_rendered_candidate_carries_evidence_not_just_tokens():
    candidate = CandidateSnapshot(
        location="Varna",
        years_experience=0.5,
        skills=["php"],
        frameworks=["laravel"],
    )
    rendered = render_candidate(candidate, cv_text=CV_TEXT)
    assert "Laravel-based accounting application" in rendered
    assert "Bus Radar" in rendered
    assert "Commercial experience: 0.5 years" in rendered


def test_an_unfilled_cv_template_is_detected():
    """CV_IT_Junior_BG.pdf ships as a template whose name is "[Име Фамилия]"."""
    found = cv_has_placeholders(TEMPLATE_CV)
    assert found, "a template must never be attachable to a real application"


def test_a_real_cv_is_not_flagged():
    assert cv_has_placeholders(CV_TEXT) == []


def test_the_candidate_fingerprint_tracks_the_cv_as_well_as_the_profile():
    candidate = CandidateSnapshot(location="Varna", skills=["php"])
    assert candidate_fingerprint(candidate, CV_TEXT) != candidate_fingerprint(candidate, None)
    assert candidate_fingerprint(candidate, CV_TEXT) == candidate_fingerprint(candidate, CV_TEXT)

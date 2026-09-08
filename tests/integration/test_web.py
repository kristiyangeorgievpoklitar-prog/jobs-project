"""Dashboard routes."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from jobhunter.context import AppContext
from jobhunter.db.models import Application, Job, Notification
from jobhunter.domain.enums import JobState, Recommendation
from jobhunter.domain.schemas import MatchResult
from jobhunter.pipeline.repository import record_match, upsert_job
from jobhunter.profile.profile_store import update_profile
from jobhunter.web.app import create_app
from tests.conftest import make_job


@pytest.fixture
def client(settings) -> TestClient:
    context = AppContext(settings, configure_logs=False)
    context.db.create_all()
    with context.session() as session:
        update_profile(
            session,
            {
                "full_name": "Test Candidate",
                "location": "Varna",
                "preferred_locations": ["Varna"],
                "skills": ["php"],
            },
        )
        job, _ = upsert_job(session, make_job(source_job_id="1", title="Junior PHP Developer"))
        record_match(
            session,
            job,
            MatchResult(
                score=92,
                recommendation=Recommendation.APPLY,
                strengths=["PHP", "Laravel"],
                missing_skills=["Docker"],
            ),
        )
        job.state = JobState.REVIEW
        low, _ = upsert_job(session, make_job(source_job_id="2", title="Senior Java Architect"))
        record_match(session, low, MatchResult(score=20, recommendation=Recommendation.SKIP))
        low.state = JobState.SKIPPED
    return TestClient(create_app(context))


class TestPages:
    @pytest.mark.parametrize(
        "path",
        ["/", "/overview", "/jobs", "/applications", "/notifications", "/settings", "/health"],
    )
    def test_pages_render(self, client: TestClient, path: str) -> None:
        response = client.get(path)
        assert response.status_code == 200

    def test_home_page_leads_with_the_decision(self, client: TestClient) -> None:
        """The home page answers "what should I apply to", not "what is the score"."""
        body = client.get("/").text
        assert "What should I apply to today?" in body
        assert "worth reviewing" in body

    def test_statistics_view_still_shows_the_numbers(self, client: TestClient) -> None:
        body = client.get("/overview").text
        assert "Junior PHP Developer" in body
        assert "92" in body

    def test_job_detail_shows_analysis(self, client: TestClient) -> None:
        body = client.get("/jobs/1").text
        assert "Junior PHP Developer" in body
        assert "Docker" in body  # missing skill
        assert "Laravel" in body  # strength

    def test_missing_job_is_404(self, client: TestClient) -> None:
        assert client.get("/jobs/999999").status_code == 404

    def test_static_css_served(self, client: TestClient) -> None:
        response = client.get("/static/style.css")
        assert response.status_code == 200
        assert "JobHunter" in response.text


class TestOverviewStats:
    def test_manual_step_is_not_counted_as_a_failure(self, client: TestClient) -> None:
        """A CAPTCHA-blocked application is waiting for a person, not failed."""
        from jobhunter.web.deps import overview_stats

        context = client.app.state.context
        with context.session() as session:
            session.add(
                Application(job_id=1, success=False, is_manual=True, state=JobState.BLOCKED)
            )
        with context.session() as session:
            stats = overview_stats(session)
        assert stats["applications_failed"] == 0
        assert stats["applications_awaiting_you"] == 1

    def test_genuine_failure_is_counted(self, client: TestClient) -> None:
        from jobhunter.web.deps import overview_stats

        context = client.app.state.context
        with context.session() as session:
            session.add(
                Application(job_id=1, success=False, is_manual=False, state=JobState.FAILED)
            )
        with context.session() as session:
            stats = overview_stats(session)
        assert stats["applications_failed"] == 1


class TestFilters:
    def test_min_score_filter(self, client: TestClient) -> None:
        body = client.get("/jobs?min_score=75").text
        assert "Junior PHP Developer" in body
        assert "Senior Java Architect" not in body

    def test_state_filter(self, client: TestClient) -> None:
        body = client.get("/jobs?state=skipped").text
        assert "Senior Java Architect" in body
        assert "Junior PHP Developer" not in body

    def test_search_filter(self, client: TestClient) -> None:
        assert "Junior PHP Developer" in client.get("/jobs?search=php").text

    def test_sort_by_date(self, client: TestClient) -> None:
        assert client.get("/jobs?order=date").status_code == 200


class TestActions:
    def test_approve_moves_state(self, client: TestClient) -> None:
        response = client.post("/actions/jobs/1/approve", follow_redirects=False)
        assert response.status_code == 303
        context = client.app.state.context
        with context.session() as session:
            assert session.get(Job, 1).state is JobState.APPROVED

    def test_skip_moves_state(self, client: TestClient) -> None:
        client.post("/actions/jobs/1/skip", follow_redirects=False)
        context = client.app.state.context
        with context.session() as session:
            assert session.get(Job, 1).state is JobState.SKIPPED

    def test_rescore_adds_a_match(self, client: TestClient) -> None:
        context = client.app.state.context
        client.post("/actions/jobs/1/rescore", follow_redirects=False)
        with context.session() as session:
            job = session.get(Job, 1)
            assert len(job.matches) >= 2

    def test_submit_is_refused_while_auto_apply_is_off(self, client: TestClient) -> None:
        context = client.app.state.context
        assert context.settings.auto_apply is False
        response = client.post("/actions/jobs/1/submit", follow_redirects=False)
        assert response.status_code == 303
        assert "auto-apply" in response.headers["location"].lower().replace("%20", " ")
        with context.session() as session:
            assert session.scalar(select(Application)) is None

    def test_mark_notifications_read(self, client: TestClient) -> None:
        context = client.app.state.context
        with context.session() as session:
            session.add(Notification(kind="scan_completed", title="t"))
        client.post("/actions/notifications/read-all", follow_redirects=False)
        with context.session() as session:
            assert all(n.read_at is not None for n in session.scalars(select(Notification)).all())


class TestSettingsForms:
    def test_saves_profile(self, client: TestClient) -> None:
        response = client.post(
            "/settings/profile",
            data={
                "full_name": "New Name",
                "location": "Varna",
                "years_experience": "2",
                "desired_seniority": "junior",
                "skills": "php, python",
                "frameworks": "laravel",
                "databases": "",
                "tools": "",
                "soft_skills": "",
                "preferred_locations": "Varna, Sofia",
                "languages": "[]",
                "email": "",
                "phone": "",
                "summary": "",
                "github_url": "",
                "linkedin_url": "",
                "portfolio_url": "",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        context = client.app.state.context
        from jobhunter.profile.profile_store import get_active_profile

        with context.session() as session:
            profile = get_active_profile(session)
            assert profile.full_name == "New Name"
            assert profile.skills == ["php", "python"]
            assert profile.preferred_locations == ["Varna", "Sofia"]

    def test_rejects_malformed_languages_json(self, client: TestClient) -> None:
        response = client.post(
            "/settings/profile",
            data={
                "full_name": "X",
                "location": "Varna",
                "years_experience": "1",
                "desired_seniority": "junior",
                "languages": "{not json",
                "skills": "",
                "frameworks": "",
                "databases": "",
                "tools": "",
                "soft_skills": "",
                "preferred_locations": "",
                "email": "",
                "phone": "",
                "summary": "",
                "github_url": "",
                "linkedin_url": "",
                "portfolio_url": "",
            },
            follow_redirects=False,
        )
        assert "error" in response.headers["location"]

    def test_saves_runtime_settings(self, client: TestClient) -> None:
        context = client.app.state.context
        client.post(
            "/settings/runtime",
            data={
                "review_threshold": "60",
                "auto_apply_threshold": "85",
                "search_location": "Varna",
                "max_seniority": "mid",
                "max_pages_per_scan": "3",
                "max_jobs_per_scan": "50",
                "request_delay_seconds": "3.0",
                "scan_interval_hours": "24",
                "scan_at_hour": "9",
                "cover_letter_max_words": "180",
                "max_auto_applications_per_run": "5",
                "ai_provider": "auto",
            },
            follow_redirects=False,
        )
        assert context.settings.review_threshold == 60

    def test_unchecked_boxes_become_false(self, client: TestClient) -> None:
        context = client.app.state.context
        client.post("/settings/runtime", data={"review_threshold": "75"}, follow_redirects=False)
        assert context.settings.auto_apply is False

    def test_rejects_missing_cv_path(self, client: TestClient) -> None:
        response = client.post(
            "/settings/cv", data={"cv_path": "/nope/missing.pdf"}, follow_redirects=False
        )
        assert "error" in response.headers["location"]

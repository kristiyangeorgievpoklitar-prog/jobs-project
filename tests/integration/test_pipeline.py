"""End-to-end scan pipeline with the browser layer replaced by fixtures."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from jobhunter.browser.challenge import ChallengeDetectedError, ChallengeResult, ChallengeType
from jobhunter.context import AppContext
from jobhunter.db.models import AutomationRun, ErrorRecord, Job, Notification
from jobhunter.domain.enums import JobState, RunStatus
from jobhunter.domain.schemas import RawJob
from jobhunter.pipeline import runner as runner_module
from jobhunter.pipeline.runner import ScanOptions, ScanPipeline
from jobhunter.profile.profile_store import update_profile


class FakeBrowser:
    """Stands in for BrowserManager; never touches the network."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    def start(self) -> FakeBrowser:
        return self

    def stop(self) -> None:
        return None

    def __enter__(self) -> FakeBrowser:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeDiscovery:
    """Returns canned listings and details."""

    def __init__(self, cards: list[RawJob], details: dict[str, RawJob] | None = None) -> None:
        self.cards = cards
        self.details = details or {}
        self.enrich_calls: list[int] = []

    def __call__(self, browser: Any, settings: Any) -> FakeDiscovery:
        return self

    def search(self, **kwargs: Any) -> list[RawJob]:
        return self.cards[: kwargs.get("max_results", len(self.cards))]

    def enrich(self, jobs: list[RawJob], *, limit: int, page: Any = None) -> dict[str, RawJob]:
        self.enrich_calls.append(limit)
        return {
            job.source_url: self.details[job.source_url]
            for job in jobs[:limit]
            if job.source_url in self.details
        }


def card(job_id: str, title: str, location: str = "Варна", **extra: Any) -> RawJob:
    return RawJob(
        source_job_id=job_id,
        source_url=f"https://www.jobs.bg/job/{job_id}",
        title=title,
        company_name=extra.pop("company", "Example EOOD"),
        location_raw=location,
        level_raw=extra.pop("level", "Ниво Junior"),
        experience_raw=extra.pop("experience", "Години опит от 0 до 2"),
        tech_tags=extra.pop("tech", ["PHP", "Laravel"]),
        **extra,
    )


def detail_for(job_id: str, title: str, **extra: Any) -> RawJob:
    return card(
        job_id,
        title,
        description=extra.pop("description", "Изисквания:\n- PHP\n- Laravel\n- MySQL\n" * 12),
        **extra,
    )


@pytest.fixture
def context(settings, monkeypatch) -> AppContext:
    ctx = AppContext(settings, configure_logs=False)
    ctx.db.create_all()
    with ctx.session() as session:
        update_profile(
            session,
            {
                "full_name": "Test Candidate",
                "location": "Varna",
                "years_experience": 1.0,
                "preferred_locations": ["Varna"],
                "skills": ["php", "javascript", "sql"],
                "frameworks": ["laravel"],
                "databases": ["mysql"],
            },
        )
    monkeypatch.setattr(runner_module, "BrowserManager", FakeBrowser)
    return ctx


def install_discovery(monkeypatch, discovery: FakeDiscovery) -> None:
    monkeypatch.setattr(runner_module, "JobsBgDiscovery", discovery)


class TestScanPipeline:
    def test_discovers_classifies_and_scores(self, context, monkeypatch) -> None:
        cards = [
            card("1", "Junior PHP Developer"),
            card("2", "Senior Java Architect", level="Ниво Senior-level"),
        ]
        details = {c.source_url: detail_for(c.source_job_id, c.title) for c in cards}
        install_discovery(monkeypatch, FakeDiscovery(cards, details))

        stats = ScanPipeline(context).run(ScanOptions())

        assert stats.jobs_seen == 2
        assert stats.jobs_new == 2
        assert stats.jobs_matched == 2
        assert stats.errors_count == 0

        with context.session() as session:
            jobs = session.scalars(select(Job)).all()
            assert len(jobs) == 2
            assert all(job.latest_match is not None for job in jobs)
            assert all(job.state is not JobState.DISCOVERED for job in jobs)

    def test_is_idempotent_across_runs(self, context, monkeypatch) -> None:
        cards = [card("1", "Junior PHP Developer")]
        install_discovery(monkeypatch, FakeDiscovery(cards, {}))
        pipeline = ScanPipeline(context)

        first = pipeline.run(ScanOptions())
        second = pipeline.run(ScanOptions())

        assert first.jobs_new == 1
        assert second.jobs_new == 0
        assert second.jobs_updated == 1
        with context.session() as session:
            assert len(session.scalars(select(Job)).all()) == 1

    def test_senior_role_is_skipped(self, context, monkeypatch) -> None:
        cards = [
            card("9", "Senior Java Architect", level="Ниво Senior-level", experience="от 8 до 12")
        ]
        install_discovery(monkeypatch, FakeDiscovery(cards, {}))
        ScanPipeline(context).run(ScanOptions())
        with context.session() as session:
            job = session.scalar(select(Job))
            assert job.state is JobState.SKIPPED

    def test_review_mode_never_auto_approves(self, context, monkeypatch) -> None:
        """With auto-apply off, even a perfect match waits in REVIEW."""
        assert context.settings.auto_apply is False
        cards = [card("1", "Junior PHP Developer")]
        details = {cards[0].source_url: detail_for("1", "Junior PHP Developer")}
        install_discovery(monkeypatch, FakeDiscovery(cards, details))

        ScanPipeline(context).run(ScanOptions())
        with context.session() as session:
            job = session.scalar(select(Job))
            assert job.state is not JobState.APPROVED
            assert job.state in (JobState.REVIEW, JobState.SKIPPED)

    def test_records_a_run(self, context, monkeypatch) -> None:
        install_discovery(monkeypatch, FakeDiscovery([card("1", "Junior PHP Developer")], {}))
        ScanPipeline(context).run(ScanOptions())
        with context.session() as session:
            run = session.scalar(select(AutomationRun))
            assert run.status is RunStatus.SUCCEEDED
            assert run.finished_at is not None
            assert run.duration_seconds is not None

    def test_enrichment_is_capped_and_prioritised(self, context, monkeypatch) -> None:
        cards = [card(str(i), f"Junior PHP Developer {i}") for i in range(1, 11)]
        discovery = FakeDiscovery(cards, {})
        install_discovery(monkeypatch, discovery)
        ScanPipeline(context).run(ScanOptions(enrich_limit=3))
        assert discovery.enrich_calls == [3]

    def test_non_it_jobs_are_not_enriched(self, context, monkeypatch) -> None:
        cards = [card("1", "Шофьор на камион", tech=[]), card("2", "Junior PHP Developer")]
        discovery = FakeDiscovery(cards, {})
        install_discovery(monkeypatch, discovery)
        ScanPipeline(context).run(ScanOptions(enrich_limit=10))
        # Only the IT role survives the shortlist.
        assert discovery.enrich_calls == [1]

    def test_notifies_on_a_strong_match(self, context, monkeypatch) -> None:
        cards = [card("1", "Junior PHP Developer")]
        details = {cards[0].source_url: detail_for("1", "Junior PHP Developer")}
        install_discovery(monkeypatch, FakeDiscovery(cards, details))
        context.settings.notify_dashboard = True
        context.__dict__.pop("notifier", None)

        ScanPipeline(context).run(ScanOptions())
        with context.session() as session:
            kinds = {n.kind for n in session.scalars(select(Notification)).all()}
            assert "high_match_job" in {str(k) for k in kinds}


class TestFailureHandling:
    def test_challenge_marks_the_run_blocked(self, context, monkeypatch) -> None:
        class BlockingDiscovery(FakeDiscovery):
            def search(self, **kwargs: Any) -> list[RawJob]:
                raise ChallengeDetectedError(
                    ChallengeResult(ChallengeType.CLOUDFLARE_INTERSTITIAL, "blocked"),
                    "https://www.jobs.bg/",
                )

        install_discovery(monkeypatch, BlockingDiscovery([], {}))
        stats = ScanPipeline(context).run(ScanOptions())

        assert stats.blocked is True
        with context.session() as session:
            run = session.scalar(select(AutomationRun))
            assert run.status is RunStatus.BLOCKED
            assert session.scalar(select(ErrorRecord)) is not None

    def test_unexpected_error_marks_the_run_failed(self, context, monkeypatch) -> None:
        class BrokenDiscovery(FakeDiscovery):
            def search(self, **kwargs: Any) -> list[RawJob]:
                raise RuntimeError("network exploded")

        install_discovery(monkeypatch, BrokenDiscovery([], {}))
        stats = ScanPipeline(context).run(ScanOptions())

        assert stats.errors_count >= 1
        with context.session() as session:
            assert session.scalar(select(AutomationRun)).status is RunStatus.FAILED

    def test_one_bad_job_does_not_stop_the_run(self, context, monkeypatch) -> None:
        cards = [card("1", "Junior PHP Developer"), card("2", "Junior Python Developer")]
        install_discovery(monkeypatch, FakeDiscovery(cards, {}))

        original = ScanPipeline._process_job
        calls = {"n": 0}

        def flaky(self, raw, candidate, stats, run_id):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("boom")
            return original(self, raw, candidate, stats, run_id)

        monkeypatch.setattr(ScanPipeline, "_process_job", flaky)
        stats = ScanPipeline(context).run(ScanOptions())

        assert stats.errors_count == 1
        assert stats.jobs_new == 1  # the second job still landed
        with context.session() as session:
            assert session.scalar(select(AutomationRun)).status is RunStatus.SUCCEEDED

"""The daily "what appeared today" pass, end to end with a fake site.

The behaviour under test is the bound, not the matching: only listings printed
with today's date are read, only listings this database has never seen are
announced, and running the scan twice does neither of those twice.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from jobhunter.ai.local_model import LocalModelConfig, LocalModelProvider
from jobhunter.context import AppContext
from jobhunter.db.models import Job, Notification
from jobhunter.domain.evaluation import Decision
from jobhunter.domain.schemas import RawJob
from jobhunter.matching.evaluator import JobEvaluator
from jobhunter.pipeline import runner as runner_module
from jobhunter.profile.profile_store import update_profile
from jobhunter.sources.jobsbg.discovery import published_on
from jobhunter.today import build_today_jobs, run_today_scan

TODAY = date(2026, 9, 9)
YESTERDAY = TODAY - timedelta(days=1)

LONG_DESCRIPTION = (
    "Изисквания:\n- PHP\n- Laravel\n- MySQL\n- Git\nОтговорности: разработка и "
    "поддръжка на уеб приложения, работа в екип, ревю на код.\n" * 6
)


class FakeBrowser:
    """Stands in for BrowserManager; never touches the network."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    def __enter__(self) -> FakeBrowser:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeDiscovery:
    """Canned listings, filtered by publication date exactly as the real one is."""

    def __init__(self, cards: list[RawJob], details: dict[str, RawJob] | None = None) -> None:
        self.cards = cards
        self.details = details or {}
        self.searched_dates: list[date | None] = []

    def __call__(self, browser: Any, settings: Any) -> FakeDiscovery:
        return self

    def search(self, **kwargs: Any) -> list[RawJob]:
        posted_on = kwargs.get("posted_on")
        self.searched_dates.append(posted_on)
        cards = (
            [card for card in self.cards if published_on(card, posted_on)]
            if posted_on is not None
            else self.cards
        )
        return cards[: kwargs.get("max_results", len(cards))]

    def enrich(self, jobs: list[RawJob], *, limit: int, page: Any = None) -> dict[str, RawJob]:
        return {
            job.source_url: self.details[job.source_url]
            for job in jobs[:limit]
            if job.source_url in self.details
        }


def card(job_id: str, title: str, *, posted: date = TODAY, **extra: Any) -> RawJob:
    return RawJob(
        source_job_id=job_id,
        source_url=f"https://www.jobs.bg/job/{job_id}",
        title=title,
        company_name=extra.pop("company", "Example EOOD"),
        location_raw=extra.pop("location", "Варна"),
        level_raw=extra.pop("level", "Ниво Junior"),
        experience_raw=extra.pop("experience", "Години опит от 0 до 2"),
        tech_tags=extra.pop("tech", ["PHP", "Laravel"]),
        posted_at_raw=posted.strftime("%d.%m.%y"),
        **extra,
    )


def with_detail(one: RawJob) -> RawJob:
    return one.model_copy(update={"description": LONG_DESCRIPTION})


def evaluation_json(decision: str, *, confidence: float = 0.85) -> str:
    return json.dumps(
        {
            "is_it_role": True,
            "seniority": "junior",
            "seniority_reasoning": "Junior bar.",
            "location_fit": "exact_city",
            "location_reasoning": "Varna.",
            "experience_fit": "acceptable",
            "mandatory_requirements": [{"requirement": "PHP", "candidate_fit": "strong"}],
            "nice_to_have_requirements": [],
            "major_strengths": ["PHP and Laravel match the stack"],
            "major_risks": ["Small team"],
            "reasoning": "Good overlap with the candidate's PHP work.",
            "decision": decision,
            "confidence": confidence,
        }
    )


class StubLocalModel(LocalModelProvider):
    """Answers instantly, and differently per role, so buckets can be asserted."""

    def __init__(self) -> None:
        super().__init__(LocalModelConfig(model="stub:test"))
        self.calls = 0

    def _chat(self, system: str, user: str) -> tuple[str, int]:
        # Keyed off the role title so a test can ask for a specific bucket.
        self.calls += 1
        decision = "review" if "QA Engineer" in user else "apply"
        return evaluation_json(decision), 5


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
    ctx.__dict__["local_model"] = StubLocalModel()
    ctx.__dict__["evaluator"] = JobEvaluator(ctx.local_model)
    return ctx


def install(monkeypatch, discovery: FakeDiscovery) -> FakeDiscovery:
    monkeypatch.setattr(runner_module, "JobsBgDiscovery", discovery)
    return discovery


def notifying(context: AppContext) -> AppContext:
    """Turn the dashboard channel on so notifications are observable."""
    context.settings.notify_dashboard = True
    context.__dict__.pop("notifier", None)
    return context


def digests(context: AppContext) -> list[Notification]:
    with context.session() as session:
        return [
            row
            for row in session.scalars(select(Notification)).all()
            if str(row.kind) == "today_digest"
        ]


class TestDateFiltering:
    def test_only_todays_listings_are_processed(self, context, monkeypatch) -> None:
        today_card = card("1", "Junior PHP Developer")
        old_card = card("2", "Junior Python Developer", posted=YESTERDAY)
        install(monkeypatch, FakeDiscovery([today_card, old_card]))

        result = run_today_scan(context, day=TODAY, notify=False)

        assert [o.title for o in result.outcomes] == ["Junior PHP Developer"]
        with context.session() as session:
            stored = session.scalars(select(Job)).all()
        assert [job.source_job_id for job in stored] == ["1"]

    def test_the_requested_date_reaches_discovery(self, context, monkeypatch) -> None:
        discovery = install(monkeypatch, FakeDiscovery([card("1", "Junior PHP Developer")]))
        run_today_scan(context, day=TODAY, notify=False)
        assert discovery.searched_dates == [TODAY]

    def test_a_listing_with_no_readable_date_is_not_claimed_for_today(
        self, context, monkeypatch
    ) -> None:
        """Guessing would let history through as new."""
        undated = card("3", "Junior PHP Developer")
        undated.posted_at_raw = None
        install(monkeypatch, FakeDiscovery([undated]))

        result = run_today_scan(context, day=TODAY, notify=False)
        assert result.outcomes == []


class TestNewVersusSeen:
    def test_a_new_listing_is_evaluated(self, context, monkeypatch) -> None:
        one = card("1", "Junior PHP Developer")
        install(monkeypatch, FakeDiscovery([one], {one.source_url: with_detail(one)}))

        result = run_today_scan(context, day=TODAY, notify=False)

        assert len(result.new) == 1
        assert context.local_model.calls == 1
        assert result.outcomes[0].evaluation.source != "unknown"

    def test_a_second_run_finds_nothing_new(self, context, monkeypatch) -> None:
        one = card("1", "Junior PHP Developer")
        install(monkeypatch, FakeDiscovery([one], {one.source_url: with_detail(one)}))

        first = run_today_scan(context, day=TODAY, notify=False)
        second = run_today_scan(context, day=TODAY, notify=False)

        assert len(first.new) == 1
        assert second.new == []
        assert len(second.already_seen) == 1

    def test_the_evaluation_cache_is_respected_on_a_re_scan(self, context, monkeypatch) -> None:
        """Re-seeing an unchanged listing must not cost a second model call."""
        one = card("1", "Junior PHP Developer")
        install(monkeypatch, FakeDiscovery([one], {one.source_url: with_detail(one)}))

        run_today_scan(context, day=TODAY, notify=False)
        after_first = context.local_model.calls
        run_today_scan(context, day=TODAY, notify=False)

        assert after_first == 1
        assert context.local_model.calls == 1

    def test_an_old_listing_rediscovered_is_not_new(self, context, monkeypatch) -> None:
        """Seen on an earlier day, published today: shown, but not announced."""
        one = card("1", "Junior PHP Developer")
        install(monkeypatch, FakeDiscovery([one], {one.source_url: with_detail(one)}))
        run_today_scan(context, day=TODAY, notify=False)

        with context.session() as session:
            job = session.scalar(select(Job))
            job.first_seen_at = job.first_seen_at - timedelta(days=3)

        result = run_today_scan(context, day=TODAY, notify=False)
        assert result.new == []
        with context.session() as session:
            today = build_today_jobs(session, TODAY)
        assert today.total == 1
        assert today.new_count == 0


class TestDecisionCounts:
    def test_apply_review_and_skip_are_counted(self, context, monkeypatch) -> None:
        cards = [
            card("1", "Junior PHP Developer"),
            card("2", "Junior QA Engineer", tech=["Selenium"]),
            card("3", "Senior Java Architect", level="Ниво Senior-level"),
        ]
        details = {one.source_url: with_detail(one) for one in cards}
        install(monkeypatch, FakeDiscovery(cards, details))

        result = run_today_scan(context, day=TODAY, notify=False)

        assert result.apply_count == 1
        assert result.review_count == 1
        assert result.skip_count == 1
        assert len(result.outcomes) == 3

    def test_the_gate_still_settles_a_senior_title_without_the_model(
        self, context, monkeypatch
    ) -> None:
        one = card("9", "Senior Java Architect", level="Ниво Senior-level")
        install(monkeypatch, FakeDiscovery([one], {one.source_url: with_detail(one)}))

        result = run_today_scan(context, day=TODAY, notify=False)

        assert result.outcomes[0].decision is Decision.SKIP
        assert context.local_model.calls == 0

    def test_the_policy_still_downgrades_another_city(self, context, monkeypatch) -> None:
        """The model says apply; the posting is in Sofia. The policy wins."""
        one = card("4", "Junior PHP Developer", location="София")
        install(monkeypatch, FakeDiscovery([one], {one.source_url: with_detail(one)}))

        result = run_today_scan(context, day=TODAY, notify=False)
        assert result.outcomes[0].decision is Decision.SKIP


class TestNotifications:
    def test_new_listings_produce_one_digest(self, context, monkeypatch) -> None:
        cards = [card("1", "Junior PHP Developer"), card("2", "Junior QA Engineer")]
        details = {one.source_url: with_detail(one) for one in cards}
        install(monkeypatch, FakeDiscovery(cards, details))

        result = run_today_scan(notifying(context), day=TODAY)

        assert result.notified is True
        sent = digests(context)
        assert len(sent) == 1
        assert "2 new IT job" in sent[0].title
        assert "Junior PHP Developer" in (sent[0].body or "")

    def test_a_digest_replaces_the_per_job_notifications(self, context, monkeypatch) -> None:
        one = card("1", "Junior PHP Developer")
        install(monkeypatch, FakeDiscovery([one], {one.source_url: with_detail(one)}))

        run_today_scan(notifying(context), day=TODAY)

        with context.session() as session:
            kinds = [str(n.kind) for n in session.scalars(select(Notification)).all()]
        assert kinds == ["today_digest"]

    def test_running_again_does_not_notify_twice(self, context, monkeypatch) -> None:
        one = card("1", "Junior PHP Developer")
        install(monkeypatch, FakeDiscovery([one], {one.source_url: with_detail(one)}))

        run_today_scan(notifying(context), day=TODAY)
        second = run_today_scan(context, day=TODAY)

        assert second.notified is False
        assert len(digests(context)) == 1

    def test_nothing_new_sends_nothing(self, context, monkeypatch) -> None:
        install(monkeypatch, FakeDiscovery([card("1", "Junior PHP Developer", posted=YESTERDAY)]))

        result = run_today_scan(notifying(context), day=TODAY)

        assert result.notified is False
        assert digests(context) == []

    def test_notify_false_stays_silent(self, context, monkeypatch) -> None:
        one = card("1", "Junior PHP Developer")
        install(monkeypatch, FakeDiscovery([one], {one.source_url: with_detail(one)}))

        run_today_scan(notifying(context), day=TODAY, notify=False)
        assert digests(context) == []


class TestEmptyDay:
    def test_a_day_with_nothing_is_a_clean_result(self, context, monkeypatch) -> None:
        install(monkeypatch, FakeDiscovery([]))

        result = run_today_scan(context, day=TODAY, notify=False)

        assert result.completed is True
        assert result.outcomes == []
        assert result.new == []
        assert (result.apply_count, result.review_count, result.skip_count) == (0, 0, 0)
        assert result.notified is False

    def test_the_summary_says_so_plainly(self, context, monkeypatch) -> None:
        from jobhunter.today import render_today_scan

        install(monkeypatch, FakeDiscovery([]))
        text = render_today_scan(run_today_scan(context, day=TODAY, notify=False))
        assert "Nothing has been published today yet." in text


class TestReadBack:
    def test_the_page_model_sees_what_the_scan_stored(self, context, monkeypatch) -> None:
        cards = [card("1", "Junior PHP Developer"), card("2", "Junior QA Engineer")]
        details = {one.source_url: with_detail(one) for one in cards}
        install(monkeypatch, FakeDiscovery(cards, details))
        run_today_scan(context, day=TODAY, notify=False)

        with context.session() as session:
            today = build_today_jobs(session, TODAY)

        assert today.total == 2
        assert today.new_count == 2
        assert today.apply_count + today.review_count + today.skip_count == 2
        assert [item.decision for item in today.jobs] == sorted(
            [item.decision for item in today.jobs],
            key=lambda d: {"apply": 0, "review": 1}.get(d.value, 2),
        )

    def test_yesterdays_listing_is_not_on_todays_page(self, context, monkeypatch) -> None:
        old = card("2", "Junior Python Developer", posted=YESTERDAY)
        install(monkeypatch, FakeDiscovery([old], {old.source_url: with_detail(old)}))
        run_today_scan(context, day=YESTERDAY, notify=False)

        with context.session() as session:
            assert build_today_jobs(session, TODAY).total == 0
            assert build_today_jobs(session, YESTERDAY).total == 1

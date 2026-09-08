"""Browser manager behaviour that does not need a real browser."""

from __future__ import annotations

from typing import Any

from jobhunter.browser.challenge import ChallengeResult
from jobhunter.browser.manager import BrowserManager


class RecordingPage:
    def __init__(self) -> None:
        self.visited: list[str] = []

    def goto(self, url: str, **kwargs: Any) -> None:
        self.visited.append(url)

    def title(self) -> str:
        return "JOBS.BG"

    def inner_text(self, selector: str) -> str:
        return "content " * 300

    def content(self) -> str:
        return "<html></html>"

    def query_selector_all(self, selector: str) -> list[Any]:
        return []

    def get_by_role(self, *args: Any, **kwargs: Any) -> Any:
        class _L:
            @staticmethod
            def count() -> int:
                return 0

        return _L()


def make_manager(settings) -> BrowserManager:
    manager = BrowserManager(settings)
    # Skip pacing so the test does not sleep.
    manager._wait_turn = lambda: None  # type: ignore[method-assign]
    manager.check_challenge = lambda page, status=None: ChallengeResult()  # type: ignore[method-assign]
    return manager


class TestSessionWarmUp:
    """A cold profile that deep-links into a search URL is served an empty 403.

    Visiting the front page first is what makes a first run work at all.
    """

    def test_visits_the_site_root_before_a_deep_link(self, settings) -> None:
        manager = make_manager(settings)
        page = RecordingPage()
        manager.warm_up("https://www.jobs.bg/front_job_search.php?subm=1", page)
        assert page.visited == ["https://www.jobs.bg/"]

    def test_only_warms_each_host_once(self, settings) -> None:
        manager = make_manager(settings)
        page = RecordingPage()
        manager.warm_up("https://www.jobs.bg/front_job_search.php", page)
        manager.warm_up("https://www.jobs.bg/job/123", page)
        assert page.visited == ["https://www.jobs.bg/"]

    def test_does_not_warm_up_for_the_root_itself(self, settings) -> None:
        manager = make_manager(settings)
        page = RecordingPage()
        manager.warm_up("https://www.jobs.bg/", page)
        assert page.visited == []

    def test_warm_up_failure_is_not_fatal(self, settings) -> None:
        class BrokenPage(RecordingPage):
            def goto(self, url: str, **kwargs: Any) -> None:
                raise RuntimeError("network down")

        manager = make_manager(settings)
        manager.warm_up("https://www.jobs.bg/front_job_search.php", BrokenPage())


class TestRobots:
    def test_disabled_setting_allows_everything(self, settings) -> None:
        settings.respect_robots_txt = False
        manager = BrowserManager(settings)
        assert manager.robots_allows("https://www.jobs.bg/anything") is True

    def test_unreadable_robots_does_not_block(self, settings) -> None:
        manager = BrowserManager(settings)
        manager._robots_checked = True
        manager._robots = None
        assert manager.robots_allows("https://www.jobs.bg/front_job_search.php") is True

    def test_parsed_rules_are_honoured(self, settings) -> None:
        import urllib.robotparser

        parser = urllib.robotparser.RobotFileParser()
        parser.parse(["User-agent: *", "Disallow: /private"])
        manager = BrowserManager(settings)
        manager._robots_checked = True
        manager._robots = parser
        assert manager.robots_allows("https://www.jobs.bg/private/x") is False
        assert manager.robots_allows("https://www.jobs.bg/job/1") is True

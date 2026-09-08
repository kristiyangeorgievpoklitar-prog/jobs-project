"""Application automation, driven against fake page objects.

These assert the safety boundaries: external listings, employer questionnaires
and CAPTCHA gates must all stop rather than guess or bypass.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jobhunter.browser.challenge import ChallengeResult, ChallengeType
from jobhunter.domain.enums import ApplicationMethod
from jobhunter.sources.jobsbg.applier import JobsBgApplier


class FakeElement:
    def __init__(
        self, attrs: dict[str, str] | None = None, text: str = "", visible: bool = True
    ) -> None:
        self.attrs = attrs or {}
        self.text = text
        self.visible = visible
        self.filled: str | None = None
        self.files: list[str] = []
        self.clicked = False

    def get_attribute(self, name: str) -> str | None:
        return self.attrs.get(name)

    def inner_text(self) -> str:
        return self.text

    def is_visible(self) -> bool:
        return self.visible

    def fill(self, value: str) -> None:
        self.filled = value

    def set_input_files(self, path: str) -> None:
        self.files.append(path)

    def click(self, **kwargs: Any) -> None:
        self.clicked = True


class FakePage:
    """Minimal Playwright page stand-in driven by a selector map."""

    def __init__(
        self,
        selectors: dict[str, list[FakeElement]],
        html: str = "",
        url: str = "https://www.jobs.bg/job/1",
        body: str = "",
    ) -> None:
        self.selectors = selectors
        self._html = html
        self.url = url
        self.body = body

    def query_selector(self, selector: str) -> FakeElement | None:
        found = self.query_selector_all(selector)
        return found[0] if found else None

    def query_selector_all(self, selector: str) -> list[FakeElement]:
        for key, elements in self.selectors.items():
            if key in selector or selector in key:
                return elements
        return []

    def content(self) -> str:
        return self._html

    def inner_text(self, selector: str) -> str:
        return self.body

    def get_by_text(self, text: str, exact: bool = False) -> Any:
        class _Locator:
            @staticmethod
            def count() -> int:
                return 0

        return _Locator()


class FakeBrowserManager:
    def __init__(self, page: FakePage, challenge: ChallengeResult | None = None) -> None:
        self.page = page
        self.challenge = challenge or ChallengeResult()
        self.visited: list[str] = []

    def goto(self, url: str, page: Any = None, **kwargs: Any) -> FakePage:
        self.visited.append(url)
        self.page.url = url
        return self.page

    def new_page(self) -> FakePage:
        return self.page

    def check_challenge(self, page: Any, status: int | None = None) -> ChallengeResult:
        return self.challenge


class TestApplyMethodDetection:
    def test_detects_internal_form(self) -> None:
        page = FakePage(
            {"js_send_cv": [FakeElement({"href": "https://www.jobs.bg/js_send_cv.php?job_sid=1"})]}
        )
        method, url = JobsBgApplier(FakeBrowserManager(page)).find_apply_entry(page)
        assert method is ApplicationMethod.JOBSBG_INTERNAL
        assert url is not None

    def test_detects_external_link(self) -> None:
        page = FakePage(
            {
                "a[href]": [
                    FakeElement(
                        {"href": "https://acme.workday.com/job/1"},
                        "open_in_new ВЪНШНО КАНДИДАТСТВАНЕ",
                    )
                ]
            }
        )
        method, url = JobsBgApplier(FakeBrowserManager(page)).find_apply_entry(page)
        assert method is ApplicationMethod.EXTERNAL_URL
        assert url == "https://acme.workday.com/job/1"


class TestSafetyBoundaries:
    def test_external_listing_requires_a_manual_step(self) -> None:
        page = FakePage(
            {
                "a[href]": [
                    FakeElement({"href": "https://acme.workday.com/job/1"}, "ВЪНШНО КАНДИДАТСТВАНЕ")
                ]
            }
        )
        outcome = JobsBgApplier(FakeBrowserManager(page)).apply("https://www.jobs.bg/job/1")
        assert outcome.success is False
        assert outcome.requires_manual_step is True
        assert "their own site" in (outcome.failure_reason or "")

    def test_employer_questionnaire_is_never_auto_answered(self, tmp_path: Path) -> None:
        """Answering an employer's free-text question would mean inventing content."""
        page = FakePage(
            {
                "js_send_cv": [
                    FakeElement({"href": "https://www.jobs.bg/js_send_cv.php?job_sid=1"})
                ],
                "textarea[name^='q_textarea']": [FakeElement(), FakeElement()],
                "form": [FakeElement()],
            },
            html="<form id='questionaryForm'></form>",
        )
        outcome = JobsBgApplier(FakeBrowserManager(page)).apply(
            "https://www.jobs.bg/job/1", submit=True, screenshots_dir=tmp_path
        )
        assert outcome.success is False
        assert outcome.requires_manual_step is True
        assert "questions" in (outcome.failure_reason or "").lower()

    def test_recaptcha_gate_blocks_submission(self, tmp_path: Path) -> None:
        page = FakePage(
            {
                "js_send_cv": [
                    FakeElement({"href": "https://www.jobs.bg/js_send_cv.php?job_sid=1"})
                ],
                "form": [FakeElement()],
            },
            html="<button onclick='submitRecaptchaForm(this)'>ЗАПИШИ</button>",
        )
        outcome = JobsBgApplier(FakeBrowserManager(page)).apply(
            "https://www.jobs.bg/job/1", submit=True, screenshots_dir=tmp_path
        )
        assert outcome.success is False
        assert outcome.blocked is True
        assert "recaptcha" in (outcome.failure_reason or "").lower()

    def test_detected_challenge_stops_the_flow(self, tmp_path: Path) -> None:
        page = FakePage(
            {"js_send_cv": [FakeElement({"href": "https://www.jobs.bg/js_send_cv.php?job_sid=1"})]}
        )
        browser = FakeBrowserManager(
            page, ChallengeResult(ChallengeType.CAPTCHA, "captcha visible")
        )
        outcome = JobsBgApplier(browser).apply(
            "https://www.jobs.bg/job/1", screenshots_dir=tmp_path
        )
        assert outcome.blocked is True

    def test_prepare_only_does_not_submit(self, tmp_path: Path) -> None:
        submit_button = FakeElement(text="ЗАПИШИ")
        page = FakePage(
            {
                "js_send_cv": [
                    FakeElement({"href": "https://www.jobs.bg/js_send_cv.php?job_sid=1"})
                ],
                "form": [FakeElement()],
                "button": [submit_button],
            },
            html="<form></form>",
        )
        outcome = JobsBgApplier(FakeBrowserManager(page)).apply(
            "https://www.jobs.bg/job/1", submit=False, screenshots_dir=tmp_path
        )
        assert outcome.success is False
        assert submit_button.clicked is False


class TestFormFilling:
    def test_fills_known_fields_and_attaches_cv(self, tmp_path: Path) -> None:
        cv = tmp_path / "cv.pdf"
        cv.write_bytes(b"%PDF-1.4")
        name, email, phone, letter, file_input = (
            FakeElement(),
            FakeElement(),
            FakeElement(),
            FakeElement(),
            FakeElement(),
        )
        page = FakePage(
            {
                "input[name*='name']": [name],
                "input[type=email]": [email],
                "input[name*='phone']": [phone],
                "textarea[name*='letter']": [letter],
                "input[type=file]": [file_input],
            }
        )
        filled = JobsBgApplier(FakeBrowserManager(page)).fill_form(
            page,
            full_name="Ivan",
            email="i@example.com",
            phone="+359",
            cover_letter="Hello",
            cv_path=cv,
        )
        assert set(filled) == {"name", "email", "phone", "cover_letter", "cv"}
        assert name.filled == "Ivan"
        assert file_input.files == [str(cv)]

    def test_missing_fields_are_skipped_quietly(self) -> None:
        page = FakePage({})
        assert JobsBgApplier(FakeBrowserManager(page)).fill_form(page, full_name="Ivan") == []


class TestSubmissionVerification:
    def test_requires_positive_confirmation(self) -> None:
        page = FakePage({}, body="Благодарим! Кандидатурата е изпратена успешно.")
        ok, evidence = JobsBgApplier(FakeBrowserManager(page)).verify_submission(page, timeout=2)
        assert ok is True
        assert "благодарим" in evidence.lower()

    def test_error_text_is_a_failure(self) -> None:
        page = FakePage({}, body="Грешка: задължително поле")
        ok, _ = JobsBgApplier(FakeBrowserManager(page)).verify_submission(page, timeout=2)
        assert ok is False

    def test_silence_is_not_success(self) -> None:
        """Absence of an error must never be read as a successful application."""
        page = FakePage({}, body="Some unrelated page content")
        ok, evidence = JobsBgApplier(FakeBrowserManager(page)).verify_submission(page, timeout=2)
        assert ok is False
        assert "no confirmation" in evidence

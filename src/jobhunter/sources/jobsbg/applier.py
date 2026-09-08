"""Jobs.bg application automation.

What this can and cannot do, established by inspecting the live site:

* **External listings** ("ВЪНШНО КАНДИДАТСТВАНЕ") hand off to a third-party ATS
  (Workday, UKG, ...). Those are not automated - the job is recorded as needing
  a manual step and the apply URL is surfaced in the dashboard.
* **Internal listings** use the site's own form. That form frequently redirects
  to an employer questionnaire with free-text questions, and its submit button
  is wired to reCAPTCHA (``submitRecaptchaForm``).

The system therefore fills everything it legitimately can and then stops at the
gate: it never answers employer questions on the candidate's behalf (that would
mean inventing answers) and never attempts to solve a CAPTCHA. When either is
present the workflow pauses and hands the open browser back to the user.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobhunter.browser.diagnostics import capture
from jobhunter.browser.manager import BrowserManager
from jobhunter.domain.enums import ApplicationMethod
from jobhunter.domain.schemas import ApplicationOutcome
from jobhunter.logging_setup import get_logger
from jobhunter.sources.jobsbg.selectors import INTERNAL_APPLY_TEXTS, ApplySelectors

log = get_logger(__name__)

SUCCESS_MARKERS = (
    "благодарим",
    "кандидатурата е изпратена",
    "успешно",
    "изпратена успешно",
    "вашата кандидатура",
    "thank you",
    "successfully",
    "application sent",
    "your application has been",
)

LOGIN_REQUIRED_MARKERS = (
    "необходимо е да влезете",
    "за да кандидатствате",
    "log in with your account",
    "необходимо е да влезете с вашия акаунт",
)

FAILURE_MARKERS = (
    "грешка",
    "неуспешно",
    "задължително поле",
    "error",
    "required field",
    "please fill",
)


@dataclass
class ApplyFormState:
    """What the application form actually contains."""

    url: str = ""
    has_form: bool = False
    file_inputs: int = 0
    text_questions: int = 0
    radio_questions: int = 0
    checkbox_questions: int = 0
    name_field: bool = False
    email_field: bool = False
    phone_field: bool = False
    cover_letter_field: bool = False
    submit_button: bool = False
    recaptcha_wired: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def has_employer_questions(self) -> bool:
        return (self.text_questions + self.radio_questions + self.checkbox_questions) > 0

    @property
    def blocks_automation(self) -> bool:
        return self.has_employer_questions or self.recaptcha_wired


class JobsBgApplier:
    """Drives the Jobs.bg application form."""

    name = "jobs.bg"

    def __init__(self, browser: BrowserManager) -> None:
        self.browser = browser

    # ------------------------------------------------------------ helpers

    def find_apply_entry(self, page: Any) -> tuple[ApplicationMethod, str | None]:
        """Locate the apply affordance on an open job page.

        A listing can expose two internal routes: ``js_send_cv.php`` (requires a
        Jobs.bg login) and ``js_send_cv_regless.php`` ("apply without an
        account"). When no session is present the regless route is the only one
        that can proceed, so it is preferred.
        """
        internal_links: list[str] = []
        for anchor in page.query_selector_all(
            'a[href*="js_send_cv"], a[href*="js_fill_questionary"]'
        ):
            href = anchor.get_attribute("href")
            if href:
                internal_links.append(href)

        if internal_links:
            logged_in = False
            try:
                logged_in = self.browser.is_logged_in(page)
            except Exception:
                logged_in = False

            regless = next((h for h in internal_links if "regless" in h), None)
            with_account = next((h for h in internal_links if "regless" not in h), None)

            chosen = (with_account or regless) if logged_in else (regless or with_account)
            return ApplicationMethod.JOBSBG_INTERNAL, chosen

        for anchor in page.query_selector_all("a[href]"):
            try:
                text = " ".join((anchor.inner_text() or "").split()).upper()
            except Exception:
                continue
            if "ВЪНШНО КАНДИДАТСТВАНЕ" in text:
                href = anchor.get_attribute("href")
                if href and "jobs.bg" not in href.lower():
                    return ApplicationMethod.EXTERNAL_URL, href

        for label in INTERNAL_APPLY_TEXTS:
            try:
                candidate = page.get_by_text(label, exact=False)
                if candidate.count():
                    return ApplicationMethod.JOBSBG_INTERNAL, None
            except Exception:
                continue

        return ApplicationMethod.UNKNOWN, None

    def inspect_form(self, page: Any) -> ApplyFormState:
        """Describe the application form without changing anything."""
        state = ApplyFormState(url=page.url)
        try:
            html = page.content()
        except Exception:
            html = ""

        state.has_form = bool(page.query_selector(ApplySelectors.FORM))
        state.file_inputs = len(page.query_selector_all(ApplySelectors.FILE_INPUT))
        state.text_questions = len(page.query_selector_all(ApplySelectors.QUESTION_TEXTAREA))
        state.radio_questions = len(
            {
                el.get_attribute("name")
                for el in page.query_selector_all(ApplySelectors.QUESTION_RADIO)
                if el.get_attribute("name")
            }
        )
        state.checkbox_questions = len(page.query_selector_all(ApplySelectors.QUESTION_CHECKBOX))
        state.name_field = bool(page.query_selector(ApplySelectors.NAME_INPUT))
        state.email_field = bool(page.query_selector(ApplySelectors.EMAIL_INPUT))
        state.phone_field = bool(page.query_selector(ApplySelectors.PHONE_INPUT))
        state.cover_letter_field = bool(page.query_selector(ApplySelectors.COVER_LETTER))
        state.submit_button = bool(page.query_selector(ApplySelectors.SUBMIT_BUTTON))
        state.recaptcha_wired = "submitRecaptchaForm" in html or "g-recaptcha" in html

        if state.has_employer_questions:
            state.notes.append(
                f"{state.text_questions} free-text and {state.radio_questions} choice "
                "question(s) from the employer"
            )
        if state.recaptcha_wired:
            state.notes.append("submission is gated by reCAPTCHA")
        return state

    def fill_form(
        self,
        page: Any,
        *,
        full_name: str | None = None,
        email: str | None = None,
        phone: str | None = None,
        cover_letter: str | None = None,
        cv_path: Path | None = None,
    ) -> list[str]:
        """Fill the fields we can legitimately fill. Returns what was filled."""
        filled: list[str] = []

        def try_fill(selector: str, value: str | None, label: str) -> None:
            if not value:
                return
            try:
                element = page.query_selector(selector)
                if element is not None and element.is_visible():
                    element.fill(value)
                    filled.append(label)
            except Exception as exc:
                log.warning("form_fill_failed", field=label, error=str(exc))

        try_fill(ApplySelectors.NAME_INPUT, full_name, "name")
        try_fill(ApplySelectors.EMAIL_INPUT, email, "email")
        try_fill(ApplySelectors.PHONE_INPUT, phone, "phone")
        try_fill(ApplySelectors.COVER_LETTER, cover_letter, "cover_letter")

        if cv_path is not None and cv_path.exists():
            try:
                file_input = page.query_selector(ApplySelectors.FILE_INPUT)
                if file_input is not None:
                    file_input.set_input_files(str(cv_path))
                    filled.append("cv")
            except Exception as exc:
                log.warning("cv_attach_failed", error=str(exc))

        return filled

    def verify_submission(self, page: Any, *, timeout: float = 15.0) -> tuple[bool, str]:
        """Confirm a submission actually succeeded.

        Success is only reported on positive evidence in the page, never
        assumed from the absence of an error.
        """
        deadline = time.monotonic() + timeout
        last_text = ""
        while time.monotonic() < deadline:
            time.sleep(1.5)
            try:
                last_text = page.inner_text("body").lower()
            except Exception:
                continue
            for marker in SUCCESS_MARKERS:
                if marker in last_text:
                    return True, f"confirmation text found: '{marker}'"
            for marker in FAILURE_MARKERS:
                if marker in last_text:
                    return False, f"error text found: '{marker}'"
        return False, "no confirmation message appeared"

    # -------------------------------------------------------------- apply

    def apply(
        self,
        job_url: str,
        *,
        submit: bool = False,
        full_name: str | None = None,
        email: str | None = None,
        phone: str | None = None,
        cover_letter: str | None = None,
        cv_path: Path | None = None,
        screenshots_dir: Path | None = None,
    ) -> ApplicationOutcome:
        """Open a listing and take the application as far as is permitted."""
        page = self.browser.goto(job_url)

        method, apply_url = self.find_apply_entry(page)
        log.info("apply_method_detected", method=method.value, url=apply_url)

        if method is ApplicationMethod.EXTERNAL_URL:
            return ApplicationOutcome(
                success=False,
                requires_manual_step=True,
                failure_reason=(
                    "This employer accepts applications on their own site, not through "
                    "Jobs.bg. Open the external link and apply there."
                ),
                evidence=apply_url,
            )

        if method is not ApplicationMethod.JOBSBG_INTERNAL:
            return ApplicationOutcome(
                success=False,
                requires_manual_step=True,
                failure_reason="No Jobs.bg application form was found on this listing.",
            )

        if apply_url:
            page = self.browser.goto(apply_url, page=page)
        else:
            for label in INTERNAL_APPLY_TEXTS:
                try:
                    button = page.get_by_text(label, exact=False)
                    if button.count():
                        button.first.click()
                        time.sleep(3)
                        break
                except Exception:
                    continue

        try:
            page_text = page.inner_text("body").lower()
        except Exception:
            page_text = ""
        if any(marker in page_text for marker in LOGIN_REQUIRED_MARKERS):
            shot = capture(page, screenshots_dir, "apply-login-required") if screenshots_dir else {}
            return ApplicationOutcome(
                success=False,
                requires_manual_step=True,
                failure_reason=(
                    "Jobs.bg requires a signed-in account for this application. Log in once "
                    "in the browser window that just opened - the session is kept in the "
                    "local browser profile - then run this again."
                ),
                screenshot_path=shot.get("screenshot"),
            )

        challenge = self.browser.check_challenge(page)
        if challenge.is_blocking:
            shot = capture(page, screenshots_dir, "apply-challenge") if screenshots_dir else {}
            return ApplicationOutcome(
                success=False,
                blocked=True,
                failure_reason=f"Blocked by {challenge.type.value}: {challenge.detail}",
                screenshot_path=shot.get("screenshot"),
            )

        state = self.inspect_form(page)
        log.info(
            "apply_form_inspected",
            has_form=state.has_form,
            questions=state.text_questions + state.radio_questions,
            recaptcha=state.recaptcha_wired,
        )

        filled = self.fill_form(
            page,
            full_name=full_name,
            email=email,
            phone=phone,
            cover_letter=cover_letter,
            cv_path=cv_path,
        )

        artifacts = capture(page, screenshots_dir, "apply-form") if screenshots_dir else {}

        if state.has_employer_questions:
            return ApplicationOutcome(
                success=False,
                requires_manual_step=True,
                failure_reason=(
                    "This employer asks their own questions ("
                    + "; ".join(state.notes)
                    + "). Answering them automatically would mean inventing answers, "
                    "so the form has been prepared and left open for you to complete."
                ),
                evidence=f"filled: {', '.join(filled) or 'nothing'}",
                screenshot_path=artifacts.get("screenshot"),
            )

        if state.recaptcha_wired:
            return ApplicationOutcome(
                success=False,
                blocked=True,
                requires_manual_step=True,
                failure_reason=(
                    "Submission on this form is gated by reCAPTCHA, which must be "
                    "completed by a person. The form is filled and waiting in the browser."
                ),
                evidence=f"filled: {', '.join(filled) or 'nothing'}",
                screenshot_path=artifacts.get("screenshot"),
            )

        if not submit:
            return ApplicationOutcome(
                success=False,
                requires_manual_step=True,
                failure_reason="Prepared but not submitted (submit was not requested).",
                evidence=f"filled: {', '.join(filled) or 'nothing'}",
                screenshot_path=artifacts.get("screenshot"),
            )

        if not state.submit_button:
            return ApplicationOutcome(
                success=False,
                failure_reason="No submit button was found on the application form.",
                screenshot_path=artifacts.get("screenshot"),
            )

        try:
            page.query_selector(ApplySelectors.SUBMIT_BUTTON).click()
        except Exception as exc:
            return ApplicationOutcome(
                success=False,
                failure_reason=f"Could not click submit: {exc}",
                screenshot_path=artifacts.get("screenshot"),
            )

        ok, evidence = self.verify_submission(page)
        final = capture(page, screenshots_dir, "apply-result") if screenshots_dir else {}
        return ApplicationOutcome(
            success=ok,
            evidence=evidence,
            failure_reason=None if ok else evidence,
            screenshot_path=final.get("screenshot"),
        )

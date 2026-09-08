"""Every Jobs.bg CSS selector and site literal lives here.

Centralising them means a site redesign is a one-file change, and the parser
tests can assert against the same constants the browser code uses.
"""

from __future__ import annotations

from typing import Final


class ListingSelectors:
    """Search-results page."""

    CARD: Final = "div.mdc-layout-grid__inner"
    JOB_LINK: Final = 'a[href*="/job/"]'
    CARD_TITLE: Final = "div.card-title"
    CARD_INFO: Final = "div.card-info"
    CARD_DATE: Final = "div.card-date"
    SCROLL_AREA: Final = "div.scroll-area[data-id]"
    SKILL_TAG: Final = "span.skill, div.skill, .skill"
    COMPANY_SUBSCRIBE: Final = '[data-action="searchSubscribe"]'
    COMPANY_DETAILS: Final = '[data-action="openCompanyDetailsBottomUp"]'
    RESULT_COUNT_TITLE: Final = "title"


class DetailSelectors:
    """Single job page."""

    CONTENT: Final = "#jobViewContent"
    # The description body is rendered inside a sandboxed iframe, not in the
    # page DOM. Without entering it the listing has no requirements text at all.
    DESCRIPTION_IFRAME: Final = "iframe#customJobIframe, iframe.job-view-iframe"
    DESCRIPTION_IFRAME_URL_MARKER: Final = "job_view_sandboxed"
    # Some listings render the body inline instead of in the iframe.
    DESCRIPTION_INLINE: Final = ".job-view-left-column .bg-white"
    DESCRIPTION_CHROME: Final = ".no-print, .apply-actions, .view-extra, script, style"
    TITLE: Final = "div.card-title, h1"
    SKILL_TAG: Final = ".skill"
    COMPANY_DETAILS: Final = '[data-action="openCompanyDetailsBottomUp"]'
    EXTERNAL_APPLY_LINK: Final = 'a[href]:has-text("ВЪНШНО КАНДИДАТСТВАНЕ")'
    INTERNAL_APPLY_HREF: Final = 'a[href*="js_send_cv"]'
    ANY_APPLY_HREF: Final = 'a[href*="js_send_cv"], a[href*="js_fill_questionary"]'


class ApplySelectors:
    """Application form / questionnaire."""

    FORM: Final = (
        "form#questionaryForm, form[action*='js_fill_questionary'], form[action*='js_send_cv']"
    )
    SUBMIT_BUTTON: Final = "button[onclick*='submitRecaptchaForm'], button.mdc-button--raised"
    FILE_INPUT: Final = "input[type=file]"
    NAME_INPUT: Final = "input[name*='name'], input[name='names']"
    EMAIL_INPUT: Final = "input[type=email], input[name*='email']"
    PHONE_INPUT: Final = "input[name*='phone'], input[name*='tel']"
    COVER_LETTER: Final = (
        "textarea[name*='letter'], textarea[name*='message'], textarea[name*='comment']"
    )
    QUESTION_TEXTAREA: Final = "textarea[name^='q_textarea']"
    QUESTION_RADIO: Final = "input[type=radio][name^='q_radio']"
    QUESTION_CHECKBOX: Final = "input[type=checkbox][name^='q_check']"


class CommonSelectors:
    COOKIE_ACCEPT_LABELS: Final = ("ПРИЕМАМ ВСИЧКИ", "Приемам избраните", "Приемам", "Accept all")
    LOCATION_CHIP: Final = '[type="location_sid"]'
    LOGGED_IN_MARKERS: Final = ("Моят профил", "Изход", "My profile", "Logout")
    LOGGED_OUT_MARKERS: Final = ("Вход", "Създай акаунт", "Login")


# --- site text literals -------------------------------------------------------

EXTERNAL_APPLY_TEXT: Final = "ВЪНШНО КАНДИДАТСТВАНЕ"
EXTERNAL_APPLY_TEXT_ALT: Final = "Външно кандидатстване"
INTERNAL_APPLY_TEXTS: Final = (
    "КАНДИДАТСТВАЙ БЕЗ АКАУНТ",
    "КАНДИДАТСТВАЙ",
    "Кандидатствай",
)

# Material icon names used as field keys on the detail page.
ICON_LOCATION: Final = "location_on"
ICON_LEVEL: Final = "stairs"
ICON_EXPERIENCE: Final = "psychology"
ICON_HOME_OFFICE: Final = "chair"
ICON_CONTRACT: Final = "work"
ICON_SCHEDULE: Final = "schedule"
ICON_LEAVE: Final = "beach_access"
ICON_LANGUAGE: Final = "language"
ICON_SALARY: Final = "payments"
ICON_REMOTE_INTERVIEW: Final = "3p"

FIELD_ICONS: Final = frozenset(
    {
        ICON_LOCATION,
        ICON_LEVEL,
        ICON_EXPERIENCE,
        ICON_HOME_OFFICE,
        ICON_CONTRACT,
        ICON_SCHEDULE,
        ICON_LEAVE,
        ICON_LANGUAGE,
        ICON_SALARY,
        ICON_REMOTE_INTERVIEW,
    }
)

# Anti-automation / challenge markers. Checked against *visible* page state.
CHALLENGE_TITLE_MARKERS: Final = (
    "just a moment",
    "един момент",
    "attention required",
    "checking your browser",
    "проверка на браузъра",
)
CHALLENGE_BODY_MARKERS: Final = (
    "enable javascript and cookies to continue",
    "cf-challenge",
    "cf_chl_opt",
    "verify you are human",
    "потвърдете, че сте човек",
)

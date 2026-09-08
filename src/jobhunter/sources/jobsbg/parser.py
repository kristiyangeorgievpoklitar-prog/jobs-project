"""Pure HTML -> RawJob parsing for Jobs.bg.

Nothing here touches the network or a browser, so every branch is unit-testable
against the saved fixtures. Parsing is defensive: a malformed card yields None
rather than raising, and a missing field is simply absent.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from bs4 import BeautifulSoup, Tag

from jobhunter.domain.enums import ApplicationMethod
from jobhunter.domain.schemas import RawJob
from jobhunter.sources.jobsbg import selectors as S
from jobhunter.sources.jobsbg.urls import BASE_URL, extract_job_id

_WS = re.compile(r"\s+")
# Material icon ligature names leak into text nodes; strip them out.
_ICON_WORDS = re.compile(
    r"\b(location_on|chair|stairs|psychology|work|schedule|beach_access|language|"
    r"payments|info|3p|star|bookmark_border|mail_outline|people_alt|open_in_new|"
    r"calendar_today|list|search|share|send|more_vert)\b"
)


def clean_text(value: str | None) -> str:
    """Collapse whitespace and drop material-icon ligature words."""
    if not value:
        return ""
    return _WS.sub(" ", _ICON_WORDS.sub(" ", value)).strip(" ;, ")


_BLANKS = re.compile(r"[ \t\x0b\f\r]+")
_MULTINEWLINE = re.compile(r"\n{3,}")


def clean_block_text(value: str | None) -> str:
    """Like :func:`clean_text` but keeps line breaks so bullet lists survive."""
    if not value:
        return ""
    without_icons = _ICON_WORDS.sub(" ", value)
    lines = [_BLANKS.sub(" ", line).strip() for line in without_icons.splitlines()]
    joined = "\n".join(line for line in lines if line)
    return _MULTINEWLINE.sub("\n\n", joined).strip()


def _text_of(node: Tag | None, selector: str | None = None) -> str:
    if node is None:
        return ""
    target = node.select_one(selector) if selector else node
    if target is None:
        return ""
    return clean_text(target.get_text(" ", strip=True))


def _parse_action_args(node: Tag | None) -> dict[str, Any]:
    """Decode a ``data-action-args`` JSON blob, tolerating malformed values."""
    if node is None:
        return {}
    raw = node.get("data-action-args")
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def parse_posted_date(value: str | None) -> datetime | None:
    """Parse Jobs.bg date strings: ``02.09.26`` or ``02.09.2026``."""
    text = clean_text(value)
    match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", text)
    if not match:
        return None
    day, month, year_raw = (int(match.group(1)), int(match.group(2)), match.group(3))
    year = int(year_raw)
    if len(year_raw) == 2:
        year += 2000
    try:
        return datetime(year, month, day, tzinfo=UTC)
    except ValueError:
        return None


# Leading segments that are field labels rather than a place name.
_FIELD_LABEL = re.compile(
    r"^(Ниво|Години опит|Отпуск|Заплата|Възможност|Дистанционн|Постоянна|Временна|"
    r"Пълно|Непълно|Стаж|Практика|Level|Salary)\b",
    re.IGNORECASE,
)


def parse_card_info(text: str) -> dict[str, str | None]:
    """Split the card subtitle into its labelled parts.

    Example input::

        Месторабота: София; Възможност за работа от вкъщи;
        Ниво Mid-level, Senior-level; Години опит от 3 до 6; Отпуск 24 дни
    """
    cleaned = clean_text(text)
    out: dict[str, str | None] = {
        "location": None,
        "level": None,
        "experience": None,
        "work_mode": None,
        "employment": None,
    }
    if not cleaned:
        return out

    if m := re.search(r"Месторабота\s*:?\s*([^;]+)", cleaned):
        out["location"] = clean_text(m.group(1))
    else:
        # Cards for jobs in the searched city omit the "Месторабота:" prefix and
        # simply lead with the place name.
        head = clean_text(cleaned.split(";")[0])
        if head and not _FIELD_LABEL.match(head) and len(head) < 60:
            out["location"] = head
    if m := re.search(r"Ниво\s+([^;]+)", cleaned):
        out["level"] = clean_text(m.group(1))
    if m := re.search(r"Години опит\s+([^;]+)", cleaned):
        out["experience"] = clean_text(m.group(1))
    if re.search(r"работа от вкъщи|from home|дистанционно", cleaned, re.I):
        out["work_mode"] = (
            clean_text(
                re.search(r"([^;]*работа от вкъщи[^;]*)", cleaned, re.I).group(1)  # type: ignore[union-attr]
            )
            if re.search(r"работа от вкъщи", cleaned, re.I)
            else "remote"
        )
    if m := re.search(r"(Постоянна работа|Временна работа|Стаж|Практика)", cleaned):
        out["employment"] = clean_text(m.group(1))
    return out


def parse_years_experience(text: str | None) -> float | None:
    """Extract the *lower bound* of a required-experience phrase.

    ``от 3 до 6`` -> 3.0, ``над 5 години`` -> 5.0, ``3+ years`` -> 3.0.
    The lower bound is what gates eligibility, so that is what we keep.
    """
    cleaned = clean_text(text)
    if not cleaned:
        return None
    if m := re.search(r"от\s*(\d+(?:[.,]\d+)?)\s*(?:до\s*\d+)?", cleaned, re.I):
        return float(m.group(1).replace(",", "."))
    if m := re.search(r"(\d+(?:[.,]\d+)?)\s*(?:\+|-|–|до)\s*(\d+(?:[.,]\d+)?)?", cleaned):
        return float(m.group(1).replace(",", "."))
    if m := re.search(r"(?:над|повече от|min(?:imum)?|поне)\s*(\d+(?:[.,]\d+)?)", cleaned, re.I):
        return float(m.group(1).replace(",", "."))
    if m := re.search(r"(\d+(?:[.,]\d+)?)", cleaned):
        return float(m.group(1).replace(",", "."))
    return None


def _absolute(href: str) -> str:
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        return f"{BASE_URL}{href}"
    return f"{BASE_URL}/{href}"


def parse_listing_card(card: Tag) -> RawJob | None:
    """Turn one search-result card into a RawJob, or None if it is not a job."""
    link = card.select_one(ListingLink.SELECTOR)
    if link is None:
        return None
    href = link.get("href")
    if not isinstance(href, str) or not href:
        return None
    url = _absolute(href)

    title_attr = link.get("title")
    title = clean_text(title_attr if isinstance(title_attr, str) else None)
    if not title:
        title = _text_of(card, S.ListingSelectors.CARD_TITLE)
    if not title:
        return None

    job_id = extract_job_id(url)
    scroll = card.select_one(S.ListingSelectors.SCROLL_AREA)
    if job_id is None and scroll is not None:
        data_id = scroll.get("data-id")
        if isinstance(data_id, str) and data_id.isdigit():
            job_id = data_id

    info_node = card.select_one(S.ListingSelectors.CARD_INFO)
    info = parse_card_info(info_node.get_text(" ", strip=True) if info_node else "")

    # Company: prefer the structured JSON payload, fall back to visible text.
    subscribe_args = _parse_action_args(card.select_one(S.ListingSelectors.COMPANY_SUBSCRIBE))
    params = subscribe_args.get("params") if isinstance(subscribe_args.get("params"), dict) else {}
    company_name = params.get("company_name") if isinstance(params, dict) else None
    company_id = params.get("company_id") if isinstance(params, dict) else None
    if not company_name:
        details = _parse_action_args(card.select_one(S.ListingSelectors.COMPANY_DETAILS))
        company_name = details.get("name")
        company_id = details.get("id", company_id)

    date_node = card.select_one(S.ListingSelectors.CARD_DATE)
    date_text = ""
    if date_node is not None:
        own = date_node.find(string=True, recursive=False)
        date_text = clean_text(str(own) if own else date_node.get_text(" ", strip=True))

    tags = [
        clean_text(t.get_text(" ", strip=True)) for t in card.select(S.ListingSelectors.SKILL_TAG)
    ]
    tech_tags = [t for t in dict.fromkeys(tags) if t and len(t) < 40]

    return RawJob(
        source_job_id=job_id,
        source_url=url,
        title=title,
        company_name=clean_text(company_name) or None,
        company_source_id=str(company_id) if company_id else None,
        location_raw=info["location"],
        posted_at_raw=date_text or None,
        level_raw=info["level"],
        experience_raw=info["experience"],
        work_mode_raw=info["work_mode"],
        employment_raw=info["employment"],
        tech_tags=tech_tags,
    )


class ListingLink:
    SELECTOR = S.ListingSelectors.JOB_LINK


def parse_listing_page(html: str) -> list[RawJob]:
    """Parse every job card on a search-results page, de-duplicated by URL."""
    soup = BeautifulSoup(html, "lxml")
    results: list[RawJob] = []
    seen: set[str] = set()
    for card in soup.select(S.ListingSelectors.CARD):
        if card.select_one(S.ListingSelectors.JOB_LINK) is None:
            continue
        job = parse_listing_card(card)
        if job is None or job.source_url in seen:
            continue
        seen.add(job.source_url)
        results.append(job)
    return results


def parse_total_results(html: str) -> int | None:
    """Read the result count out of the page title (``IT JOBS - 94 Обяви ...``)."""
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(strip=True) if soup.title else ""
    if m := re.search(r"(\d[\d\s ]*)\s*Обяви", title):
        return int(re.sub(r"[^\d]", "", m.group(1)))
    return None


def extract_icon_fields(scope: Tag) -> dict[str, str]:
    """Map material-icon field keys to their values on a detail page.

    Prefers the structured ``.options li`` list (icon + span). Falls back to
    scanning icons and reading their parent, which survives layout changes.
    """
    fields: dict[str, str] = {}

    for item in scope.select(".options li, ul li"):
        icon = item.select_one("i.material-icons, i.material-icons-outlined, span.material-icons")
        if icon is None:
            continue
        name = icon.get_text(strip=True)
        if name not in S.FIELD_ICONS or name in fields:
            continue
        value_node = item.select_one("span")
        value = clean_text(
            value_node.get_text(" ", strip=True) if value_node else item.get_text(" ", strip=True)
        )
        if value:
            fields[name] = value

    for icon in scope.select("i.material-icons, span.material-icons, i.material-icons-outlined"):
        name = icon.get_text(strip=True)
        if name not in S.FIELD_ICONS or name in fields:
            continue
        parent = icon.parent
        if parent is None:
            continue
        value = clean_text(parent.get_text(" ", strip=True))
        if value:
            fields[name] = value

    return fields


def extract_detail_tech_tags(scope: Tag) -> list[str]:
    """Tech tags are ``li`` entries that carry no icon (icons mark spec fields)."""
    tags: list[str] = []
    for item in scope.select("ul li"):
        if item.select_one("i.material-icons, i.material-icons-outlined, span.material-icons"):
            continue
        text = clean_text(item.get_text(" ", strip=True))
        if text and 1 < len(text) < 40 and text not in tags:
            tags.append(text)
    for node in scope.select(S.DetailSelectors.SKILL_TAG):
        text = clean_text(node.get_text(" ", strip=True))
        if text and 1 < len(text) < 40 and text not in tags:
            tags.append(text)
    return tags


def extract_title_and_company(scope: Tag) -> tuple[str, str | None]:
    """Read the title and trailing company name from the ``.view-extra`` header."""
    bold = scope.select_one(".view-extra span.bold")
    if bold is not None:
        title = clean_text(bold.get_text(" ", strip=True))
        container = bold.parent
        company = None
        if container is not None:
            whole = clean_text(container.get_text(" ", strip=True))
            remainder = (
                whole[len(title) :] if whole.startswith(title) else whole.replace(title, "", 1)
            )
            company = clean_text(remainder.lstrip(" ,")) or None
        if title:
            return title, company

    heading = scope.select_one("h2.job-view-title, h1, h2")
    if heading is not None:
        title = clean_text(heading.get_text(" ", strip=True))
        if title:
            return title, None
    return "", None


def detect_application_method(
    soup: BeautifulSoup, html: str
) -> tuple[ApplicationMethod, str | None]:
    """Decide how this listing can be applied to, and where."""
    internal = soup.select_one('a[href*="js_send_cv"], a[href*="js_fill_questionary"]')
    if internal is not None:
        href = internal.get("href")
        if isinstance(href, str) and href:
            return ApplicationMethod.JOBSBG_INTERNAL, _absolute(href)

    if S.EXTERNAL_APPLY_TEXT in html or S.EXTERNAL_APPLY_TEXT_ALT in html:
        for anchor in soup.find_all("a", href=True):
            text = clean_text(anchor.get_text(" ", strip=True)).upper()
            href = anchor["href"]
            if (
                S.EXTERNAL_APPLY_TEXT in text
                and isinstance(href, str)
                and "jobs.bg" not in href.lower()
            ):
                return ApplicationMethod.EXTERNAL_URL, href
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            if isinstance(href, str) and href.startswith("http") and "jobs.bg" not in href.lower():
                return ApplicationMethod.EXTERNAL_URL, href
        return ApplicationMethod.EXTERNAL_URL, None

    if m := re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", html):
        return ApplicationMethod.EMAIL, m.group(0)

    return ApplicationMethod.UNKNOWN, None


def parse_detail_page(html: str, *, source_url: str) -> RawJob | None:
    """Parse a single job page into a fully populated RawJob."""
    soup = BeautifulSoup(html, "lxml")
    content = soup.select_one(S.DetailSelectors.CONTENT) or soup.body
    if content is None:
        return None

    for junk in content.select("script, style"):
        junk.decompose()

    icon_fields = extract_icon_fields(content)
    title, company_name = extract_title_and_company(content)

    if not title and soup.title:
        raw = re.sub(r"^.*?JOBS\s*-\s*", "", soup.title.get_text(strip=True))
        title = clean_text(re.split(r",\s*(?:София|Варна|Пловдив|Бургас)", raw)[0])
    if not title:
        return None

    if not company_name:
        details = _parse_action_args(content.select_one(S.DetailSelectors.COMPANY_DETAILS))
        company_name = clean_text(details.get("name")) or None

    body_text = clean_block_text(content.get_text("\n", strip=True))
    flat_text = clean_text(content.get_text(" ", strip=True))

    ref_no = None
    if m := re.search(r"Ref\.No:\s*(\d+)", html):
        ref_no = m.group(1)

    posted_raw = None
    date_node = content.select_one(".view-extra .date")
    if date_node is not None:
        posted_raw = clean_text(date_node.get_text(" ", strip=True)) or None
    if not posted_raw and (m := re.search(r"(\d{2}\.\d{2}\.\d{4})", flat_text)):
        posted_raw = m.group(1)

    method, apply_url = detect_application_method(soup, html)

    languages = []
    if lang_value := icon_fields.get(S.ICON_LANGUAGE):
        languages = [clean_text(p) for p in re.split(r"[,;]", lang_value) if clean_text(p)]

    return RawJob(
        source_job_id=extract_job_id(source_url) or ref_no,
        source_url=source_url,
        title=title,
        company_name=company_name,
        location_raw=icon_fields.get(S.ICON_LOCATION),
        description=body_text or None,
        posted_at_raw=posted_raw,
        salary_raw=icon_fields.get(S.ICON_SALARY),
        level_raw=icon_fields.get(S.ICON_LEVEL),
        experience_raw=icon_fields.get(S.ICON_EXPERIENCE),
        employment_raw=icon_fields.get(S.ICON_CONTRACT),
        work_mode_raw=icon_fields.get(S.ICON_HOME_OFFICE),
        languages_raw=languages,
        tech_tags=extract_detail_tech_tags(content),
        application_method=method,
        application_url=apply_url,
        raw_text=flat_text or None,
    )

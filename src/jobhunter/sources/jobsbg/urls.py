"""Jobs.bg URL construction.

Every parameter here was verified against the live site during development;
see ARCHITECTURE.md for how the contract was derived.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

BASE_URL = "https://www.jobs.bg"
SEARCH_PATH = "/front_job_search.php"
JOB_PATH_PREFIX = "/job/"

# Verified category ids. 56 is the aggregated "IT JOBS" section.
CATEGORY_IT = 56

CATEGORIES: dict[str, int] = {
    "it": 56,
    "engineers_technicians": 29,
    "bpo_ito": 17,
    "accounting_finance": 41,
    "administration": 38,
}

# Verified city -> location_sid map, read from the site's own location picker.
LOCATION_SIDS: dict[str, int] = {
    "софия": 1,
    "sofia": 1,
    "пловдив": 2,
    "plovdiv": 2,
    "варна": 3,
    "varna": 3,
    "бургас": 4,
    "burgas": 4,
    "благоевград": 6,
    "blagoevgrad": 6,
    "велико търново": 7,
    "veliko tarnovo": 7,
    "враца": 9,
    "vratsa": 9,
    "габрово": 10,
    "gabrovo": 10,
    "ловеч": 14,
    "lovech": 14,
    "пазарджик": 16,
    "pazardzhik": 16,
    "перник": 17,
    "pernik": 17,
    "плевен": 18,
    "pleven": 18,
    "русе": 19,
    "ruse": 19,
    "сливен": 20,
    "sliven": 20,
    "стара загора": 21,
    "stara zagora": 21,
    "шумен": 23,
    "shumen": 23,
    "разград": 34,
    "razgrad": 34,
    "дупница": 35,
    "dupnitsa": 35,
    "търговище": 40,
    "targovishte": 40,
    "елин пелин": 51,
    "elin pelin": 51,
    "казанлък": 55,
    "kazanlak": 55,
}


def resolve_location_sid(location: str) -> int | None:
    """Map a human city name to the site's location id. Case/space insensitive."""
    return LOCATION_SIDS.get(location.strip().lower())


def build_search_url(
    *,
    location: str | None = None,
    location_sid: int | None = None,
    category: int | None = CATEGORY_IT,
    keywords: str | None = None,
    entry_level_only: bool = False,
    page: int = 1,
) -> str:
    """Build a Jobs.bg search URL.

    ``location`` is resolved to a ``location_sid``; an explicit ``location_sid``
    always wins. Unknown city names are simply omitted rather than guessed.
    """
    params: list[tuple[str, str]] = [("subm", "1")]

    if category is not None:
        params.append(("categories[]", str(category)))

    sid = (
        location_sid
        if location_sid is not None
        else (resolve_location_sid(location) if location else None)
    )
    if sid is not None:
        params.append(("location_sid", str(sid)))

    if entry_level_only:
        params.append(("is_entry_level", "1"))

    if keywords:
        params.append(("keyword", keywords))

    # Verified: results paginate with a 1-indexed ``page`` parameter, 20 per
    # page. ``frompage``/``start``/``offset`` are accepted but ignored.
    if page and page > 1:
        params.append(("page", str(page)))

    query = urlencode(params, quote_via=quote, safe="[]")
    return f"{BASE_URL}{SEARCH_PATH}?{query}"


def build_job_url(job_id: str | int) -> str:
    return f"{BASE_URL}{JOB_PATH_PREFIX}{job_id}"


def extract_job_id(url: str) -> str | None:
    """Pull the numeric listing id out of a job URL."""
    parts = urlsplit(url)
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) >= 2 and segments[-2] == "job" and segments[-1].isdigit():
        return segments[-1]
    if segments and segments[-1].isdigit() and JOB_PATH_PREFIX.strip("/") in parts.path:
        return segments[-1]
    return None


TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "gclid",
        "fbclid",
        "src",
        "ref",
        "csrf_token",
    }
)


def normalize_url(url: str) -> str:
    """Canonical form used for duplicate detection.

    Drops tracking parameters, the fragment, a trailing slash and the ``www.``
    host prefix so that cosmetic variations collapse onto one key.
    """
    if not url:
        return ""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "https"
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]

    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if k not in TRACKING_PARAMS
    ]
    kept.sort()
    query = urlencode(kept)

    path = parts.path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, query, ""))

"""Turn a scraped RawJob into a canonical NormalizedJob.

Also owns fingerprinting, which is what duplicate prevention ultimately rests on.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

from jobhunter.domain.enums import EmploymentType, Language, WorkMode
from jobhunter.domain.schemas import NormalizedJob, RawJob, SalaryInfo
from jobhunter.sources.jobsbg.parser import parse_posted_date, parse_years_experience
from jobhunter.sources.jobsbg.urls import normalize_url

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")
_CYRILLIC = re.compile(r"[Ѐ-ӿ]")
_LATIN = re.compile(r"[A-Za-z]")

# Bulgarian city names -> canonical latin form used for comparisons.
CITY_ALIASES: dict[str, str] = {
    "варна": "Varna",
    "varna": "Varna",
    "софия": "Sofia",
    "sofia": "Sofia",
    "пловдив": "Plovdiv",
    "plovdiv": "Plovdiv",
    "бургас": "Burgas",
    "burgas": "Burgas",
    "русе": "Ruse",
    "ruse": "Ruse",
    "стара загора": "Stara Zagora",
    "велико търново": "Veliko Tarnovo",
    "благоевград": "Blagoevgrad",
    "плевен": "Pleven",
    "шумен": "Shumen",
    "габрово": "Gabrovo",
}

REMOTE_MARKERS = (
    "работа от вкъщи",
    "от разстояние",
    "дистанционна работа",
    "remote",
    "work from home",
    "home office",
)
HYBRID_MARKERS = ("хибрид", "hybrid", "възможност за работа от вкъщи")

EMPLOYMENT_MARKERS: list[tuple[EmploymentType, tuple[str, ...]]] = [
    (EmploymentType.INTERNSHIP, ("стаж", "практика", "internship", "intern")),
    (EmploymentType.PART_TIME, ("непълно работно време", "part time", "part-time", "хоноруван")),
    (EmploymentType.TEMPORARY, ("временна работа", "temporary", "срочен договор")),
    (EmploymentType.CONTRACT, ("граждански договор", "contract", "freelance", "фрийланс")),
    (
        EmploymentType.FULL_TIME,
        ("пълно работно време", "постоянна работа", "full time", "full-time"),
    ),
]

_CURRENCIES = {
    "лв": "BGN",
    "лв.": "BGN",
    "bgn": "BGN",
    "лева": "BGN",
    "eur": "EUR",
    "€": "EUR",
    "евро": "EUR",
    "usd": "USD",
    "$": "USD",
}


def strip_accents(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c))


def normalize_text(value: str | None) -> str:
    """Lowercase, strip punctuation and collapse whitespace."""
    if not value:
        return ""
    lowered = value.lower().strip()
    return _WS.sub(" ", _PUNCT.sub(" ", lowered)).strip()


def normalize_company(name: str | None) -> str:
    """Normalize a company name for duplicate matching.

    Legal-form suffixes vary between listings for the same employer, so they are
    removed before comparison.
    """
    text = normalize_text(name)
    if not text:
        return ""
    text = re.sub(
        r"\b(eood|ood|ad|ead|ltd|llc|inc|gmbh|bulgaria|еоод|оод|ад|еад|бг|bg)\b", " ", text
    )
    return _WS.sub(" ", text).strip()


def detect_language(*parts: str | None) -> Language:
    """Classify text as Bulgarian or English by script ratio."""
    text = " ".join(p for p in parts if p)
    if not text:
        return Language.UNKNOWN
    cyr = len(_CYRILLIC.findall(text))
    lat = len(_LATIN.findall(text))
    if cyr == 0 and lat == 0:
        return Language.UNKNOWN
    if cyr > lat * 0.30:
        return Language.BG
    return Language.EN


def extract_city(location_raw: str | None) -> str | None:
    """Pull a canonical city out of a free-form location string."""
    if not location_raw:
        return None
    head = re.split(r"[;,(]", location_raw)[0]
    normalized = normalize_text(head)
    if canonical := CITY_ALIASES.get(normalized):
        return canonical
    full = normalize_text(location_raw)
    for alias, canonical in CITY_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", full):
            return canonical
    return head.strip() or None


def detect_work_mode(*parts: str | None) -> WorkMode:
    text = " ".join(p.lower() for p in parts if p)
    if not text:
        return WorkMode.UNKNOWN
    has_remote = any(m in text for m in REMOTE_MARKERS)
    has_hybrid = any(m in text for m in HYBRID_MARKERS)
    if has_hybrid:
        return WorkMode.HYBRID
    if has_remote:
        return WorkMode.REMOTE
    return WorkMode.ONSITE if text.strip() else WorkMode.UNKNOWN


def detect_employment_type(*parts: str | None) -> EmploymentType:
    text = " ".join(p.lower() for p in parts if p)
    if not text:
        return EmploymentType.UNKNOWN
    for employment, markers in EMPLOYMENT_MARKERS:
        if any(m in text for m in markers):
            return employment
    return EmploymentType.UNKNOWN


def parse_salary(raw: str | None) -> SalaryInfo:
    """Parse salary strings such as ``от 2000 до 3000 лв.`` or ``2500 EUR``."""
    if not raw:
        return SalaryInfo()
    text = raw.strip()
    lowered = text.lower()

    currency = None
    for token, code in _CURRENCIES.items():
        if token in lowered:
            currency = code
            break

    period = None
    if re.search(r"month|месеч|месец|мес\.", lowered):
        period = "month"
    elif re.search(r"year|годиш|год\.|annual", lowered):
        period = "year"
    elif re.search(r"hour|час", lowered):
        period = "hour"

    numbers = [
        float(n.replace(" ", "").replace(" ", "").replace(",", "."))
        for n in re.findall(r"\d[\d\s ]*(?:[.,]\d+)?", text)
    ]
    numbers = [n for n in numbers if n >= 100]

    minimum = maximum = None
    if len(numbers) >= 2:
        minimum, maximum = min(numbers[:2]), max(numbers[:2])
    elif len(numbers) == 1:
        if re.search(r"\bдо\b|\bup to\b", lowered):
            maximum = numbers[0]
        else:
            minimum = numbers[0]

    return SalaryInfo(minimum=minimum, maximum=maximum, currency=currency, period=period, raw=text)


def compute_fingerprint(
    *, source: str, source_job_id: str | None, normalized_url: str, company: str | None, title: str
) -> str:
    """Stable internal id.

    Prefers the site's own listing id, then the canonical URL, and only then a
    company+title hash, so the same posting keeps one identity across scans.
    """
    if source_job_id:
        basis = f"{source}:id:{source_job_id}"
    elif normalized_url:
        basis = f"{source}:url:{normalized_url}"
    else:
        basis = f"{source}:ct:{normalize_company(company)}:{normalize_text(title)}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def content_fingerprint(company: str | None, title: str) -> str:
    """Secondary key catching the same role re-posted under a new listing id."""
    basis = f"{normalize_company(company)}|{normalize_text(title)}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def hash_description(description: str | None) -> str | None:
    if not description:
        return None
    return hashlib.sha256(normalize_text(description).encode("utf-8")).hexdigest()[:32]


def normalize_job(raw: RawJob) -> NormalizedJob:
    """Canonicalize a scraped listing."""
    canonical_url = normalize_url(raw.source_url)
    posted_at = parse_posted_date(raw.posted_at_raw)

    level = raw.level_raw
    experience = raw.experience_raw
    years = parse_years_experience(experience)

    work_mode = detect_work_mode(raw.work_mode_raw, raw.location_raw, raw.description)
    employment = detect_employment_type(raw.employment_raw, raw.description)

    tech = [t.strip() for t in raw.tech_tags if t and t.strip()]
    tech = list(dict.fromkeys(tech))

    return NormalizedJob(
        fingerprint=compute_fingerprint(
            source=raw.source,
            source_job_id=raw.source_job_id,
            normalized_url=canonical_url,
            company=raw.company_name,
            title=raw.title,
        ),
        source=raw.source,
        source_job_id=raw.source_job_id,
        source_url=raw.source_url,
        normalized_url=canonical_url,
        title=raw.title.strip(),
        title_normalized=normalize_text(raw.title),
        company_name=raw.company_name.strip() if raw.company_name else None,
        company_source_id=raw.company_source_id,
        description=raw.description,
        description_hash=hash_description(raw.description),
        location_raw=raw.location_raw,
        city=extract_city(raw.location_raw),
        work_mode=work_mode,
        salary=parse_salary(raw.salary_raw),
        employment_type=employment,
        posted_at=posted_at,
        posted_at_raw=raw.posted_at_raw,
        tech_keywords=tech,
        languages=list(raw.languages_raw),
        level_raw=level,
        experience_raw=experience,
        years_experience_required=years,
        application_method=raw.application_method,
        application_url=raw.application_url,
        language=detect_language(raw.title, raw.description),
    )

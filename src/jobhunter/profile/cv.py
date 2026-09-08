"""CV file registry and local text extraction.

CV text is extracted and stored locally only. Nothing here uploads a CV; the
browser layer attaches the file directly to the Jobs.bg form when applying.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobhunter.db.models import CVFile
from jobhunter.domain.enums import Language
from jobhunter.logging_setup import get_logger
from jobhunter.profile.context import cv_has_placeholders

log = get_logger(__name__)

SUPPORTED_SUFFIXES = frozenset({".pdf", ".docx", ".doc", ".txt", ".odt", ".rtf"})
_CYRILLIC = re.compile(r"[Ѐ-ӿ]")


class CVExtractionError(RuntimeError):
    """Raised when a CV file exists but its text cannot be read."""


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()[:32]


def extract_pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    parts = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception as exc:  # a single bad page must not lose the whole CV
            log.warning("cv_page_extract_failed", path=str(path), error=str(exc))
    return "\n".join(parts).strip()


def extract_docx_text(path: Path) -> str:
    import docx

    document = docx.Document(str(path))
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.extend(cell.text for cell in row.cells)
    return "\n".join(p for p in parts if p and p.strip()).strip()


def extract_text(path: Path) -> str:
    """Extract plain text from a CV, dispatching on suffix."""
    if not path.exists():
        raise CVExtractionError(f"CV file not found: {path}")
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            return extract_pdf_text(path)
        if suffix == ".docx":
            return extract_docx_text(path)
        if suffix in {".txt", ".rtf"}:
            return path.read_text(encoding="utf-8", errors="replace").strip()
    except CVExtractionError:
        raise
    except Exception as exc:
        raise CVExtractionError(f"Could not extract text from {path.name}: {exc}") from exc
    raise CVExtractionError(f"Unsupported CV format: {suffix}")


def detect_cv_language(text: str, filename: str = "") -> Language:
    """Infer CV language from its script, with a filename hint as a tiebreaker."""
    name = filename.lower()
    if re.search(r"[_\-. ](bg|bul)[_\-. ]|_bg\.|bg\.pdf|българ", name):
        return Language.BG
    if re.search(r"[_\-. ](en|eng)[_\-. ]|_en\.|en\.pdf", name):
        return Language.EN
    if not text:
        return Language.UNKNOWN
    cyrillic = len(_CYRILLIC.findall(text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if cyrillic == 0 and latin == 0:
        return Language.UNKNOWN
    return Language.BG if cyrillic > latin * 0.30 else Language.EN


def register_cv(
    session: Session,
    path: str | Path,
    *,
    description: str | None = None,
    version: str = "1",
    is_default: bool = False,
    language: Language | None = None,
) -> CVFile:
    """Add or refresh a CV in the registry, extracting its text."""
    resolved = Path(path).expanduser().resolve()
    existing = session.scalar(select(CVFile).where(CVFile.path == str(resolved)))

    text = ""
    error: str | None = None
    try:
        text = extract_text(resolved)
    except CVExtractionError as exc:
        error = str(exc)
        log.warning("cv_extract_failed", path=str(resolved), error=error)

    detected = language or detect_cv_language(text, resolved.name)
    available = resolved.exists()

    record = existing or CVFile(path=str(resolved))
    record.filename = resolved.name
    record.language = detected
    record.version = version
    record.description = description or record.description
    record.is_available = available
    record.extracted_text = text or None
    record.text_extracted_at = datetime.now(UTC) if text else None
    if available:
        record.file_size = resolved.stat().st_size
        record.file_hash = file_hash(resolved)

    if existing is None:
        session.add(record)
    session.flush()

    if is_default:
        set_default_cv(session, record.id)
    return record


def set_default_cv(session: Session, cv_id: int) -> None:
    """Mark exactly one CV as the default."""
    for cv in session.scalars(select(CVFile)).all():
        cv.is_default = cv.id == cv_id
    session.flush()


def list_cvs(session: Session) -> list[CVFile]:
    return list(session.scalars(select(CVFile).order_by(CVFile.id)).all())


def select_cv_for_job(
    session: Session,
    *,
    job_language: Language = Language.UNKNOWN,
) -> CVFile | None:
    """Pick the best CV for a listing.

    Prefers a CV in the job's language, then the default, then any available one.

    A CV still containing template placeholders is excluded outright. The shipped
    ``CV_IT_Junior_BG.pdf`` is an untouched template whose name is literally
    "[Име Фамилия]"; sending that to an employer is worse than sending nothing,
    and language-matching would otherwise pick it for every Bulgarian listing.
    """
    candidates = [cv for cv in list_cvs(session) if cv.is_available]
    usable = [cv for cv in candidates if not cv_has_placeholders(cv.extracted_text)]

    for rejected in set(candidates) - set(usable):
        log.warning(
            "cv_rejected_unfilled_template",
            cv_id=rejected.id,
            filename=rejected.filename,
            markers=cv_has_placeholders(rejected.extracted_text),
        )

    candidates = usable
    if not candidates:
        return None

    if job_language in (Language.BG, Language.EN):
        matching = [cv for cv in candidates if cv.language == job_language]
        if matching:
            matching.sort(key=lambda c: (not c.is_default, c.id))
            return matching[0]

    default = next((cv for cv in candidates if cv.is_default), None)
    if default is not None:
        return default
    return candidates[0]


def discover_cv_files(search_dirs: list[Path], *, limit: int = 25) -> list[Path]:
    """Find likely CV files on disk, for the first-run setup flow."""
    pattern = re.compile(r"cv|resume|автобиограф|curriculum", re.IGNORECASE)
    found: list[Path] = []
    for directory in search_dirs:
        if not directory.exists() or not directory.is_dir():
            continue
        try:
            for entry in sorted(directory.iterdir()):
                if len(found) >= limit:
                    return found
                if (
                    entry.is_file()
                    and entry.suffix.lower() in SUPPORTED_SUFFIXES
                    and pattern.search(entry.name)
                ):
                    found.append(entry.resolve())
        except PermissionError:
            continue
    return found

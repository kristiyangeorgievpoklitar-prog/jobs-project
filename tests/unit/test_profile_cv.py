"""CV handling and profile extraction."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobhunter.domain.enums import Language
from jobhunter.profile.cv import (
    CVExtractionError,
    detect_cv_language,
    discover_cv_files,
    extract_text,
    list_cvs,
    register_cv,
    select_cv_for_job,
    set_default_cv,
)
from jobhunter.profile.profile_store import (
    bootstrap_profile_from_cv,
    extract_profile_fields,
    get_active_profile,
    to_snapshot,
    update_profile,
)

SAMPLE_CV = """Ivan Petrov
Varna, Bulgaria  |  [+359 888123456]  |  ivan.petrov\\@example.com  |  GitHub: https://github.com/ivanp
SUMMARY
Computer Science student with hands-on experience in web development.
EDUCATION
University of Economics - Varna
Bachelor in Informatics
SKILLS
Backend: PHP, Laravel, MySQL
Frontend: JavaScript, HTML, CSS, Tailwind CSS
Development: Git, GitHub
LANGUAGES
- Bulgarian - Native - English - Professional working proficiency
"""


class TestExtractText:
    def test_reads_txt(self, tmp_path: Path) -> None:
        path = tmp_path / "cv.txt"
        path.write_text(SAMPLE_CV, encoding="utf-8")
        assert "Ivan Petrov" in extract_text(path)

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(CVExtractionError):
            extract_text(tmp_path / "nope.pdf")

    def test_unsupported_format_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "cv.xyz"
        path.write_text("x")
        with pytest.raises(CVExtractionError):
            extract_text(path)


class TestDetectLanguage:
    def test_from_filename_hint(self) -> None:
        assert detect_cv_language("", "CV_IT_Junior_BG.pdf") is Language.BG
        assert detect_cv_language("", "CV_IT_Junior_EN.pdf") is Language.EN

    def test_from_content(self) -> None:
        assert detect_cv_language("Автобиография на кандидата", "x.pdf") is Language.BG
        assert detect_cv_language("Curriculum vitae of the candidate", "x.pdf") is Language.EN


class TestProfileExtraction:
    def test_pulls_structured_fields(self) -> None:
        data = extract_profile_fields(SAMPLE_CV)
        assert data["full_name"] == "Ivan Petrov"
        assert data["location"] == "Varna"
        assert data["email"] == "ivan.petrov@example.com"  # escaped @ handled
        assert data["github_url"] == "https://github.com/ivanp"
        assert "laravel" in data["frameworks"]
        assert "mysql" in data["databases"]
        assert "php" in data["skills"]
        assert any(lang["name"] == "English" for lang in data["languages"])

    def test_empty_cv_yields_nothing(self) -> None:
        assert extract_profile_fields("") == {}


class TestCVRegistry:
    def test_register_and_default(self, session, tmp_path: Path) -> None:
        path = tmp_path / "cv_en.txt"
        path.write_text(SAMPLE_CV, encoding="utf-8")
        record = register_cv(session, path, is_default=True)
        assert record.is_default
        assert record.extracted_text
        assert record.file_hash
        assert len(list_cvs(session)) == 1

    def test_re_registering_updates_in_place(self, session, tmp_path: Path) -> None:
        path = tmp_path / "cv.txt"
        path.write_text(SAMPLE_CV, encoding="utf-8")
        first = register_cv(session, path)
        path.write_text(SAMPLE_CV + "\nExtra line", encoding="utf-8")
        second = register_cv(session, path)
        assert first.id == second.id
        assert len(list_cvs(session)) == 1

    def test_only_one_default(self, session, tmp_path: Path) -> None:
        a = tmp_path / "a.txt"
        a.write_text("A", encoding="utf-8")
        b = tmp_path / "b.txt"
        b.write_text("B", encoding="utf-8")
        first = register_cv(session, a, is_default=True)
        second = register_cv(session, b, is_default=True)
        set_default_cv(session, second.id)
        defaults = [c.id for c in list_cvs(session) if c.is_default]
        assert defaults == [second.id]
        assert first.is_default is False

    def test_selects_cv_matching_job_language(self, session, tmp_path: Path) -> None:
        en = tmp_path / "cv_en.txt"
        en.write_text("English CV content", encoding="utf-8")
        bg = tmp_path / "cv_bg.txt"
        bg.write_text("Български автобиография", encoding="utf-8")
        register_cv(session, en, is_default=True, language=Language.EN)
        bg_record = register_cv(session, bg, language=Language.BG)
        chosen = select_cv_for_job(session, job_language=Language.BG)
        assert chosen is not None and chosen.id == bg_record.id

    def test_falls_back_to_default(self, session, tmp_path: Path) -> None:
        en = tmp_path / "cv_en.txt"
        en.write_text("English CV", encoding="utf-8")
        record = register_cv(session, en, is_default=True, language=Language.EN)
        chosen = select_cv_for_job(session, job_language=Language.BG)
        assert chosen is not None and chosen.id == record.id

    def test_none_when_registry_empty(self, session) -> None:
        assert select_cv_for_job(session) is None

    def test_missing_file_is_recorded_unavailable(self, session, tmp_path: Path) -> None:
        record = register_cv(session, tmp_path / "ghost.pdf")
        assert record.is_available is False

    def test_discovery_finds_cv_like_names(self, tmp_path: Path) -> None:
        (tmp_path / "My CV.pdf").write_bytes(b"%PDF-1.4")
        (tmp_path / "resume.docx").write_bytes(b"x")
        (tmp_path / "notes.txt").write_text("x")
        found = {p.name for p in discover_cv_files([tmp_path])}
        assert found == {"My CV.pdf", "resume.docx"}


class TestProfileStore:
    def test_bootstrap_fills_blanks_only(self, session) -> None:
        update_profile(session, {"full_name": "Existing Name"})
        bootstrap_profile_from_cv(session, SAMPLE_CV)
        profile = get_active_profile(session)
        assert profile is not None
        assert profile.full_name == "Existing Name"  # not overwritten
        assert profile.location == "Varna"  # was blank, so filled

    def test_update_bumps_version(self, session) -> None:
        first = update_profile(session, {"full_name": "A"}).version
        second = update_profile(session, {"full_name": "B"}).version
        assert second > first

    def test_no_change_does_not_bump_version(self, session) -> None:
        first = update_profile(session, {"full_name": "A"}).version
        second = update_profile(session, {"full_name": "A"}).version
        assert first == second

    def test_snapshot_of_missing_profile_is_safe(self) -> None:
        snapshot = to_snapshot(None)
        assert snapshot.all_tech == set()

    def test_snapshot_collects_all_tech(self, session) -> None:
        update_profile(
            session, {"skills": ["PHP"], "frameworks": ["Laravel"], "databases": ["MySQL"]}
        )
        snapshot = to_snapshot(get_active_profile(session))
        assert snapshot.all_tech == {"php", "laravel", "mysql"}

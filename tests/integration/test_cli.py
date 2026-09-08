"""CLI commands, exercised through Typer's runner."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from jobhunter import cli as cli_module
from jobhunter.context import AppContext
from jobhunter.domain.enums import JobState, Recommendation
from jobhunter.domain.schemas import MatchResult, ScanStats
from jobhunter.pipeline.repository import record_match, upsert_job
from jobhunter.profile.profile_store import update_profile
from tests.conftest import make_job

runner = CliRunner()


@pytest.fixture
def cli_env(settings, tmp_path, monkeypatch):
    """Point every CLI command at a temporary database."""
    db_path = tmp_path / "cli.db"
    settings.database_url = f"sqlite:///{db_path}"

    context = AppContext(settings, configure_logs=False)
    context.db.create_all()

    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    monkeypatch.setattr(cli_module, "AppContext", lambda *a, **k: context)
    monkeypatch.setattr(cli_module, "_run_migrations", lambda: None)
    return context


def seed(context: AppContext) -> int:
    with context.session() as session:
        update_profile(
            session,
            {
                "full_name": "Test Candidate",
                "location": "Varna",
                "skills": ["php"],
                "preferred_locations": ["Varna"],
            },
        )
        job, _ = upsert_job(session, make_job())
        record_match(
            session,
            job,
            MatchResult(score=88, recommendation=Recommendation.REVIEW, strengths=["PHP"]),
        )
        job.state = JobState.REVIEW
        return job.id


class TestInspectionCommands:
    def test_help(self) -> None:
        result = runner.invoke(cli_module.app, ["--help"])
        assert result.exit_code == 0
        assert "scan" in result.output

    def test_doctor_reports_readiness(self, cli_env, monkeypatch) -> None:
        # The Chromium probe shells out; stub it so the test stays offline.
        class FakeCompleted:
            stdout = "/nonexistent/chrome"

        import subprocess

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeCompleted())
        result = runner.invoke(cli_module.app, ["doctor"])
        assert result.exit_code == 0
        assert "Profile" in result.output
        assert "Auto-apply" in result.output

    def test_jobs_lists_stored_rows(self, cli_env) -> None:
        seed(cli_env)
        result = runner.invoke(cli_module.app, ["jobs"])
        assert result.exit_code == 0
        assert "Junior PHP" in result.output

    def test_jobs_min_score_filter_excludes(self, cli_env) -> None:
        seed(cli_env)
        result = runner.invoke(cli_module.app, ["jobs", "--min-score", "95"])
        assert result.exit_code == 0
        assert "Junior PHP" not in result.output

    def test_jobs_state_filter(self, cli_env) -> None:
        seed(cli_env)
        result = runner.invoke(cli_module.app, ["jobs", "--state", "review"])
        assert result.exit_code == 0
        assert "Junior PHP" in result.output

    def test_runs_command(self, cli_env) -> None:
        assert runner.invoke(cli_module.app, ["runs"]).exit_code == 0

    def test_applications_command(self, cli_env) -> None:
        assert runner.invoke(cli_module.app, ["applications"]).exit_code == 0


class TestProfileCommands:
    def test_show(self, cli_env) -> None:
        seed(cli_env)
        result = runner.invoke(cli_module.app, ["profile", "show"])
        assert result.exit_code == 0
        assert "Test Candidate" in result.output

    def test_set_scalar(self, cli_env) -> None:
        result = runner.invoke(cli_module.app, ["profile", "set", "years_experience", "2.5"])
        assert result.exit_code == 0
        from jobhunter.profile.profile_store import get_active_profile

        with cli_env.session() as session:
            assert get_active_profile(session).years_experience == 2.5

    def test_set_list_from_json(self, cli_env) -> None:
        result = runner.invoke(
            cli_module.app, ["profile", "set", "preferred_locations", '["Varna", "Sofia"]']
        )
        assert result.exit_code == 0
        from jobhunter.profile.profile_store import get_active_profile

        with cli_env.session() as session:
            assert get_active_profile(session).preferred_locations == ["Varna", "Sofia"]

    def test_set_boolean(self, cli_env) -> None:
        runner.invoke(cli_module.app, ["profile", "set", "remote_ok", "false"])
        from jobhunter.profile.profile_store import get_active_profile

        with cli_env.session() as session:
            assert get_active_profile(session).remote_ok is False


class TestCVCommands:
    def test_add_and_list(self, cli_env, tmp_path: Path) -> None:
        cv = tmp_path / "cv.txt"
        cv.write_text("Ivan Petrov\nSKILLS\nPHP, Laravel", encoding="utf-8")

        add = runner.invoke(cli_module.app, ["cv", "add", str(cv), "--default"])
        assert add.exit_code == 0
        assert "Registered" in add.output

        listing = runner.invoke(cli_module.app, ["cv", "list"])
        assert "cv.txt" in listing.output

    def test_add_missing_file_fails_cleanly(self, cli_env, tmp_path: Path) -> None:
        result = runner.invoke(cli_module.app, ["cv", "add", str(tmp_path / "ghost.pdf")])
        # Registered as unavailable rather than crashing.
        assert result.exit_code == 0


class TestScanCommand:
    def test_scan_reports_stats(self, cli_env, monkeypatch) -> None:
        class FakePipeline:
            def __init__(self, context: Any) -> None:
                pass

            def run(self, options: Any) -> ScanStats:
                return ScanStats(jobs_seen=7, jobs_new=3, jobs_matched=7)

        monkeypatch.setattr(cli_module, "ScanPipeline", FakePipeline)
        result = runner.invoke(cli_module.app, ["scan", "--limit", "10"])
        assert result.exit_code == 0
        assert "Jobs Seen" in result.output


class TestApplyCommand:
    def test_reports_a_manual_step(self, cli_env, monkeypatch) -> None:
        job_id = seed(cli_env)
        from jobhunter.domain.schemas import ApplicationOutcome

        class FakeOrchestrator:
            def __init__(self, context: Any) -> None:
                pass

            def apply_to_job(self, job_id: int, *, submit: bool = False) -> ApplicationOutcome:
                return ApplicationOutcome(
                    success=False, requires_manual_step=True, failure_reason="employer questions"
                )

        monkeypatch.setattr(
            "jobhunter.applications.orchestrator.ApplicationOrchestrator", FakeOrchestrator
        )
        result = runner.invoke(cli_module.app, ["apply", str(job_id)])
        assert result.exit_code == 0
        assert "Manual step required" in result.output

    def test_reports_success(self, cli_env, monkeypatch) -> None:
        job_id = seed(cli_env)
        from jobhunter.domain.schemas import ApplicationOutcome

        class FakeOrchestrator:
            def __init__(self, context: Any) -> None:
                pass

            def apply_to_job(self, job_id: int, *, submit: bool = False) -> ApplicationOutcome:
                return ApplicationOutcome(success=True, evidence="confirmation shown")

        monkeypatch.setattr(
            "jobhunter.applications.orchestrator.ApplicationOrchestrator", FakeOrchestrator
        )
        result = runner.invoke(cli_module.app, ["apply", str(job_id), "--submit"])
        assert "Application submitted" in result.output

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
    @staticmethod
    def _install(monkeypatch, *, model_available: bool = True) -> None:
        class FakePipeline:
            def __init__(self, context: Any) -> None:
                pass

            def run(self, options: Any) -> ScanStats:
                return ScanStats(jobs_seen=7, jobs_new=3, jobs_matched=7)

        monkeypatch.setattr(cli_module, "ScanPipeline", FakePipeline)
        monkeypatch.setattr(
            "jobhunter.ai.local_model.LocalModelProvider.is_available",
            lambda self: model_available,
        )

    def test_scan_reports_stats(self, cli_env, monkeypatch) -> None:
        self._install(monkeypatch)
        result = runner.invoke(cli_module.app, ["scan", "--limit", "10"])
        assert result.exit_code == 0
        assert "Jobs Seen" in result.output

    def test_scan_refuses_to_run_without_the_model_it_is_configured_to_use(
        self, cli_env, monkeypatch
    ) -> None:
        """Scanning without the matcher would fill the database with non-decisions."""
        self._install(monkeypatch, model_available=False)
        result = runner.invoke(cli_module.app, ["scan"])
        assert result.exit_code == 1
        assert "not reachable" in result.output
        assert "ollama serve" in result.output


class TestTodayScanCommand:
    """The command reports; it never applies, and its exit code says what happened."""

    @staticmethod
    def _outcome(job_id: int, title: str, decision: str, *, is_new: bool):
        from jobhunter.domain.evaluation import Decision, JobEvaluation, RequirementAssessment
        from jobhunter.pipeline.runner import JobOutcome

        return JobOutcome(
            job_id=job_id,
            title=title,
            company="Example EOOD",
            location="Варна",
            source_url=f"https://www.jobs.bg/job/{job_id}",
            evaluation=JobEvaluation(
                decision=Decision(decision),
                confidence=0.8,
                major_strengths=["PHP matches"],
                major_risks=["Small team"],
                mandatory_requirements=[
                    RequirementAssessment(requirement="PHP", candidate_fit="strong")
                ],
                source="local",
            ),
            is_new=is_new,
        )

    def _install(self, monkeypatch, outcomes, *, status: str = "succeeded"):
        from datetime import date

        from jobhunter.domain.enums import RunStatus
        from jobhunter.domain.schemas import ScanStats
        from jobhunter.today import TodayScanResult

        result = TodayScanResult(
            day=date(2026, 9, 9),
            location="Varna",
            stats=ScanStats(jobs_seen=len(outcomes)),
            status=RunStatus(status),
            outcomes=list(outcomes),
            notified=any(o.is_new for o in outcomes),
        )
        monkeypatch.setattr("jobhunter.today.run_today_scan", lambda *a, **k: result)
        monkeypatch.setattr(
            "jobhunter.ai.local_model.LocalModelProvider.is_available", lambda self: True
        )
        return result

    def test_lists_todays_findings_with_their_decisions(self, cli_env, monkeypatch) -> None:
        seed(cli_env)
        self._install(
            monkeypatch,
            [
                self._outcome(1, "Junior PHP Developer", "apply", is_new=True),
                self._outcome(2, "Junior QA Engineer", "review", is_new=False),
                self._outcome(3, "Senior Java Architect", "skip", is_new=False),
            ],
        )
        result = runner.invoke(cli_module.app, ["today-scan"])

        assert result.exit_code == 0
        assert "Junior PHP Developer" in result.output
        assert "APPLY" in result.output and "REVIEW" in result.output and "SKIP" in result.output
        assert "already seen" in result.output
        assert "must have: PHP" in result.output
        assert "Nothing was submitted" in result.output

    def test_nothing_new_exits_one(self, cli_env, monkeypatch) -> None:
        seed(cli_env)
        self._install(
            monkeypatch, [self._outcome(1, "Junior PHP Developer", "review", is_new=False)]
        )
        assert runner.invoke(cli_module.app, ["today-scan"]).exit_code == 1

    def test_an_empty_day_exits_one(self, cli_env, monkeypatch) -> None:
        seed(cli_env)
        self._install(monkeypatch, [])
        result = runner.invoke(cli_module.app, ["today-scan"])
        assert result.exit_code == 1
        assert "Nothing has been published today yet." in result.output

    def test_a_blocked_scan_exits_two(self, cli_env, monkeypatch) -> None:
        seed(cli_env)
        self._install(monkeypatch, [], status="blocked")
        result = runner.invoke(cli_module.app, ["today-scan"])
        assert result.exit_code == 2
        assert "blocked" in result.output

    def test_refuses_to_run_without_the_model_it_is_configured_to_use(
        self, cli_env, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "jobhunter.ai.local_model.LocalModelProvider.is_available", lambda self: False
        )
        result = runner.invoke(cli_module.app, ["today-scan"])
        assert result.exit_code == 2
        assert "not reachable" in result.output

    def test_a_malformed_date_is_refused(self, cli_env, monkeypatch) -> None:
        monkeypatch.setattr(
            "jobhunter.ai.local_model.LocalModelProvider.is_available", lambda self: True
        )
        result = runner.invoke(cli_module.app, ["today-scan", "--date", "yesterday"])
        assert result.exit_code == 2
        assert "not a date" in result.output

    def test_today_still_works_and_points_at_the_scan(self, cli_env) -> None:
        seed(cli_env)
        result = runner.invoke(cli_module.app, ["today"])
        assert result.exit_code == 0
        assert "today-scan" in result.output


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

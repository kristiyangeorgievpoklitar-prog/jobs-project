"""Command line interface."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import desc, func, select

from jobhunter.config import PROJECT_ROOT, get_settings
from jobhunter.context import AppContext
from jobhunter.db.models import Application, AutomationRun, CVFile, Job, JobMatch
from jobhunter.domain.enums import JobState, RunTrigger
from jobhunter.pipeline.runner import ScanOptions, ScanPipeline
from jobhunter.profile.cv import discover_cv_files, list_cvs, register_cv, set_default_cv
from jobhunter.profile.profile_store import (
    bootstrap_profile_from_cv,
    get_active_profile,
    get_or_create_profile,
    update_profile,
)

app = typer.Typer(
    help="JobHunter - autonomous AI job hunting agent for Jobs.bg",
    no_args_is_help=True,
    add_completion=False,
)
profile_app = typer.Typer(help="Manage the candidate profile", no_args_is_help=True)
cv_app = typer.Typer(help="Manage CV files", no_args_is_help=True)
app.add_typer(profile_app, name="profile")
app.add_typer(cv_app, name="cv")

console = Console()


def _run_migrations() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "src/jobhunter/db/migrations"))
    command.upgrade(cfg, "head")


# ----------------------------------------------------------------- init ---


@app.command()
def init(
    cv: list[Path] = typer.Option(None, "--cv", help="CV file to register (repeatable)"),
    skip_discovery: bool = typer.Option(False, help="Do not search the machine for CVs"),
) -> None:
    """Create the database, register CVs and bootstrap the profile from a CV."""
    settings = get_settings()
    settings.ensure_directories()
    console.print("[bold]Setting up JobHunter[/bold]")

    _run_migrations()
    console.print("  [green]OK[/green] database schema up to date")

    context = AppContext(settings)
    paths: list[Path] = list(cv or [])

    if not paths and not skip_discovery:
        candidates = discover_cv_files(
            [Path.home() / "Documents", Path.home() / "Downloads", Path.home() / "Desktop"]
        )
        if candidates:
            console.print(f"  Found {len(candidates)} possible CV file(s):")
            for index, path in enumerate(candidates, start=1):
                console.print(f"    {index}. {path}")
            paths = candidates

    with context.session() as session:
        for index, path in enumerate(paths):
            try:
                record = register_cv(session, path, is_default=(index == 0))
                console.print(
                    f"  [green]OK[/green] registered CV {record.filename} "
                    f"(language={record.language.value}, {len(record.extracted_text or '')} chars)"
                )
            except Exception as exc:
                console.print(f"  [yellow]![/yellow] could not register {path}: {exc}")

        profile = get_or_create_profile(session)
        default_cv = next((c for c in list_cvs(session) if c.is_default), None)
        if default_cv is not None and default_cv.extracted_text:
            bootstrap_profile_from_cv(session, default_cv.extracted_text)
            session.refresh(profile)
            console.print(
                f"  [green]OK[/green] profile bootstrapped: {profile.full_name or '(name not found)'}"
            )

        if not profile.preferred_locations:
            update_profile(session, {"preferred_locations": [settings.search_location]})

    console.print("\n[bold green]Setup complete.[/bold green]")
    console.print("Next: [cyan]uv run jobhunter serve[/cyan] then open http://127.0.0.1:8000")
    context.close()


# --------------------------------------------------------------- doctor ---


@app.command()
def doctor() -> None:
    """Check that the environment is ready to run."""
    import sys

    settings = get_settings()
    table = Table(title="JobHunter environment check")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")

    def row(name: str, ok: bool | None, detail: str) -> None:
        mark = (
            "[green]OK[/green]"
            if ok
            else ("[yellow]WARN[/yellow]" if ok is None else "[red]FAIL[/red]")
        )
        table.add_row(name, mark, detail)

    row("Python", sys.version_info >= (3, 12), sys.version.split()[0])

    # Run the browser probe in a subprocess: tearing the Playwright driver down
    # in-process prints noisy asyncio warnings that would clutter this report.
    import subprocess

    probe = (
        "from playwright.sync_api import sync_playwright;"
        "p=sync_playwright().start();print(p.chromium.executable_path);p.stop()"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, timeout=90
        )
        chromium_path = (
            Path(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else None
        )
        if chromium_path is not None and chromium_path.exists():
            row("Chromium", True, str(chromium_path))
        else:
            row("Chromium", False, "not installed - run: uv run playwright install chromium")
    except Exception as exc:
        row("Chromium", False, str(exc)[:60])

    context = AppContext(settings, configure_logs=False)
    try:
        with context.session() as session:
            jobs = session.scalar(select(func.count()).select_from(Job)) or 0
            cvs = session.scalar(select(func.count()).select_from(CVFile)) or 0
            profile = get_active_profile(session)
        row("Database", True, f"{settings.resolved_database_url} ({jobs} jobs)")
        row("CV files", cvs > 0, f"{cvs} registered")
        row(
            "Profile",
            profile is not None and bool(profile.full_name),
            (profile.full_name if profile else "not created") or "no name set",
        )
    except Exception as exc:
        row("Database", False, str(exc)[:70])

    row("AI provider", True, context.provider.describe())
    row(
        "Auto-apply",
        None if settings.auto_apply else True,
        "ENABLED - applications may be submitted"
        if settings.auto_apply
        else "disabled (review mode)",
    )
    row(
        "Thresholds",
        settings.thresholds_are_sane(),
        f"apply>={settings.auto_apply_threshold}, review>={settings.review_threshold}",
    )
    row("Browser mode", True, "headless" if settings.browser_headless else "headed (recommended)")

    console.print(table)
    context.close()


# ----------------------------------------------------------------- scan ---


@app.command()
def scan(
    location: str = typer.Option(None, help="Override the search location"),
    keywords: str = typer.Option(None, help="Extra search keywords"),
    limit: int = typer.Option(None, help="Maximum jobs to process"),
    enrich: int = typer.Option(None, help="Maximum detail pages to fetch"),
    entry_level: bool = typer.Option(False, help="Use the site's entry-level filter"),
) -> None:
    """Run one discovery + scoring pass."""
    context = AppContext()
    pipeline = ScanPipeline(context)
    stats = pipeline.run(
        ScanOptions(
            location=location,
            keywords=keywords,
            max_results=limit,
            enrich_limit=enrich,
            entry_level_only=entry_level,
            trigger=RunTrigger.MANUAL,
        )
    )
    table = Table(title="Scan results")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    for key, value in stats.model_dump(exclude={"notes"}).items():
        table.add_row(key.replace("_", " ").title(), str(value))
    console.print(table)
    for note in stats.notes:
        console.print(f"[yellow]{note}[/yellow]")
    context.close()


# ----------------------------------------------------------------- jobs ---


@app.command("jobs")
def list_jobs(
    state: str = typer.Option(None, help="Filter by state"),
    limit: int = typer.Option(25, help="Rows to show"),
    min_score: int = typer.Option(0, help="Minimum match score"),
) -> None:
    """List stored jobs with their latest score."""
    context = AppContext(configure_logs=False)
    with context.session() as session:
        stmt = select(Job).order_by(desc(Job.last_seen_at))
        if state:
            stmt = stmt.where(Job.state == JobState(state))
        rows = []
        for job in session.scalars(stmt).all():
            match = session.scalar(
                select(JobMatch)
                .where(JobMatch.job_id == job.id)
                .order_by(desc(JobMatch.id))
                .limit(1)
            )
            score = match.score if match else 0
            if score < min_score:
                continue
            rows.append((score, job, match))
            if len(rows) >= limit:
                break

        table = Table(title=f"Jobs ({len(rows)})")
        table.add_column("ID", justify="right")
        table.add_column("Score", justify="right")
        table.add_column("Title", max_width=42)
        table.add_column("Company", max_width=24)
        table.add_column("City")
        table.add_column("Seniority")
        table.add_column("State")
        for score, job, _match in rows:
            colour = "green" if score >= 75 else ("yellow" if score >= 50 else "white")
            table.add_row(
                str(job.id),
                f"[{colour}]{score}[/{colour}]",
                job.title,
                job.company_display,
                job.city or "-",
                str(job.seniority),
                str(job.state),
            )
    console.print(table)
    context.close()


@app.command()
def runs(limit: int = typer.Option(10)) -> None:
    """Show recent automation runs."""
    context = AppContext(configure_logs=False)
    with context.session() as session:
        rows = session.scalars(
            select(AutomationRun).order_by(desc(AutomationRun.id)).limit(limit)
        ).all()
        table = Table(title="Automation runs")
        for col in (
            "ID",
            "Trigger",
            "Status",
            "Seen",
            "New",
            "Matched",
            "Applied",
            "Errors",
            "Started",
        ):
            table.add_column(col)
        for run in rows:
            table.add_row(
                str(run.id),
                str(run.trigger),
                str(run.status),
                str(run.jobs_seen),
                str(run.jobs_new),
                str(run.jobs_matched),
                str(run.applications_submitted),
                str(run.errors_count),
                run.started_at.strftime("%Y-%m-%d %H:%M"),
            )
    console.print(table)
    context.close()


# -------------------------------------------------------------- profile ---


@profile_app.command("show")
def profile_show() -> None:
    """Print the current candidate profile."""
    context = AppContext(configure_logs=False)
    with context.session() as session:
        profile = get_active_profile(session)
        if profile is None:
            console.print("[yellow]No profile yet. Run 'jobhunter init'.[/yellow]")
            return
        table = Table(title=f"Candidate profile (v{profile.version})")
        table.add_column("Field")
        table.add_column("Value")
        for field in (
            "full_name",
            "location",
            "years_experience",
            "desired_seniority",
            "preferred_locations",
            "remote_ok",
            "skills",
            "frameworks",
            "databases",
            "tools",
            "languages",
            "github_url",
            "linkedin_url",
        ):
            value = getattr(profile, field, None)
            table.add_row(field, str(value))
    console.print(table)
    context.close()


@profile_app.command("set")
def profile_set(
    field: str = typer.Argument(..., help="Profile field name"),
    value: str = typer.Argument(..., help="New value (JSON for lists)"),
) -> None:
    """Update one profile field."""
    import json

    context = AppContext(configure_logs=False)
    parsed: object = value
    if value.strip().startswith(("[", "{")):
        parsed = json.loads(value)
    elif value.lower() in {"true", "false"}:
        parsed = value.lower() == "true"
    else:
        try:
            parsed = float(value) if "." in value else int(value)
        except ValueError:
            parsed = value

    with context.session() as session:
        update_profile(session, {field: parsed})
    console.print(f"[green]Updated[/green] {field} = {parsed}")
    context.close()


# ------------------------------------------------------------------- cv ---


@cv_app.command("list")
def cv_list() -> None:
    """List registered CV files."""
    context = AppContext(configure_logs=False)
    with context.session() as session:
        table = Table(title="CV files")
        for col in ("ID", "File", "Language", "Default", "Available", "Text"):
            table.add_column(col)
        for record in list_cvs(session):
            table.add_row(
                str(record.id),
                record.filename,
                str(record.language),
                "yes" if record.is_default else "",
                "yes" if record.is_available else "no",
                f"{len(record.extracted_text or '')} chars",
            )
    console.print(table)
    context.close()


@cv_app.command("add")
def cv_add(
    path: Path = typer.Argument(..., help="Path to the CV file"),
    default: bool = typer.Option(False, "--default", help="Make this the default CV"),
    description: str = typer.Option(None),
) -> None:
    """Register a CV file."""
    context = AppContext(configure_logs=False)
    with context.session() as session:
        record = register_cv(session, path, is_default=default, description=description)
        console.print(
            f"[green]Registered[/green] {record.filename} "
            f"(language={record.language.value}, {len(record.extracted_text or '')} chars)"
        )
    context.close()


@cv_app.command("default")
def cv_default(cv_id: int = typer.Argument(...)) -> None:
    """Mark a CV as the default."""
    context = AppContext(configure_logs=False)
    with context.session() as session:
        set_default_cv(session, cv_id)
    console.print(f"[green]CV {cv_id} is now the default.[/green]")
    context.close()


# ---------------------------------------------------------------- serve ---


@app.command()
def serve(
    host: str = typer.Option(None),
    port: int = typer.Option(None),
    reload: bool = typer.Option(False, help="Auto-reload on code changes"),
) -> None:
    """Start the local dashboard."""
    import uvicorn

    settings = get_settings()
    _run_migrations()
    bind_host = host or settings.host
    bind_port = port or settings.port
    console.print(f"[bold green]Dashboard:[/bold green] http://{bind_host}:{bind_port}")
    uvicorn.run(
        "jobhunter.web.app:create_app",
        host=bind_host,
        port=bind_port,
        reload=reload,
        factory=True,
        log_level=settings.log_level.lower(),
    )


# ------------------------------------------------------------ scheduler ---


@app.command()
def schedule() -> None:
    """Run the scheduler in the foreground."""
    from jobhunter.scheduler.scheduler import run_forever

    run_forever(AppContext())


# ---------------------------------------------------------------- apply ---


@app.command()
def apply(
    job_id: int = typer.Argument(..., help="Job to apply to"),
    submit: bool = typer.Option(
        False, "--submit", help="Actually submit (otherwise prepare and stop before submitting)"
    ),
) -> None:
    """Prepare (and optionally submit) an application for one job."""
    from jobhunter.applications.orchestrator import ApplicationOrchestrator

    context = AppContext()
    orchestrator = ApplicationOrchestrator(context)
    outcome = orchestrator.apply_to_job(job_id, submit=submit)

    if outcome.success:
        console.print(f"[bold green]Application submitted.[/bold green] {outcome.evidence or ''}")
    elif outcome.blocked:
        console.print(f"[yellow]Blocked:[/yellow] {outcome.failure_reason}")
    elif outcome.requires_manual_step:
        console.print(f"[yellow]Manual step required:[/yellow] {outcome.failure_reason}")
    else:
        console.print(f"[red]Not submitted:[/red] {outcome.failure_reason}")
    context.close()


@app.command()
def applications(limit: int = typer.Option(20)) -> None:
    """List application records."""
    context = AppContext(configure_logs=False)
    with context.session() as session:
        rows = session.scalars(
            select(Application).order_by(desc(Application.id)).limit(limit)
        ).all()
        table = Table(title="Applications")
        for col in ("ID", "Job", "Company", "State", "Method", "Success", "Submitted"):
            table.add_column(col)
        for record in rows:
            job = session.get(Job, record.job_id)
            table.add_row(
                str(record.id),
                (job.title[:32] if job else "-"),
                (job.company_display[:20] if job else "-"),
                str(record.state),
                str(record.method),
                {True: "yes", False: "no", None: "-"}[record.success],
                record.submitted_at.strftime("%Y-%m-%d %H:%M") if record.submitted_at else "-",
            )
    console.print(table)
    context.close()


if __name__ == "__main__":
    app()

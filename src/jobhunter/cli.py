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
    to_snapshot,
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

    if settings.ai_provider == "local":
        local = context.local_model
        try:
            installed = local.installed_models()
            if settings.local_model in installed:
                row("Local model", True, f"{settings.local_model} at {settings.local_model_host}")
            else:
                row(
                    "Local model",
                    False,
                    f"{settings.local_model!r} not installed - "
                    f"run: ollama pull {settings.local_model}",
                )
        except Exception:
            row(
                "Local model",
                False,
                f"no Ollama server at {settings.local_model_host} - run: ollama serve",
            )

        # A CV is what makes the matching good, so say plainly if none is usable.
        try:
            from jobhunter.profile.context import cv_has_placeholders

            with context.session() as session:
                usable = [
                    cv
                    for cv in session.scalars(select(CVFile)).all()
                    if cv.extracted_text and not cv_has_placeholders(cv.extracted_text)
                ]
                templates = (session.scalar(select(func.count()).select_from(CVFile)) or 0) - len(
                    usable
                )
            row(
                "Usable CV",
                len(usable) > 0,
                f"{len(usable)} usable"
                + (f", {templates} unfilled template(s) excluded" if templates else ""),
            )
        except Exception as exc:
            row("Usable CV", None, str(exc)[:60])
    else:
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
    """Discover listings, filter them, and evaluate what survives."""
    context = AppContext()

    if context.settings.ai_provider == "local" and not context.local_model.is_available():
        console.print(
            f"[red]Local model {context.settings.local_model!r} is not reachable[/red] at "
            f"{context.settings.local_model_host}.\n"
            "Start it with `ollama serve`, or set AI_PROVIDER=rule_based to scan without it."
        )
        raise typer.Exit(code=1)

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
    console.print("\nRun [bold]jobhunter today[/bold] to see what is worth applying to.")
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


# --------------------------------------------------------------- the new agent


@app.command()
def today(
    limit: int = typer.Option(10, help="How many jobs to list per bucket"),
    hours: int | None = typer.Option(None, help="Only listings first seen in the last N hours"),
) -> None:
    """What is worth applying to right now."""
    from jobhunter.briefing import build_briefing, render_briefing

    context = AppContext(configure_logs=False)
    with context.session() as session:
        briefing = build_briefing(session, limit=limit, since_hours=hours)
        console.print(render_briefing(briefing))

        for heading, bucket, style in (
            ("APPLY", briefing.apply, "bold green"),
            ("REVIEW", briefing.review, "yellow"),
        ):
            if not bucket:
                continue
            console.print(f"\n[{style}]{heading}[/{style}]")
            for ranked in bucket:
                job, evaluation = ranked.job, ranked.evaluation
                console.print(
                    f"  [{job.id}] {job.title} - {job.company_display} "
                    f"({job.city or 'location unknown'})"
                )
                if evaluation.recommendation:
                    console.print(f"      {evaluation.recommendation}")
                if evaluation.major_risks:
                    console.print(f"      [dim]risk: {evaluation.major_risks[0]}[/dim]")
    context.close()


@app.command()
def evaluate(
    job_id: int | None = typer.Option(None, help="Evaluate one job instead of all pending"),
    limit: int = typer.Option(25, help="Maximum jobs to evaluate in this pass"),
    force: bool = typer.Option(False, help="Ignore cached evaluations"),
) -> None:
    """Run the two-stage matcher over stored jobs."""
    from jobhunter.db.models import CVFile
    from jobhunter.db.models import JobEvaluation as JobEvaluationRow
    from jobhunter.matching.evaluator import EvaluationStats
    from jobhunter.normalize.normalizer import normalize_job
    from jobhunter.pipeline.repository import raw_job_from_model

    context = AppContext(configure_logs=False)
    stats = EvaluationStats()

    if not context.local_model.is_available():
        console.print(
            f"[red]Local model {context.settings.local_model!r} is not available[/red] at "
            f"{context.settings.local_model_host}. Start it with `ollama serve`."
        )
        raise typer.Exit(code=1)

    with context.session() as session:
        candidate = to_snapshot(get_active_profile(session))
        cv = (
            session.query(CVFile)
            .filter(CVFile.is_available.is_(True), CVFile.extracted_text.isnot(None))
            .order_by(CVFile.is_default.desc(), CVFile.id)
            .first()
        )
        cv_text = cv.extracted_text if cv else None

        query = session.query(Job).filter(Job.is_archived.is_(False))
        if job_id is not None:
            query = query.filter(Job.id == job_id)
        else:
            evaluated = session.query(JobEvaluationRow.job_id).filter(
                JobEvaluationRow.is_current.is_(True)
            )
            if not force:
                query = query.filter(~Job.id.in_(evaluated))
        jobs = query.order_by(Job.id).limit(limit).all()

        if not jobs:
            console.print("Nothing to evaluate.")
            context.close()
            return

        console.print(f"Evaluating {len(jobs)} job(s) with {context.settings.local_model}...")
        for index, job in enumerate(jobs, start=1):
            normalized = normalize_job(raw_job_from_model(job))
            evaluation = context.evaluator.evaluate(
                session,
                job.id,
                normalized,
                candidate,
                cv_text=cv_text,
                stats=stats,
                force=force,
            )
            colour = {"apply": "green", "review": "yellow", "skip": "dim"}[
                evaluation.decision.value
            ]
            console.print(
                f"  [{index}/{len(jobs)}] [{colour}]{evaluation.decision.value.upper():6}[/{colour}] "
                f"{job.title[:52]} [dim]({evaluation.source})[/dim]"
            )

    console.print(
        f"\nmodel calls {stats.evaluated} | gated {stats.gated} | cached {stats.cached} "
        f"| degraded {stats.degraded} | downgraded {stats.downgraded}"
    )
    if stats.gate_reasons:
        console.print(f"gate reasons: {stats.gate_reasons}")
    context.close()


@app.command()
def feedback(
    job_id: int = typer.Argument(..., help="The job you are giving feedback on"),
    action: str = typer.Argument(..., help="apply | skip | not_sure"),
    reason: str | None = typer.Option(None, help="too_senior, wrong_location, salary, ..."),
    note: str | None = typer.Option(None, help="Free-text note"),
) -> None:
    """Record what you decided, so future rankings improve."""
    from jobhunter.pipeline.feedback_store import record_feedback

    context = AppContext(configure_logs=False)
    with context.session() as session:
        try:
            row = record_feedback(session, job_id, action=action, reason=reason, note=note)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        console.print(
            f"Recorded: job {row.job_id} -> {row.action}"
            + (f" ({row.reason})" if row.reason else "")
        )
    context.close()


@app.command()
def preferences() -> None:
    """Show what the system has learned from your decisions."""
    from jobhunter.db.models import CandidatePreference

    context = AppContext(configure_logs=False)
    with context.session() as session:
        rows = (
            session.query(CandidatePreference)
            .order_by(desc(CandidatePreference.evidence_count))
            .all()
        )
        if not rows:
            console.print("Nothing learned yet - give feedback on a few jobs first.")
            context.close()
            return
        table = Table(title="Learned preferences")
        for col in ("Dimension", "Value", "Weight", "Applies", "Skips", "Evidence"):
            table.add_column(col)
        for row in rows:
            table.add_row(
                row.dimension,
                row.value[:30],
                f"{row.weight:+.2f}",
                str(row.applies),
                str(row.skips),
                str(row.evidence_count),
            )
        console.print(table)
    context.close()


@app.command()
def benchmark(
    models: str = typer.Option("", help="Comma-separated models; defaults to the configured one"),
    include_legacy: bool = typer.Option(True, help="Also run the old rule-based scorer"),
    limit: int | None = typer.Option(None, help="Only the first N cases"),
) -> None:
    """Measure matchers against the labelled dataset."""
    from jobhunter.ai.local_model import LocalModelConfig
    from jobhunter.db.models import CVFile
    from jobhunter.evaluation.dataset import Dataset, load_dataset
    from jobhunter.evaluation.metrics import format_report
    from jobhunter.evaluation.runner import (
        AlwaysSkipMatcher,
        LegacyMatcher,
        LocalModelMatcher,
        run_benchmark,
    )

    context = AppContext(configure_logs=False)
    dataset = load_dataset()
    if limit:
        dataset = Dataset(dataset.cases[:limit], dataset.candidate_note)

    with context.session() as session:
        candidate = to_snapshot(get_active_profile(session))
        cv = (
            session.query(CVFile)
            .filter(CVFile.is_available.is_(True), CVFile.extracted_text.isnot(None))
            .order_by(CVFile.is_default.desc(), CVFile.id)
            .first()
        )
        cv_text = cv.extracted_text if cv else None

    console.print(f"Dataset: {len(dataset)} cases {dataset.decision_counts}\n")

    matchers: list = [AlwaysSkipMatcher()]
    if include_legacy:
        matchers.append(LegacyMatcher(candidate, context.scoring_config))
    for model in [m.strip() for m in models.split(",") if m.strip()] or [
        context.settings.local_model
    ]:
        matchers.append(
            LocalModelMatcher(
                candidate,
                LocalModelConfig(
                    model=model,
                    host=context.settings.local_model_host,
                    timeout_seconds=context.settings.local_model_timeout_seconds,
                    num_ctx=context.settings.local_model_num_ctx,
                    num_predict=context.settings.local_model_num_predict,
                ),
                cv_text=cv_text,
            )
        )

    for matcher in matchers:
        report = run_benchmark(matcher, dataset)
        console.print(format_report(report))
        for outcome in report.failures():
            marker = "HARMFUL" if outcome.harmful else "       "
            console.print(
                f"    [dim]{marker} {outcome.case_id:8} want {outcome.expected.value:6} "
                f"got {outcome.predicted.value:6} | {outcome.title[:44]}[/dim]"
            )
        console.print()
    context.close()


@app.command("dataset")
def dataset_build() -> None:
    """Rebuild the benchmark dataset from labels plus stored postings."""
    from jobhunter.evaluation.build import build_and_save
    from jobhunter.evaluation.dataset import load_dataset

    context = AppContext(configure_logs=False)
    with context.session() as session:
        path = build_and_save(session)
    dataset = load_dataset(path)
    console.print(f"Wrote {len(dataset)} cases to {path}")
    console.print(f"Decisions: {dataset.decision_counts}")
    context.close()


if __name__ == "__main__":
    app()

"""SQLAlchemy models for the whole system."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from jobhunter.db.base import Base, StrEnumType, TimestampMixin, utcnow
from jobhunter.domain.enums import (
    ApplicationMethod,
    EmploymentType,
    JobState,
    Language,
    NotificationKind,
    NotificationLevel,
    Recommendation,
    RunStatus,
    RunTrigger,
    Seniority,
    WorkMode,
)


class Company(Base, TimestampMixin):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    normalized_name: Mapped[str] = mapped_column(
        String(300), nullable=False, unique=True, index=True
    )
    website: Mapped[str | None] = mapped_column(String(500))
    source_company_id: Mapped[str | None] = mapped_column(String(100), index=True)

    jobs: Mapped[list[Job]] = relationship(back_populates="company")

    def __repr__(self) -> str:
        return f"<Company {self.name!r}>"


class Job(Base, TimestampMixin):
    """A single discovered listing, plus everything derived from it."""

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("fingerprint", name="uq_jobs_fingerprint"),
        Index("ix_jobs_state_score", "state"),
        Index("ix_jobs_company_title", "company_id", "title_normalized"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # --- identity / dedupe
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="jobs.bg", index=True)
    source_job_id: Mapped[str | None] = mapped_column(String(100), index=True)
    source_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    normalized_url: Mapped[str] = mapped_column(String(1000), nullable=False, index=True)

    # --- core content
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    title_normalized: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"))
    company_name_raw: Mapped[str | None] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    description_hash: Mapped[str | None] = mapped_column(String(64), index=True)

    # --- location
    location_raw: Mapped[str | None] = mapped_column(String(300))
    city: Mapped[str | None] = mapped_column(String(120), index=True)
    work_mode: Mapped[WorkMode] = mapped_column(StrEnumType(WorkMode, 20), default=WorkMode.UNKNOWN)

    # --- compensation
    salary_min: Mapped[float | None] = mapped_column(Float)
    salary_max: Mapped[float | None] = mapped_column(Float)
    salary_currency: Mapped[str | None] = mapped_column(String(10))
    salary_period: Mapped[str | None] = mapped_column(String(20))
    salary_raw: Mapped[str | None] = mapped_column(String(300))

    # --- classification
    employment_type: Mapped[EmploymentType] = mapped_column(
        StrEnumType(EmploymentType, 20), default=EmploymentType.UNKNOWN
    )
    seniority: Mapped[Seniority] = mapped_column(
        StrEnumType(Seniority, 20), default=Seniority.UNKNOWN, index=True
    )
    seniority_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    seniority_signals: Mapped[list[str]] = mapped_column(JSON, default=list)
    is_it: Mapped[bool | None] = mapped_column(Boolean, index=True)
    it_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    it_signals: Mapped[list[str]] = mapped_column(JSON, default=list)
    location_relevant: Mapped[bool | None] = mapped_column(Boolean, index=True)
    language: Mapped[Language] = mapped_column(StrEnumType(Language, 10), default=Language.UNKNOWN)

    # --- what the site itself asserts, kept verbatim
    #
    # These are the site's own tags ("Ниво Entry-level / Junior, Mid-level",
    # "от 0 до 4" years, the home-office field). They are frequently wrong, which
    # is why nothing derives a decision from them directly — but they are real
    # evidence and the matcher should see them rather than have to guess.
    level_raw: Mapped[str | None] = mapped_column(String(200))
    experience_raw: Mapped[str | None] = mapped_column(String(200))
    work_mode_raw: Mapped[str | None] = mapped_column(String(200))
    languages_raw: Mapped[list[str]] = mapped_column(JSON, default=list)

    # --- extracted structure
    tech_keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    requirements_required: Mapped[list[str]] = mapped_column(JSON, default=list)
    requirements_preferred: Mapped[list[str]] = mapped_column(JSON, default=list)
    years_experience_required: Mapped[float | None] = mapped_column(Float)

    # --- application route
    application_method: Mapped[ApplicationMethod] = mapped_column(
        StrEnumType(ApplicationMethod, 30), default=ApplicationMethod.UNKNOWN
    )
    application_url: Mapped[str | None] = mapped_column(String(1000))
    application_email: Mapped[str | None] = mapped_column(String(300))

    # --- lifecycle
    state: Mapped[JobState] = mapped_column(
        StrEnumType(JobState, 20), default=JobState.DISCOVERED, index=True
    )
    state_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    posted_at_raw: Mapped[str | None] = mapped_column(String(100))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    seen_count: Mapped[int] = mapped_column(Integer, default=1)
    detail_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    company: Mapped[Company | None] = relationship(back_populates="jobs")
    matches: Mapped[list[JobMatch]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobMatch.id.desc()"
    )
    application: Mapped[Application | None] = relationship(
        back_populates="job", cascade="all, delete-orphan", uselist=False
    )
    evaluations: Mapped[list[JobEvaluation]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobEvaluation.id.desc()"
    )
    feedback: Mapped[list[UserFeedback]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="UserFeedback.id.desc()"
    )

    @property
    def latest_match(self) -> JobMatch | None:
        return self.matches[0] if self.matches else None

    @property
    def current_evaluation(self) -> JobEvaluation | None:
        """The evaluation the dashboard should show for this job."""
        return next((e for e in self.evaluations if e.is_current), None)

    @property
    def latest_feedback(self) -> UserFeedback | None:
        return self.feedback[0] if self.feedback else None

    @property
    def company_display(self) -> str:
        if self.company is not None:
            return self.company.name
        return self.company_name_raw or "Unknown"

    def __repr__(self) -> str:
        return f"<Job {self.id} {self.title!r} state={self.state}>"


class JobMatch(Base, TimestampMixin):
    """One scoring pass over a job. Kept append-only so re-scores are auditable."""

    __tablename__ = "job_matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )

    score: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    recommendation: Mapped[Recommendation] = mapped_column(
        StrEnumType(Recommendation, 20), default=Recommendation.SKIP, index=True
    )

    strengths: Mapped[list[str]] = mapped_column(JSON, default=list)
    missing_skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    disqualifiers: Mapped[list[str]] = mapped_column(JSON, default=list)
    component_scores: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    reasoning: Mapped[str | None] = mapped_column(Text)

    provider: Mapped[str] = mapped_column(String(50), default="rule_based")
    model: Mapped[str | None] = mapped_column(String(100))
    profile_version: Mapped[int] = mapped_column(Integer, default=1)

    job: Mapped[Job] = relationship(back_populates="matches")

    def __repr__(self) -> str:
        return f"<JobMatch job={self.job_id} score={self.score} rec={self.recommendation}>"


class CandidateProfile(Base, TimestampMixin):
    __tablename__ = "candidate_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)

    full_name: Mapped[str] = mapped_column(String(200), default="")
    email: Mapped[str | None] = mapped_column(String(300))
    phone: Mapped[str | None] = mapped_column(String(60))
    location: Mapped[str] = mapped_column(String(200), default="")
    summary: Mapped[str | None] = mapped_column(Text)

    years_experience: Mapped[float] = mapped_column(Float, default=0.0)
    desired_seniority: Mapped[Seniority] = mapped_column(
        StrEnumType(Seniority, 20), default=Seniority.JUNIOR_MID
    )
    preferred_locations: Mapped[list[str]] = mapped_column(JSON, default=list)
    remote_ok: Mapped[bool] = mapped_column(Boolean, default=True)

    education: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    languages: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    frameworks: Mapped[list[str]] = mapped_column(JSON, default=list)
    databases: Mapped[list[str]] = mapped_column(JSON, default=list)
    tools: Mapped[list[str]] = mapped_column(JSON, default=list)
    soft_skills: Mapped[list[str]] = mapped_column(JSON, default=list)

    employment_types: Mapped[list[str]] = mapped_column(JSON, default=list)
    salary_expectation_min: Mapped[float | None] = mapped_column(Float)
    salary_expectation_max: Mapped[float | None] = mapped_column(Float)
    salary_currency: Mapped[str] = mapped_column(String(10), default="BGN")

    portfolio_url: Mapped[str | None] = mapped_column(String(500))
    linkedin_url: Mapped[str | None] = mapped_column(String(500))
    github_url: Mapped[str | None] = mapped_column(String(500))

    def __repr__(self) -> str:
        return f"<CandidateProfile {self.full_name!r} v{self.version}>"


class CVFile(Base, TimestampMixin):
    __tablename__ = "cv_files"
    __table_args__ = (UniqueConstraint("path", name="uq_cv_files_path"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    path: Mapped[str] = mapped_column(String(1000), nullable=False)
    filename: Mapped[str] = mapped_column(String(300), nullable=False)
    language: Mapped[Language] = mapped_column(
        StrEnumType(Language, 10), default=Language.UNKNOWN, index=True
    )
    version: Mapped[str] = mapped_column(String(50), default="1")
    description: Mapped[str | None] = mapped_column(Text)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)

    file_size: Mapped[int | None] = mapped_column(Integer)
    file_hash: Mapped[str | None] = mapped_column(String(64))
    extracted_text: Mapped[str | None] = mapped_column(Text)
    text_extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    applications: Mapped[list[Application]] = relationship(back_populates="cv_file")

    def __repr__(self) -> str:
        return f"<CVFile {self.filename!r} lang={self.language}>"


class Application(Base, TimestampMixin):
    """At most one application row per job — the hard duplicate guard."""

    __tablename__ = "applications"
    __table_args__ = (UniqueConstraint("job_id", name="uq_applications_job_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    cv_file_id: Mapped[int | None] = mapped_column(ForeignKey("cv_files.id", ondelete="SET NULL"))

    state: Mapped[JobState] = mapped_column(
        StrEnumType(JobState, 20), default=JobState.APPROVED, index=True
    )
    method: Mapped[ApplicationMethod] = mapped_column(
        StrEnumType(ApplicationMethod, 30), default=ApplicationMethod.UNKNOWN
    )
    cover_letter: Mapped[str | None] = mapped_column(Text)
    cover_letter_language: Mapped[Language] = mapped_column(
        StrEnumType(Language, 10), default=Language.UNKNOWN
    )

    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    success: Mapped[bool | None] = mapped_column(Boolean)
    confirmation_evidence: Mapped[str | None] = mapped_column(Text)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    screenshot_path: Mapped[str | None] = mapped_column(String(1000))
    is_manual: Mapped[bool] = mapped_column(Boolean, default=False)

    job: Mapped[Job] = relationship(back_populates="application")
    cv_file: Mapped[CVFile | None] = relationship(back_populates="applications")
    events: Mapped[list[ApplicationEvent]] = relationship(
        back_populates="application", cascade="all, delete-orphan", order_by="ApplicationEvent.id"
    )

    def __repr__(self) -> str:
        return f"<Application job={self.job_id} state={self.state}>"


class ApplicationEvent(Base):
    """Append-only audit trail of every state transition and notable action."""

    __tablename__ = "application_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int | None] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), index=True
    )

    event: Mapped[str] = mapped_column(String(100), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(20))
    to_state: Mapped[str | None] = mapped_column(String(20))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )

    application: Mapped[Application | None] = relationship(back_populates="events")

    def __repr__(self) -> str:
        return f"<ApplicationEvent {self.event} {self.from_state}->{self.to_state}>"


class Setting(Base, TimestampMixin):
    """Runtime-editable settings that override the .env defaults."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)

    def __repr__(self) -> str:
        return f"<Setting {self.key}>"


class AutomationRun(Base, TimestampMixin):
    __tablename__ = "automation_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trigger: Mapped[RunTrigger] = mapped_column(
        StrEnumType(RunTrigger, 20), default=RunTrigger.MANUAL
    )
    status: Mapped[RunStatus] = mapped_column(
        StrEnumType(RunStatus, 20), default=RunStatus.RUNNING, index=True
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    jobs_seen: Mapped[int] = mapped_column(Integer, default=0)
    jobs_new: Mapped[int] = mapped_column(Integer, default=0)
    jobs_updated: Mapped[int] = mapped_column(Integer, default=0)
    jobs_classified: Mapped[int] = mapped_column(Integer, default=0)
    jobs_matched: Mapped[int] = mapped_column(Integer, default=0)
    applications_submitted: Mapped[int] = mapped_column(Integer, default=0)
    errors_count: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str | None] = mapped_column(Text)
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    errors: Mapped[list[ErrorRecord]] = relationship(back_populates="run")

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    def __repr__(self) -> str:
        return f"<AutomationRun {self.id} {self.status}>"


class ErrorRecord(Base):
    __tablename__ = "errors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("automation_runs.id", ondelete="SET NULL"), index=True
    )
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))

    category: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    traceback: Mapped[str | None] = mapped_column(Text)
    screenshot_path: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )

    run: Mapped[AutomationRun | None] = relationship(back_populates="errors")

    def __repr__(self) -> str:
        return f"<ErrorRecord {self.category}>"


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[NotificationKind] = mapped_column(
        StrEnumType(NotificationKind, 40), nullable=False, index=True
    )
    level: Mapped[NotificationLevel] = mapped_column(
        StrEnumType(NotificationLevel, 20), default=NotificationLevel.INFO
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )

    def __repr__(self) -> str:
        return f"<Notification {self.kind} {self.title!r}>"


class JobEvaluation(Base, TimestampMixin):
    """A cached structured evaluation of one job by one matcher.

    Append-only, and keyed by everything that could change the answer: the job's
    content, the candidate's profile, the model, and the prompt. A scan re-sees
    the same listings every day, and a local model costs tens of seconds per job,
    so re-running an evaluation whose inputs are all unchanged is the single
    biggest waste the pipeline can make.
    """

    __tablename__ = "job_evaluations"
    __table_args__ = (
        Index(
            "ix_job_evaluations_cache",
            "job_content_hash",
            "candidate_fingerprint",
            "model",
            "prompt_version",
            "schema_version",
        ),
        Index("ix_job_evaluations_job_current", "job_id", "is_current"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # --- the decision
    decision: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    recommendation: Mapped[str | None] = mapped_column(Text)
    reasoning: Mapped[str | None] = mapped_column(Text)

    # --- the claims behind it
    is_it_role: Mapped[bool | None] = mapped_column(Boolean)
    seniority: Mapped[Seniority] = mapped_column(
        StrEnumType(Seniority, 20), default=Seniority.UNKNOWN
    )
    seniority_reasoning: Mapped[str | None] = mapped_column(Text)
    location_fit: Mapped[str | None] = mapped_column(String(20))
    location_reasoning: Mapped[str | None] = mapped_column(Text)
    employment_fit: Mapped[bool | None] = mapped_column(Boolean)
    experience_fit: Mapped[str | None] = mapped_column(String(20))

    mandatory_requirements: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    nice_to_have_requirements: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    major_strengths: Mapped[list[str]] = mapped_column(JSON, default=list)
    major_risks: Mapped[list[str]] = mapped_column(JSON, default=list)

    # --- ordering signal, deliberately not the headline
    rank_score: Mapped[float | None] = mapped_column(Float, index=True)

    # --- provenance / cache key
    source: Mapped[str] = mapped_column(String(40), default="local")
    model: Mapped[str | None] = mapped_column(String(120), index=True)
    prompt_version: Mapped[str | None] = mapped_column(String(80), index=True)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    job_content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    candidate_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    profile_version: Mapped[int] = mapped_column(Integer, default=1)

    latency_ms: Mapped[int | None] = mapped_column(Integer)
    degraded: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    degraded_reason: Mapped[str | None] = mapped_column(String(300))

    # Only the newest evaluation for a job drives the dashboard; older rows stay
    # for auditing how a decision changed when the profile or prompt changed.
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    job: Mapped[Job] = relationship(back_populates="evaluations")

    def __repr__(self) -> str:
        return f"<JobEvaluation job={self.job_id} {self.decision} model={self.model}>"


class UserFeedback(Base):
    """What the candidate actually decided, and why.

    This is the only ground truth the system ever gets about its own quality, so
    it is recorded verbatim and never overwritten.
    """

    __tablename__ = "user_feedback"
    __table_args__ = (Index("ix_user_feedback_job", "job_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    evaluation_id: Mapped[int | None] = mapped_column(
        ForeignKey("job_evaluations.id", ondelete="SET NULL")
    )

    # What the candidate did: apply / skip / not_sure.
    action: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    # Why, from a fixed vocabulary: too_senior, wrong_location, salary,
    # technology, company, not_interested, other.
    reason: Mapped[str | None] = mapped_column(String(40), index=True)
    note: Mapped[str | None] = mapped_column(Text)

    # What the system had said, so agreement can be measured over time.
    predicted_decision: Mapped[str | None] = mapped_column(String(20))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )

    job: Mapped[Job] = relationship(back_populates="feedback")

    def __repr__(self) -> str:
        return f"<UserFeedback job={self.job_id} {self.action} {self.reason}>"


class CandidatePreference(Base, TimestampMixin):
    """One learned preference, derived from feedback.

    Kept as interpretable rows rather than model weights: with a handful of
    decisions a month, anything fancier would overfit, and the candidate has to
    be able to see and correct what the system thinks it has learned.
    """

    __tablename__ = "candidate_preferences"
    __table_args__ = (
        UniqueConstraint("dimension", "value", name="uq_candidate_preferences_dim_value"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # What kind of thing this is about: technology, company, seniority,
    # work_mode, city, employment_type.
    dimension: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    value: Mapped[str] = mapped_column(String(120), nullable=False)

    # Positive means the candidate leans towards it, negative away from it.
    weight: Mapped[float] = mapped_column(Float, default=0.0)
    applies: Mapped[int] = mapped_column(Integer, default=0)
    skips: Mapped[int] = mapped_column(Integer, default=0)
    evidence_count: Mapped[int] = mapped_column(Integer, default=0)

    def __repr__(self) -> str:
        return f"<CandidatePreference {self.dimension}={self.value} w={self.weight:+.2f}>"

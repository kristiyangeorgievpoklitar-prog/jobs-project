"""Transport objects passed between pipeline stages.

These are deliberately decoupled from the ORM so parsing, classification and
matching can all be unit-tested without a database.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from jobhunter.domain.enums import (
    ApplicationMethod,
    EmploymentType,
    Language,
    Recommendation,
    Seniority,
    WorkMode,
)


class SalaryInfo(BaseModel):
    minimum: float | None = None
    maximum: float | None = None
    currency: str | None = None
    period: str | None = None
    raw: str | None = None

    @property
    def is_present(self) -> bool:
        return self.minimum is not None or self.maximum is not None


class RawJob(BaseModel):
    """A listing exactly as scraped, before normalization."""

    model_config = ConfigDict(extra="ignore")

    source: str = "jobs.bg"
    source_job_id: str | None = None
    source_url: str
    title: str
    company_name: str | None = None
    company_source_id: str | None = None
    location_raw: str | None = None
    description: str | None = None
    posted_at_raw: str | None = None
    salary_raw: str | None = None
    level_raw: str | None = None
    experience_raw: str | None = None
    employment_raw: str | None = None
    work_mode_raw: str | None = None
    languages_raw: list[str] = Field(default_factory=list)
    tech_tags: list[str] = Field(default_factory=list)
    application_method: ApplicationMethod = ApplicationMethod.UNKNOWN
    application_url: str | None = None
    raw_text: str | None = None


class NormalizedJob(BaseModel):
    """A cleaned listing with a stable fingerprint."""

    model_config = ConfigDict(extra="ignore")

    fingerprint: str
    source: str
    source_job_id: str | None
    source_url: str
    normalized_url: str
    title: str
    title_normalized: str
    company_name: str | None
    company_source_id: str | None = None
    description: str | None
    description_hash: str | None
    location_raw: str | None
    city: str | None
    work_mode: WorkMode = WorkMode.UNKNOWN
    salary: SalaryInfo = Field(default_factory=SalaryInfo)
    employment_type: EmploymentType = EmploymentType.UNKNOWN
    posted_at: datetime | None = None
    posted_at_raw: str | None = None
    tech_keywords: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    level_raw: str | None = None
    experience_raw: str | None = None
    years_experience_required: float | None = None
    application_method: ApplicationMethod = ApplicationMethod.UNKNOWN
    application_url: str | None = None
    language: Language = Language.UNKNOWN


class ClassificationResult(BaseModel):
    """Output of the rule-based classifier."""

    seniority: Seniority = Seniority.UNKNOWN
    seniority_confidence: float = 0.0
    seniority_signals: list[str] = Field(default_factory=list)
    is_it: bool | None = None
    it_confidence: float = 0.0
    it_signals: list[str] = Field(default_factory=list)
    location_relevant: bool | None = None
    location_signals: list[str] = Field(default_factory=list)
    language: Language = Language.UNKNOWN
    requirements_required: list[str] = Field(default_factory=list)
    requirements_preferred: list[str] = Field(default_factory=list)
    years_experience_required: float | None = None


class MatchResult(BaseModel):
    """Output of the matching engine for one job."""

    score: int = Field(ge=0, le=100)
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    recommendation: Recommendation = Recommendation.SKIP
    strengths: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    disqualifiers: list[str] = Field(default_factory=list)
    component_scores: dict[str, float] = Field(default_factory=dict)
    reasoning: str | None = None
    provider: str = "rule_based"
    model: str | None = None

    @property
    def is_disqualified(self) -> bool:
        return bool(self.disqualifiers)


class CandidateSnapshot(BaseModel):
    """Read-only view of the candidate handed to matchers and AI providers."""

    full_name: str = ""
    location: str = ""
    years_experience: float = 0.0
    desired_seniority: Seniority = Seniority.JUNIOR_MID
    preferred_locations: list[str] = Field(default_factory=list)
    remote_ok: bool = True
    skills: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    databases: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    soft_skills: list[str] = Field(default_factory=list)
    languages: list[dict[str, str]] = Field(default_factory=list)
    education: list[dict[str, str]] = Field(default_factory=list)
    employment_types: list[str] = Field(default_factory=list)
    salary_expectation_min: float | None = None
    salary_expectation_max: float | None = None
    summary: str | None = None
    version: int = 1

    @property
    def all_tech(self) -> set[str]:
        """Every technical token the candidate claims, lowercased."""
        out: set[str] = set()
        for bucket in (self.skills, self.frameworks, self.databases, self.tools):
            out.update(item.strip().lower() for item in bucket if item.strip())
        return out

    @property
    def spoken_languages(self) -> set[str]:
        return {str(item.get("name", "")).strip().lower() for item in self.languages}


class ApplicationPlan(BaseModel):
    """What the orchestrator intends to do for one approved job."""

    job_id: int
    cv_file_id: int | None = None
    cv_path: str | None = None
    cover_letter: str | None = None
    cover_letter_language: Language = Language.UNKNOWN
    method: ApplicationMethod = ApplicationMethod.UNKNOWN
    apply_url: str | None = None
    requires_manual_step: bool = False
    manual_reason: str | None = None


class ApplicationOutcome(BaseModel):
    """Result of attempting an application."""

    success: bool = False
    blocked: bool = False
    evidence: str | None = None
    failure_reason: str | None = None
    screenshot_path: str | None = None
    requires_manual_step: bool = False


class ScanStats(BaseModel):
    jobs_seen: int = 0
    jobs_new: int = 0
    jobs_updated: int = 0
    jobs_classified: int = 0
    jobs_matched: int = 0
    applications_submitted: int = 0
    errors_count: int = 0
    blocked: bool = False
    notes: list[str] = Field(default_factory=list)

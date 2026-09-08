"""Core domain enumerations shared across the whole system."""

from __future__ import annotations

from enum import StrEnum


class Seniority(StrEnum):
    """Seniority levels, ordered from least to most experienced."""

    INTERNSHIP = "internship"
    ENTRY = "entry"
    JUNIOR = "junior"
    JUNIOR_MID = "junior_mid"
    MID = "mid"
    MID_SENIOR = "mid_senior"
    SENIOR = "senior"
    LEAD = "lead"
    UNKNOWN = "unknown"

    @property
    def rank(self) -> int:
        """Numeric rank for comparison. UNKNOWN deliberately sits mid-scale."""
        return _SENIORITY_RANK[self]

    @property
    def is_known(self) -> bool:
        return self is not Seniority.UNKNOWN


_SENIORITY_RANK: dict[Seniority, int] = {
    Seniority.INTERNSHIP: 0,
    Seniority.ENTRY: 1,
    Seniority.JUNIOR: 2,
    Seniority.JUNIOR_MID: 3,
    Seniority.MID: 4,
    Seniority.MID_SENIOR: 5,
    Seniority.SENIOR: 6,
    Seniority.LEAD: 7,
    Seniority.UNKNOWN: 3,
}


class JobState(StrEnum):
    """Pipeline state of a single job. Transitions are guarded by the state machine."""

    DISCOVERED = "discovered"
    CLASSIFIED = "classified"
    MATCHED = "matched"
    REVIEW = "review"
    APPROVED = "approved"
    APPLYING = "applying"
    APPLIED = "applied"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATES


_TERMINAL_STATES = frozenset({JobState.APPLIED, JobState.SKIPPED})


class Recommendation(StrEnum):
    """Outcome of the decision engine for a scored job."""

    APPLY = "apply"
    REVIEW = "review"
    SKIP = "skip"


class ApplicationMethod(StrEnum):
    """How an application can be submitted for a given listing."""

    JOBSBG_INTERNAL = "jobsbg_internal"
    EXTERNAL_URL = "external_url"
    EMAIL = "email"
    UNKNOWN = "unknown"

    @property
    def is_automatable(self) -> bool:
        """Only the site's own apply form is driven by the browser automation."""
        return self is ApplicationMethod.JOBSBG_INTERNAL


class Language(StrEnum):
    BG = "bg"
    EN = "en"
    UNKNOWN = "unknown"


class EmploymentType(StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    INTERNSHIP = "internship"
    TEMPORARY = "temporary"
    UNKNOWN = "unknown"


class WorkMode(StrEnum):
    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"
    UNKNOWN = "unknown"


class NotificationLevel(StrEnum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


class NotificationKind(StrEnum):
    """Discrete notification events the system emits."""

    HIGH_MATCH_JOB = "high_match_job"
    APPLICATION_SUCCESS = "application_success"
    APPLICATION_FAILED = "application_failed"
    AUTOMATION_BLOCKED = "automation_blocked"
    CHALLENGE_DETECTED = "challenge_detected"
    AUTH_EXPIRED = "auth_expired"
    SCAN_COMPLETED = "scan_completed"
    SCAN_FAILED = "scan_failed"


class RunTrigger(StrEnum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"

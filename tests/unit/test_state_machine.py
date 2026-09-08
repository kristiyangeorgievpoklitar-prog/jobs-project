"""Job lifecycle transitions."""

from __future__ import annotations

import itertools

import pytest

from jobhunter.applications.state_machine import (
    ALLOWED_TRANSITIONS,
    InvalidTransitionError,
    can_transition,
)
from jobhunter.domain.enums import JobState


class TestTransitionTable:
    def test_every_state_has_an_entry(self) -> None:
        for state in JobState:
            assert state in ALLOWED_TRANSITIONS

    def test_happy_path(self) -> None:
        path = [
            JobState.DISCOVERED,
            JobState.CLASSIFIED,
            JobState.MATCHED,
            JobState.REVIEW,
            JobState.APPROVED,
            JobState.APPLYING,
            JobState.APPLIED,
        ]
        for current, target in itertools.pairwise(path):
            assert can_transition(current, target), f"{current} -> {target}"

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (JobState.DISCOVERED, JobState.APPLIED),
            (JobState.DISCOVERED, JobState.APPLYING),
            (JobState.CLASSIFIED, JobState.APPLIED),
            (JobState.REVIEW, JobState.APPLIED),
        ],
    )
    def test_cannot_jump_straight_to_applied(self, current, target) -> None:
        assert not can_transition(current, target)

    def test_applied_is_terminal(self) -> None:
        """This is what stops a job ever being applied to twice."""
        for state in JobState:
            if state is not JobState.APPLIED:
                assert not can_transition(JobState.APPLIED, state)
        assert JobState.APPLIED.is_terminal

    def test_failed_can_be_retried(self) -> None:
        assert can_transition(JobState.FAILED, JobState.APPROVED)
        assert can_transition(JobState.FAILED, JobState.APPLYING)

    def test_blocked_can_be_resumed(self) -> None:
        assert can_transition(JobState.BLOCKED, JobState.APPLYING)

    def test_skipped_can_be_reopened_for_review(self) -> None:
        assert can_transition(JobState.SKIPPED, JobState.REVIEW)

    def test_accepts_string_states(self) -> None:
        assert can_transition("discovered", "classified")


class TestTransitionJob:
    def test_records_an_event_and_moves_state(self, session) -> None:
        from jobhunter.applications.state_machine import transition_job
        from jobhunter.db.models import ApplicationEvent, Job

        job = Job(
            fingerprint="fp1",
            source="jobs.bg",
            source_url="u",
            normalized_url="u",
            title="T",
            title_normalized="t",
            state=JobState.DISCOVERED,
        )
        session.add(job)
        session.flush()

        assert transition_job(session, job, JobState.CLASSIFIED, event="classified")
        assert job.state is JobState.CLASSIFIED
        events = session.query(ApplicationEvent).filter_by(job_id=job.id).all()
        assert len(events) == 1
        assert events[0].from_state == "discovered"
        assert events[0].to_state == "classified"

    def test_invalid_transition_raises_when_strict(self, session) -> None:
        from jobhunter.applications.state_machine import transition_job
        from jobhunter.db.models import Job

        job = Job(
            fingerprint="fp2",
            source="jobs.bg",
            source_url="u",
            normalized_url="u",
            title="T",
            title_normalized="t",
            state=JobState.APPLIED,
        )
        session.add(job)
        session.flush()
        with pytest.raises(InvalidTransitionError):
            transition_job(session, job, JobState.APPLYING)

    def test_invalid_transition_is_ignored_when_not_strict(self, session) -> None:
        from jobhunter.applications.state_machine import transition_job
        from jobhunter.db.models import Job

        job = Job(
            fingerprint="fp3",
            source="jobs.bg",
            source_url="u",
            normalized_url="u",
            title="T",
            title_normalized="t",
            state=JobState.APPLIED,
        )
        session.add(job)
        session.flush()
        assert transition_job(session, job, JobState.APPLYING, strict=False) is False
        assert job.state is JobState.APPLIED

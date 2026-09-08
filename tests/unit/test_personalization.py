"""Learning from the candidate's decisions, and what that learning may do.

The central property under test is a restraint: preferences reorder what the
candidate sees, and never change whether they see it. A few skips must not be
able to bury a job the person is well qualified for.
"""

from __future__ import annotations

import pytest

from jobhunter.db.models import Job, UserFeedback
from jobhunter.domain.enums import Seniority, WorkMode
from jobhunter.domain.evaluation import Decision, JobEvaluation
from jobhunter.personalization.learner import (
    MIN_EVIDENCE,
    load_preferences,
    observations_for_job,
    rebuild_preferences,
    smoothed_weight,
)
from jobhunter.personalization.ranker import (
    MAX_PREFERENCE_SHIFT,
    explain_preferences,
    preference_shift,
    rank_jobs,
)
from jobhunter.pipeline.feedback_store import agreement_stats, record_feedback


def make_job(session, *, title="Junior PHP Developer", company="Acme", tech=None, city="Varna"):
    job = Job(
        fingerprint=f"fp-{title}-{company}-{city}",
        source="jobs.bg",
        source_url=f"https://www.jobs.bg/job/{abs(hash(title + company)) % 99999}",
        normalized_url=f"https://www.jobs.bg/job/{abs(hash(title + company)) % 99999}",
        title=title,
        title_normalized=title.lower(),
        company_name_raw=company,
        city=city,
        seniority=Seniority.JUNIOR,
        work_mode=WorkMode.ONSITE,
        tech_keywords=tech or ["php", "laravel"],
        description="Requirements: PHP and Laravel. " * 20,
    )
    session.add(job)
    session.flush()
    return job


# ------------------------------------------------------------------ smoothing


def test_a_single_decision_leans_rather_than_concludes():
    assert smoothed_weight(1, 0) == pytest.approx(0.333, abs=0.01)


def test_confidence_grows_with_consistent_evidence():
    assert smoothed_weight(5, 0) > smoothed_weight(2, 0) > smoothed_weight(1, 0)


def test_weights_stay_inside_the_unit_interval():
    for applies, skips in [(0, 0), (50, 0), (0, 50), (10, 10)]:
        assert -1.0 <= smoothed_weight(applies, skips) <= 1.0


def test_no_evidence_means_no_opinion():
    assert smoothed_weight(0, 0) == 0.0


# ------------------------------------------------------------------- learning


def test_feedback_builds_preferences_from_the_job_it_was_given_on(session):
    job = make_job(session, tech=["php", "laravel"])
    record_feedback(session, job.id, action="apply")

    preferences = load_preferences(session, min_evidence=1)
    assert preferences[("technology", "php")] > 0
    assert preferences[("company", "acme")] > 0


def test_skipping_pushes_a_preference_negative(session):
    for index in range(3):
        job = make_job(session, title=f"Java Developer {index}", tech=["java"])
        record_feedback(session, job.id, action="skip", reason="technology")

    preferences = load_preferences(session, min_evidence=1)
    assert preferences[("technology", "java")] < 0


def test_an_explicit_reason_counts_for_more_than_an_incidental_tag(session):
    job = make_job(session, tech=["java"])
    explicit = observations_for_job(job, reason="technology")
    incidental = observations_for_job(job, reason="salary")

    weight_of = lambda obs: next(o.weight for o in obs if o.value == "java")  # noqa: E731
    assert weight_of(explicit) > weight_of(incidental)


def test_not_sure_carries_no_directional_signal(session):
    job = make_job(session)
    record_feedback(session, job.id, action="not_sure")
    assert load_preferences(session, min_evidence=1) == {}


def test_preferences_are_recomputed_from_scratch(session):
    """A rebuild must not leave residue from feedback that no longer exists."""
    job = make_job(session, tech=["php"])
    record_feedback(session, job.id, action="skip", reason="technology")
    assert load_preferences(session, min_evidence=1)[("technology", "php")] < 0

    session.query(UserFeedback).delete()
    rebuild_preferences(session)
    assert load_preferences(session, min_evidence=1) == {}


def test_thin_evidence_is_withheld_until_it_accumulates(session):
    job = make_job(session, tech=["rust"])
    record_feedback(session, job.id, action="apply")
    assert ("technology", "rust") not in load_preferences(session, min_evidence=MIN_EVIDENCE)


def test_invalid_feedback_is_refused(session):
    job = make_job(session)
    with pytest.raises(ValueError):
        record_feedback(session, job.id, action="maybe")
    with pytest.raises(ValueError):
        record_feedback(session, job.id, action="skip", reason="because")


def test_feedback_on_an_unknown_job_is_refused(session):
    with pytest.raises(ValueError):
        record_feedback(session, 9999, action="apply")


# ------------------------------------------------------------------- ranking


def evaluation(decision: Decision, confidence: float = 0.8) -> JobEvaluation:
    return JobEvaluation(decision=decision, confidence=confidence)


def test_ranking_puts_apply_above_review_above_skip(session):
    apply_job = make_job(session, title="A")
    review_job = make_job(session, title="B")
    skip_job = make_job(session, title="C")

    ranked = rank_jobs(
        [
            (skip_job, evaluation(Decision.SKIP)),
            (review_job, evaluation(Decision.REVIEW)),
            (apply_job, evaluation(Decision.APPLY)),
        ]
    )
    assert [r.job.title for r in ranked] == ["A", "B", "C"]


def test_preferences_cannot_lift_a_review_above_an_apply(session):
    """The decision bands are wider than any preference shift can bridge."""
    loved = make_job(session, title="Loved", tech=["php"])
    plain = make_job(session, title="Plain", tech=["cobol"])
    preferences = {("technology", "php"): 1.0, ("technology", "cobol"): -1.0}

    ranked = rank_jobs(
        [(loved, evaluation(Decision.REVIEW)), (plain, evaluation(Decision.APPLY))],
        preferences,
    )
    assert ranked[0].job.title == "Plain"


def test_preferences_reorder_within_a_decision_band(session):
    loved = make_job(session, title="Loved", company="Nice Co", tech=["php"])
    disliked = make_job(session, title="Disliked", company="Meh Co", tech=["java"])
    preferences = {("technology", "php"): 0.8, ("technology", "java"): -0.8}

    ranked = rank_jobs(
        [(disliked, evaluation(Decision.REVIEW)), (loved, evaluation(Decision.REVIEW))],
        preferences,
    )
    assert ranked[0].job.title == "Loved"
    assert ranked[0].personalised


def test_a_job_with_many_tags_is_not_favoured_merely_for_having_them(session):
    """The shift averages its matches, so tag count does not become a score."""
    many = make_job(session, title="Many", tech=["php"] * 1 + ["laravel", "mysql", "css"])
    few = make_job(session, title="Few", tech=["php"])
    preferences = dict.fromkeys(
        [("technology", x) for x in ("php", "laravel", "mysql", "css")], 0.5
    )

    shift_many, _ = preference_shift(many, preferences)
    shift_few, _ = preference_shift(few, preferences)
    assert shift_many == pytest.approx(shift_few, abs=0.001)


def test_the_shift_is_bounded(session):
    job = make_job(session, tech=["php"])
    shift, _ = preference_shift(job, {("technology", "php"): 1.0})
    assert abs(shift) <= MAX_PREFERENCE_SHIFT + 1e-9


def test_no_preferences_means_no_shift(session):
    job = make_job(session)
    assert preference_shift(job, {}) == (0.0, [])


def test_the_reason_for_a_reorder_can_be_explained():
    assert "tend to like" in explain_preferences([("technology", "php", 0.6)])
    assert "tend to skip" in explain_preferences([("company", "acme", -0.6)])
    assert explain_preferences([]) == ""


# ------------------------------------------------------------------ agreement


def test_agreement_tracks_where_the_system_and_the_candidate_differ(session):
    agreed = make_job(session, title="Agreed")
    disagreed = make_job(session, title="Disagreed")

    from jobhunter.db.models import JobEvaluation as Row

    session.add(Row(job_id=agreed.id, decision="apply", is_current=True))
    session.add(Row(job_id=disagreed.id, decision="skip", is_current=True))
    session.flush()

    record_feedback(session, agreed.id, action="apply")
    record_feedback(session, disagreed.id, action="apply")

    stats = agreement_stats(session)
    assert stats == {"total": 2, "agreed": 1, "disagreed": 1}

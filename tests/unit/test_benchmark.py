"""The benchmark's own arithmetic.

The metrics are the evidence for every claim about whether the matcher improved,
so they get tested like production code. The property that matters most is that
the headline number cannot be gamed by refusing to recommend anything.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobhunter.domain.enums import Seniority
from jobhunter.domain.evaluation import Decision, JobEvaluation, LocationFit
from jobhunter.evaluation.dataset import (
    BenchmarkCase,
    CaseLabel,
    Dataset,
    load_dataset,
    save_dataset,
)
from jobhunter.evaluation.metrics import BenchmarkReport, build_outcome, format_report
from jobhunter.evaluation.runner import AlwaysSkipMatcher, run_benchmark


def case(case_id: str, decision: Decision, **overrides) -> BenchmarkCase:
    payload = {
        "id": case_id,
        "title": f"Job {case_id}",
        "company": "Acme",
        "location_raw": "Варна",
        "description": "Requirements: PHP and Laravel. " * 20,
        "tags": overrides.pop("tags", ["english"]),
        "label": CaseLabel(
            decision=decision,
            is_it_role=overrides.pop("is_it_role", True),
            seniority=overrides.pop("seniority", Seniority.JUNIOR),
            location_fit=overrides.pop("location_fit", LocationFit.EXACT_CITY),
        ),
    }
    payload.update(overrides)
    return BenchmarkCase(**payload)


def evaluation(decision: Decision, **overrides) -> JobEvaluation:
    return JobEvaluation(decision=decision, **overrides)


def report_for(pairs: list[tuple[BenchmarkCase, JobEvaluation]]) -> BenchmarkReport:
    report = BenchmarkReport(matcher="test")
    for benchmark_case, ev in pairs:
        report.outcomes.append(build_outcome(benchmark_case, ev, "test"))
    return report


# ------------------------------------------------------------ the headline


def test_refusing_everything_scores_zero_on_the_metric_that_matters():
    """Plain accuracy flatters a matcher that never recommends anything."""
    cases = [case("a", Decision.APPLY), case("b", Decision.REVIEW)] + [
        case(f"s{i}", Decision.SKIP) for i in range(8)
    ]
    report = report_for([(c, evaluation(Decision.SKIP)) for c in cases])

    assert report.decision_accuracy == pytest.approx(0.8), "accuracy looks respectable"
    assert report.worth_surfacing_recall == 0.0, "but it showed the candidate nothing"
    assert report.apply_recall == 0.0


def test_worth_surfacing_recall_counts_review_as_shown():
    """REVIEW still puts the job in front of the candidate."""
    cases = [case("a", Decision.APPLY), case("b", Decision.REVIEW)]
    report = report_for([(c, evaluation(Decision.REVIEW)) for c in cases])
    assert report.worth_surfacing_recall == 1.0
    assert report.apply_recall == 0.0, "but it never made the stronger call"


def test_precision_falls_when_skips_are_surfaced():
    cases = [case("a", Decision.APPLY), case("s", Decision.SKIP)]
    report = report_for([(c, evaluation(Decision.REVIEW)) for c in cases])
    assert report.surfaced_precision == 0.5


# --------------------------------------------------------- error weighting


def test_a_missed_opportunity_is_counted_as_harmful():
    report = report_for([(case("a", Decision.APPLY), evaluation(Decision.SKIP))])
    assert report.missed_opportunities == 1
    assert report.harmful_errors == 1


def test_recommending_a_job_that_should_have_been_filtered_is_harmful():
    report = report_for([(case("s", Decision.SKIP), evaluation(Decision.APPLY))])
    assert report.wasted_attention == 1
    assert report.harmful_errors == 1


def test_an_adjacent_confusion_is_wrong_but_not_harmful():
    """APPLY judged REVIEW still shows the candidate the job."""
    report = report_for([(case("a", Decision.APPLY), evaluation(Decision.REVIEW))])
    assert report.harmful_errors == 0
    assert report.decision_accuracy == 0.0


# ------------------------------------------------------ supporting claims


def test_seniority_is_scored_within_one_band():
    cases = [
        (case("a", Decision.REVIEW, seniority=Seniority.JUNIOR), Seniority.JUNIOR_MID),
        (case("b", Decision.REVIEW, seniority=Seniority.JUNIOR), Seniority.SENIOR),
    ]
    report = report_for([(c, evaluation(Decision.REVIEW, seniority=s)) for c, s in cases])
    assert report.seniority_accuracy == 0.5


def test_an_unknown_seniority_is_excluded_rather_than_counted_wrong():
    """Declining to guess is not the same as guessing badly."""
    report = report_for(
        [(case("a", Decision.REVIEW), evaluation(Decision.REVIEW, seniority=Seniority.UNKNOWN))]
    )
    assert report.seniority_accuracy == 0.0
    assert report.total == 1


def test_degraded_evaluations_are_tracked_separately():
    report = report_for(
        [
            (case("a", Decision.REVIEW), evaluation(Decision.REVIEW, degraded=True)),
            (case("b", Decision.REVIEW), evaluation(Decision.REVIEW)),
        ]
    )
    assert report.degraded_rate == 0.5


def test_latency_percentiles_ignore_matchers_that_do_not_report_one():
    report = report_for(
        [
            (case("a", Decision.REVIEW), evaluation(Decision.REVIEW, latency_ms=1000)),
            (case("b", Decision.REVIEW), evaluation(Decision.REVIEW, latency_ms=3000)),
        ]
    )
    assert report.latency_p50_ms == 3000
    assert report.latency_p90_ms == 3000


def test_failures_by_tag_expose_where_a_matcher_is_weak():
    cases = [
        case("a", Decision.SKIP, tags=["german-required"]),
        case("b", Decision.SKIP, tags=["german-required"]),
    ]
    report = report_for(
        [(cases[0], evaluation(Decision.SKIP)), (cases[1], evaluation(Decision.APPLY))]
    )
    assert report.by_tag()["german-required"] == (1, 2)


def test_the_confusion_matrix_accounts_for_every_case():
    cases = [case("a", Decision.APPLY), case("b", Decision.SKIP)]
    report = report_for(
        [(cases[0], evaluation(Decision.REVIEW)), (cases[1], evaluation(Decision.SKIP))]
    )
    matrix = report.confusion()
    assert matrix["apply"]["review"] == 1
    assert matrix["skip"]["skip"] == 1
    assert sum(sum(row.values()) for row in matrix.values()) == 2


def test_an_empty_report_does_not_divide_by_zero():
    empty = BenchmarkReport(matcher="none")
    assert empty.decision_accuracy == 0.0
    assert empty.worth_surfacing_recall == 0.0
    assert empty.latency_p50_ms is None


def test_the_report_renders_without_a_latency_line_when_there_is_none():
    report = report_for([(case("a", Decision.SKIP), evaluation(Decision.SKIP))])
    text = format_report(report)
    assert "worth-surfacing rec." in text
    assert "latency" not in text


# ------------------------------------------------------------- the dataset


def test_a_case_rebuilds_the_job_the_pipeline_would_have_seen():
    benchmark_case = case("a", Decision.APPLY, level_raw="Ниво Entry-level / Junior")
    job = benchmark_case.to_normalized_job()
    assert job.title == "Job a"
    assert job.city == "Varna"
    assert job.level_raw == "Ниво Entry-level / Junior"


def test_a_dataset_survives_a_round_trip_to_disk(tmp_path: Path):
    dataset = Dataset([case("a", Decision.APPLY), case("b", Decision.SKIP)], "a note")
    path = save_dataset(dataset, tmp_path / "dataset.json")
    restored = load_dataset(path)

    assert len(restored) == 2
    assert restored.candidate_note == "a note"
    assert restored.decision_counts == {"apply": 1, "skip": 1}
    assert restored.cases[0].label.decision is Decision.APPLY


def test_a_dataset_can_be_filtered_to_one_kind_of_case():
    dataset = Dataset(
        [case("a", Decision.APPLY, tags=["remote"]), case("b", Decision.SKIP, tags=["english"])]
    )
    assert len(dataset.filter_by_tag("remote")) == 1


def test_a_missing_dataset_says_how_to_build_one(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="dataset build"):
        load_dataset(tmp_path / "absent.json")


def test_the_always_skip_baseline_is_available_to_every_run():
    dataset = Dataset([case("a", Decision.APPLY), case("s", Decision.SKIP)])
    report = run_benchmark(AlwaysSkipMatcher(), dataset)
    assert report.decision_accuracy == 0.5
    assert report.worth_surfacing_recall == 0.0


def test_a_matcher_that_raises_does_not_lose_the_whole_run():
    class BrokenMatcher:
        name = "broken"

        def evaluate_case(self, benchmark_case):
            raise RuntimeError("boom")

    report = run_benchmark(BrokenMatcher(), Dataset([case("a", Decision.APPLY)]))
    assert report.total == 1
    assert report.degraded_count == 1


# ------------------------------------------------- the committed dataset


def test_the_shipped_dataset_covers_the_cases_that_motivated_it():
    """The benchmark is only meaningful if it contains the hard cases."""
    dataset = load_dataset()
    tags = dataset.tags

    for required in (
        "junior-title-senior-reality",
        "generic-title-genuinely-junior",
        "sofia-in-varna-search",
        "remote",
        "hybrid-varna",
        "german-required",
        "non-it-with-tech-words",
        "missing-mandatory-skill",
        "nice-to-have-missing",
        "bulgarian",
        "english",
        "mixed-language",
        "short-description",
        "long-description",
    ):
        assert required in tags, f"the dataset lost coverage of {required!r}"

    assert len(dataset) >= 30
    assert set(dataset.decision_counts) == {"apply", "review", "skip"}


def test_every_shipped_label_carries_a_reason():
    """A label without a stated reason cannot be argued with later."""
    labels = json.loads(Path("evaluation/labels.json").read_text(encoding="utf-8"))["labels"]
    missing = [job_id for job_id, data in labels.items() if not data.get("notes")]
    assert missing == []

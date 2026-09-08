"""Scoring a matcher against the labelled set.

Plain accuracy is the wrong headline here. The three decisions are not equally
wrong when confused: recommending SKIP for a job worth applying to costs the
candidate an opportunity they never learn about, while a wrong APPLY costs them
an hour of reading. So the errors are counted separately and the summary leads
with the harmful ones.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from jobhunter.domain.enums import Seniority
from jobhunter.domain.evaluation import Decision, JobEvaluation, LocationFit
from jobhunter.evaluation.dataset import BenchmarkCase


@dataclass
class CaseOutcome:
    """What one matcher did on one case."""

    case_id: str
    title: str
    expected: Decision
    predicted: Decision
    tags: list[str]
    seniority_expected: Seniority
    seniority_predicted: Seniority
    location_expected: LocationFit
    location_predicted: LocationFit
    latency_ms: int | None = None
    degraded: bool = False
    degraded_reason: str | None = None
    reasoning: str = ""

    @property
    def correct(self) -> bool:
        return self.expected is self.predicted

    @property
    def missed_opportunity(self) -> bool:
        """Said SKIP about a job the candidate should have applied to."""
        return self.expected is Decision.APPLY and self.predicted is Decision.SKIP

    @property
    def wasted_attention(self) -> bool:
        """Said APPLY about a job that should have been filtered out."""
        return self.expected is Decision.SKIP and self.predicted is Decision.APPLY

    @property
    def harmful(self) -> bool:
        return self.missed_opportunity or self.wasted_attention

    @property
    def seniority_within_one(self) -> bool:
        """Seniority is a band, so being one level out is a near miss, not a failure."""
        return abs(self.seniority_expected.rank - self.seniority_predicted.rank) <= 1


@dataclass
class BenchmarkReport:
    """Aggregate performance of one matcher over one dataset."""

    matcher: str
    outcomes: list[CaseOutcome] = field(default_factory=list)
    notes: dict[str, str] = field(default_factory=dict)

    # ---------------------------------------------------------- headline

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def harmful_errors(self) -> int:
        return sum(1 for o in self.outcomes if o.harmful)

    @property
    def harmful_error_rate(self) -> float:
        return self.harmful_errors / self.total if self.total else 0.0

    @property
    def missed_opportunities(self) -> int:
        return sum(1 for o in self.outcomes if o.missed_opportunity)

    @property
    def wasted_attention(self) -> int:
        return sum(1 for o in self.outcomes if o.wasted_attention)

    @property
    def worth_surfacing_recall(self) -> float:
        """Of the jobs a human said were worth a look, how many did we show?

        This is the product metric. A matcher that says SKIP to everything scores
        well on plain accuracy here, because most listings really are skips — but
        it scores zero on this, which is the failure the candidate actually feels.
        """
        relevant = [o for o in self.outcomes if o.expected is not Decision.SKIP]
        if not relevant:
            return 0.0
        shown = sum(1 for o in relevant if o.predicted is not Decision.SKIP)
        return shown / len(relevant)

    @property
    def surfaced_precision(self) -> float:
        """Of the jobs we showed, how many were worth showing."""
        shown = [o for o in self.outcomes if o.predicted is not Decision.SKIP]
        if not shown:
            return 0.0
        return sum(1 for o in shown if o.expected is not Decision.SKIP) / len(shown)

    @property
    def apply_recall(self) -> float:
        """Of the strongest matches, how many were called APPLY."""
        relevant = [o for o in self.outcomes if o.expected is Decision.APPLY]
        if not relevant:
            return 0.0
        return sum(1 for o in relevant if o.predicted is Decision.APPLY) / len(relevant)

    @property
    def decision_accuracy(self) -> float:
        return sum(1 for o in self.outcomes if o.correct) / self.total if self.total else 0.0

    # ------------------------------------------------- supporting claims

    @property
    def seniority_accuracy(self) -> float:
        scored = [o for o in self.outcomes if o.seniority_predicted is not Seniority.UNKNOWN]
        if not scored:
            return 0.0
        return sum(1 for o in scored if o.seniority_within_one) / len(scored)

    @property
    def location_accuracy(self) -> float:
        scored = [o for o in self.outcomes if o.location_predicted is not LocationFit.UNCLEAR]
        if not scored:
            return 0.0
        return sum(1 for o in scored if o.location_expected is o.location_predicted) / len(scored)

    @property
    def degraded_count(self) -> int:
        return sum(1 for o in self.outcomes if o.degraded)

    @property
    def degraded_rate(self) -> float:
        return self.degraded_count / self.total if self.total else 0.0

    @property
    def latency_p50_ms(self) -> int | None:
        return self._latency_percentile(0.5)

    @property
    def latency_p90_ms(self) -> int | None:
        return self._latency_percentile(0.9)

    def _latency_percentile(self, q: float) -> int | None:
        values = sorted(o.latency_ms for o in self.outcomes if o.latency_ms is not None)
        if not values:
            return None
        index = min(int(q * len(values)), len(values) - 1)
        return values[index]

    # ------------------------------------------------------- breakdowns

    def confusion(self) -> dict[str, dict[str, int]]:
        """expected -> predicted -> count."""
        matrix: dict[str, dict[str, int]] = {
            d.value: {p.value: 0 for p in Decision} for d in Decision
        }
        for outcome in self.outcomes:
            matrix[outcome.expected.value][outcome.predicted.value] += 1
        return matrix

    def by_tag(self) -> dict[str, tuple[int, int]]:
        """tag -> (correct, total), to expose where a matcher is weak."""
        stats: dict[str, tuple[int, int]] = {}
        for outcome in self.outcomes:
            for tag in outcome.tags:
                correct, total = stats.get(tag, (0, 0))
                stats[tag] = (correct + (1 if outcome.correct else 0), total + 1)
        return dict(sorted(stats.items()))

    def failures(self) -> list[CaseOutcome]:
        return [o for o in self.outcomes if not o.correct]

    def summary(self) -> dict[str, object]:
        return {
            "matcher": self.matcher,
            "cases": self.total,
            "harmful_errors": self.harmful_errors,
            "harmful_error_rate": round(self.harmful_error_rate, 3),
            "missed_opportunities": self.missed_opportunities,
            "wasted_attention": self.wasted_attention,
            "decision_accuracy": round(self.decision_accuracy, 3),
            "worth_surfacing_recall": round(self.worth_surfacing_recall, 3),
            "surfaced_precision": round(self.surfaced_precision, 3),
            "apply_recall": round(self.apply_recall, 3),
            "seniority_accuracy": round(self.seniority_accuracy, 3),
            "location_accuracy": round(self.location_accuracy, 3),
            "degraded_rate": round(self.degraded_rate, 3),
            "latency_p50_ms": self.latency_p50_ms,
            "latency_p90_ms": self.latency_p90_ms,
            **self.notes,
        }


def build_outcome(case: BenchmarkCase, evaluation: JobEvaluation, matcher: str) -> CaseOutcome:
    return CaseOutcome(
        case_id=case.id,
        title=case.title,
        expected=case.label.decision,
        predicted=evaluation.decision,
        tags=case.tags,
        seniority_expected=case.label.seniority,
        seniority_predicted=evaluation.seniority,
        location_expected=case.label.location_fit,
        location_predicted=evaluation.location_fit,
        latency_ms=evaluation.latency_ms,
        degraded=evaluation.degraded,
        degraded_reason=evaluation.degraded_reason,
        reasoning=evaluation.reasoning,
    )


def format_report(report: BenchmarkReport) -> str:
    """A report a human can read in the terminal without post-processing."""
    s = report.summary()
    lines = [
        f"{report.matcher}",
        f"  cases                {s['cases']}",
        f"  decision accuracy    {s['decision_accuracy']:.0%}",
        f"  worth-surfacing rec. {s['worth_surfacing_recall']:.0%}"
        f"   (precision {s['surfaced_precision']:.0%}, APPLY recall {s['apply_recall']:.0%})",
        f"  HARMFUL errors       {s['harmful_errors']} ({s['harmful_error_rate']:.0%})"
        f"   [missed {s['missed_opportunities']} / wasted {s['wasted_attention']}]",
        f"  seniority (±1 band)  {s['seniority_accuracy']:.0%}",
        f"  location             {s['location_accuracy']:.0%}",
        f"  degraded             {s['degraded_rate']:.0%}",
    ]
    if s["latency_p50_ms"] is not None:
        lines.append(
            f"  latency p50/p90      {s['latency_p50_ms'] / 1000:.1f}s / "
            f"{s['latency_p90_ms'] / 1000:.1f}s"
        )
    return "\n".join(lines)

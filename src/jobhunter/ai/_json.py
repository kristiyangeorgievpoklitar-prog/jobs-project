"""Shared JSON-response handling for LLM-backed providers."""

from __future__ import annotations

import json
import re
from typing import Any

from jobhunter.domain.enums import Recommendation
from jobhunter.domain.schemas import MatchResult
from jobhunter.matching.rules import ScoringConfig

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def parse_json_object(text: str) -> dict[str, Any]:
    """Extract a JSON object from a model response.

    Tolerates markdown fences and stray prose around the object.
    """
    if not text:
        raise ValueError("empty response")
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        match = _JSON_BLOCK.search(cleaned)
        if not match:
            raise ValueError(f"no JSON object found in response: {cleaned[:200]}") from None
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("response JSON was not an object")
    return parsed


def _string_list(value: Any, limit: int = 12) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        elif isinstance(item, dict) and "name" in item:
            out.append(str(item["name"]))
    return out[:limit]


def match_result_from_payload(
    payload: dict[str, Any],
    *,
    provider: str,
    model: str | None,
    config: ScoringConfig,
    baseline: MatchResult,
) -> MatchResult:
    """Build a MatchResult from a model payload, falling back per-field.

    The deterministic ``baseline`` supplies anything the model omitted, so a
    partial response still produces a complete, usable result.
    """
    try:
        score = round(float(payload.get("score", baseline.score)))
    except (TypeError, ValueError):
        score = baseline.score
    score = max(0, min(100, score))

    try:
        confidence = float(payload.get("confidence", baseline.confidence))
    except (TypeError, ValueError):
        confidence = baseline.confidence
    confidence = max(0.0, min(1.0, confidence))

    disqualifiers = _string_list(payload.get("disqualifiers"))
    # Hard blockers found by the deterministic rules are never discarded by the
    # model's opinion; safety here is one-directional.
    for item in baseline.disqualifiers:
        if item not in disqualifiers:
            disqualifiers.append(item)

    if disqualifiers:
        score = min(score, config.review_threshold - 1)
        recommendation = Recommendation.SKIP
    elif score >= config.auto_apply_threshold:
        recommendation = Recommendation.APPLY
    elif score >= config.review_threshold:
        recommendation = Recommendation.REVIEW
    else:
        recommendation = Recommendation.SKIP

    reasoning = payload.get("reasoning")
    if not isinstance(reasoning, str):
        reasoning = baseline.reasoning

    return MatchResult(
        score=score,
        confidence=round(confidence, 2),
        recommendation=recommendation,
        strengths=_string_list(payload.get("strengths")) or baseline.strengths,
        missing_skills=_string_list(payload.get("missing_skills")) or baseline.missing_skills,
        disqualifiers=disqualifiers,
        component_scores=baseline.component_scores,
        reasoning=reasoning,
        provider=provider,
        model=model,
    )

"""The local instruct model that does the actual job/candidate reasoning.

Local by default, for two reasons that both matter here: the prompt contains the
candidate's CV, and a job hunt runs every day for months — neither the privacy
nor the cost profile of a hosted API suits that.

Ollama is the runtime. It is the one small-model runner on this machine that
already handles model download, GPU offload and a stable HTTP contract, so this
module stays a thin, well-validated client rather than an inference stack.

Failure is expected and handled explicitly. A small model will sometimes emit
malformed JSON, invent a field, or take too long. None of those may ever look
like a confident match, so every failure path returns a *degraded* evaluation
the caller can recognise, never a silent default.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import ValidationError

from jobhunter.ai.base import AIProvider
from jobhunter.classify.classifier import classify_job
from jobhunter.domain.enums import Language, Recommendation
from jobhunter.domain.evaluation import Decision, JobEvaluation
from jobhunter.domain.schemas import (
    CandidateSnapshot,
    ClassificationResult,
    MatchResult,
    NormalizedJob,
)
from jobhunter.logging_setup import get_logger
from jobhunter.matching.job_context import job_content_hash, render_job
from jobhunter.profile.context import render_candidate
from jobhunter.prompts import PromptTemplate, cover_letter_prompt, job_evaluation_prompt

log = get_logger(__name__)

DEFAULT_HOST = "http://127.0.0.1:11434"

# Bumped when the response schema changes shape or field order, both of which
# change what the model answers. Part of the evaluation cache key.
SCHEMA_VERSION = 2


class LocalModelUnavailableError(RuntimeError):
    """The Ollama server is not reachable or the model is not installed."""


@dataclass(frozen=True)
class LocalModelConfig:
    """Everything that affects the model's output, and so the cache key."""

    model: str = "qwen2.5:3b"
    host: str = DEFAULT_HOST
    timeout_seconds: float = 180.0
    # Deterministic decisions matter more than variety: the same job on two runs
    # must not flip between APPLY and SKIP.
    temperature: float = 0.0
    # Large enough that the longest real prompt plus the reply fits. Measured
    # over the labelled set the prompt reaches ~14.7k characters (~3.3k tokens);
    # at 4096 that plus num_predict overflowed and the context was silently
    # truncated, which costs the model the system prompt or the requirements.
    num_ctx: int = 6144
    num_predict: int = 900
    max_description_chars: int = 5000
    keep_alive: str = "10m"


def _requirement_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "requirement": {"type": "string"},
            "candidate_fit": {
                "type": "string",
                "enum": ["strong", "acceptable", "weak", "missing", "unknown"],
            },
            "evidence": {"type": "string"},
        },
        "required": ["requirement", "candidate_fit"],
    }


def _response_schema() -> dict[str, Any]:
    """JSON Schema handed to Ollama so the model is constrained, not merely asked.

    Written out flat rather than derived from the Pydantic model: Ollama compiles
    the schema to a GBNF grammar, and that compiler rejects ``$ref``, ``anyOf``
    and ``maxLength`` — all of which ``model_json_schema()`` emits. Constraining
    the model this way is what makes a 3B model's JSON reliable enough to use;
    :meth:`LocalModelProvider.parse` still validates, because a grammar
    guarantees shape but not sense.
    """
    return {
        "type": "object",
        "properties": {
            "is_it_role": {"type": "boolean"},
            "seniority": {
                "type": "string",
                "enum": [
                    "internship",
                    "entry",
                    "junior",
                    "junior_mid",
                    "mid",
                    "mid_senior",
                    "senior",
                    "lead",
                    "unknown",
                ],
            },
            "seniority_reasoning": {"type": "string"},
            "location_fit": {
                "type": "string",
                "enum": ["exact_city", "hybrid_city", "remote", "other_city", "unclear"],
            },
            "location_reasoning": {"type": "string"},
            "experience_fit": {
                "type": "string",
                "enum": ["good", "acceptable", "stretch", "insufficient", "unknown"],
            },
            # maxItems is load-bearing, not cosmetic: the arrays are what make
            # the response long, and an unbounded list runs the model past
            # num_predict and truncates the JSON mid-string.
            "mandatory_requirements": {
                "type": "array",
                "items": _requirement_schema(),
                "maxItems": 5,
            },
            "nice_to_have_requirements": {
                "type": "array",
                "items": _requirement_schema(),
                "maxItems": 4,
            },
            "major_strengths": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
            "major_risks": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
            "reasoning": {"type": "string"},
            "decision": {"type": "string", "enum": ["apply", "review", "skip"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "recommendation": {"type": "string"},
        },
        # Order matters: the grammar emits required properties in this sequence, so
        # listing the evidence first makes the model settle the facts before it
        # commits to a decision, rather than justifying a decision after the fact.
        "required": [
            "seniority",
            "seniority_reasoning",
            "location_fit",
            "location_reasoning",
            "experience_fit",
            "mandatory_requirements",
            "nice_to_have_requirements",
            "major_strengths",
            "major_risks",
            "is_it_role",
            "reasoning",
            "decision",
            "confidence",
            "recommendation",
        ],
    }


# Openers a model adds despite being told not to.
_LETTER_PREAMBLE = re.compile(
    r"^\s*(here(?:'s| is)[^\n]*|sure[^\n]*|certainly[^\n]*|cover letter:?)\s*\n+",
    re.IGNORECASE,
)


def _strip_letter_furniture(text: str) -> str:
    """Remove the scaffolding a model wraps a letter in."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        parts = cleaned.split("```")
        cleaned = parts[1] if len(parts) > 1 else cleaned.strip("`")
        cleaned = cleaned.removeprefix("text").removeprefix("markdown").strip()
    cleaned = _LETTER_PREAMBLE.sub("", cleaned)
    return cleaned.strip()


def _loads_or_repair(body: str) -> dict[str, Any] | None:
    """Parse the object, repairing a response that was cut off mid-generation.

    A small model that hits its token limit stops mid-string, leaving unbalanced
    braces. Everything it produced before that point is still usable — and
    because the schema puts the evidence fields first, a truncated response has
    usually already emitted the parts that matter. Closing the structure is worth
    far more than discarding the whole evaluation.
    """
    trimmed = body[: body.rfind("}") + 1] if "}" in body else body
    for candidate in (body, trimmed, _close_open_structures(body)):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _close_open_structures(body: str) -> str:
    """Best-effort completion of a truncated JSON object."""
    in_string = False
    escaped = False
    stack: list[str] = []

    for char in body:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char in "{[":
            stack.append(char)
        elif char in "}]" and stack:
            stack.pop()

    repaired = body
    if in_string:
        repaired += '"'
    # A trailing comma or dangling key would still be invalid, so drop them.
    repaired = repaired.rstrip().rstrip(",")
    if repaired.rstrip().endswith(":"):
        repaired = repaired.rstrip().rstrip(":")
        repaired = repaired[: repaired.rfind('"', 0, repaired.rfind('"'))].rstrip().rstrip(",")
    for opener in reversed(stack):
        repaired += "}" if opener == "{" else "]"
    return repaired


class LocalModelProvider(AIProvider):
    """Evaluates one job against one candidate with a local instruct model."""

    name = "local"

    def __init__(self, config: LocalModelConfig | None = None) -> None:
        self.config = config or LocalModelConfig()
        self.model = self.config.model
        self.prompt: PromptTemplate = job_evaluation_prompt()

    # ------------------------------------------------------------- health

    def is_available(self) -> bool:
        """Whether the server answers and has the configured model installed."""
        try:
            return self.config.model in self.installed_models()
        except Exception:
            return False

    def installed_models(self) -> list[str]:
        with httpx.Client(timeout=5.0) as client:
            response = client.get(f"{self.config.host}/api/tags")
            response.raise_for_status()
            return [m["name"] for m in response.json().get("models", [])]

    # ----------------------------------------------------------- inference

    def _payload(self, system: str, user: str, *, disable_thinking: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": _response_schema(),
            "keep_alive": self.config.keep_alive,
            "options": {
                "temperature": self.config.temperature,
                "num_ctx": self.config.num_ctx,
                "num_predict": self.config.num_predict,
            },
        }
        if disable_thinking:
            payload["think"] = False
        return payload

    def _chat(self, system: str, user: str) -> tuple[str, int]:
        """One completion. Returns the raw content and the latency in ms.

        Thinking is turned off wherever the model supports it. A reasoning model
        left to think spends its whole token budget on the reasoning and emits an
        empty answer — measured on qwen3:1.7b, which produced 3.1k characters of
        thinking and zero characters of JSON. Models that reject the option are
        retried without it, so this stays safe across the model zoo.
        """
        started = time.monotonic()
        with httpx.Client(timeout=self.config.timeout_seconds) as client:
            response = client.post(
                f"{self.config.host}/api/chat",
                json=self._payload(system, user, disable_thinking=True),
            )
            if response.status_code == 400 and "think" in response.text.lower():
                log.debug("local_model_thinking_unsupported", model=self.config.model)
                response = client.post(
                    f"{self.config.host}/api/chat",
                    json=self._payload(system, user, disable_thinking=False),
                )
            response.raise_for_status()
            body = response.json()

        latency_ms = int((time.monotonic() - started) * 1000)
        message = body.get("message", {})
        content = message.get("content", "")

        # Truncation is silent, so it has to be detected rather than hoped about:
        # a prompt that does not fit loses either the instructions or the
        # requirements, and the reply looks superficially fine either way.
        prompt_tokens = body.get("prompt_eval_count")
        if prompt_tokens and prompt_tokens + self.config.num_predict > self.config.num_ctx:
            log.warning(
                "local_model_context_overflow",
                model=self.config.model,
                prompt_tokens=prompt_tokens,
                num_predict=self.config.num_predict,
                num_ctx=self.config.num_ctx,
            )

        # A reasoning model that ignored the switch still leaves its answer
        # somewhere; prefer content, but do not silently treat thinking as none.
        if not content.strip() and message.get("thinking"):
            log.warning(
                "local_model_spent_budget_thinking",
                model=self.config.model,
                thinking_chars=len(message["thinking"]),
            )
        return content, latency_ms

    def evaluate(
        self,
        job: NormalizedJob,
        candidate: CandidateSnapshot,
        *,
        cv_text: str | None = None,
        classification: ClassificationResult | None = None,
    ) -> JobEvaluation:
        """Evaluate one job. Never raises: failures come back marked degraded."""
        if classification is None:
            classification = classify_job(
                job,
                target_locations=candidate.preferred_locations
                or ([candidate.location] if candidate.location else []),
                remote_ok=candidate.remote_ok,
            )

        user = self.prompt.render_user(
            candidate=render_candidate(candidate, cv_text=cv_text),
            job=render_job(
                job,
                max_description_chars=self.config.max_description_chars,
                classification=classification,
            ),
        )

        try:
            content, latency_ms = self._chat(self.prompt.system, user)
        except httpx.TimeoutException:
            return self._degraded(job, candidate, "model timed out")
        except Exception as exc:
            log.warning("local_model_call_failed", error=str(exc), job=job.fingerprint)
            return self._degraded(job, candidate, f"model call failed: {type(exc).__name__}")

        evaluation = self.parse(content)
        if evaluation is None:
            log.warning(
                "local_model_invalid_output",
                job=job.fingerprint,
                preview=content[:200],
            )
            return self._degraded(job, candidate, "model returned unusable JSON")

        return evaluation.model_copy(
            update={
                "source": self.name,
                "model": self.config.model,
                "prompt_version": self.prompt.identity,
                "job_content_hash": job_content_hash(job),
                "profile_version": candidate.version,
                "latency_ms": latency_ms,
            }
        )

    # ------------------------------------------------------------ parsing

    @staticmethod
    def parse(content: str) -> JobEvaluation | None:
        """Validate model output into an evaluation, or None if unusable.

        Tolerates the two things small models still do with a schema attached:
        wrapping the object in a code fence, and appending prose after it.
        """
        if not content or not content.strip():
            return None

        text = content.strip()
        if text.startswith("```"):
            text = text.split("```")[1] if "```" in text[3:] else text.lstrip("`")
            text = text.removeprefix("json").strip()

        start = text.find("{")
        if start == -1:
            return None
        body = text[start:]

        data = _loads_or_repair(body)
        if data is None:
            return None

        if not isinstance(data, dict):
            return None

        try:
            return JobEvaluation.model_validate(data)
        except ValidationError as exc:
            # Name the offending fields: "invalid output" alone is undiagnosable,
            # and which field a given model gets wrong is exactly what decides
            # whether the fix belongs in the prompt or in the schema.
            log.warning(
                "evaluation_schema_rejected",
                errors=[
                    {"field": ".".join(str(p) for p in err["loc"]), "problem": err["type"]}
                    for err in exc.errors()[:5]
                ],
            )
            return None

    def score_job(
        self,
        job: NormalizedJob,
        classification: ClassificationResult,
        candidate: CandidateSnapshot,
    ) -> MatchResult:
        """The old score-shaped view of an evaluation, for the legacy interface.

        Nothing in the decision path calls this — matching goes through
        :class:`~jobhunter.matching.evaluator.JobEvaluator`, which returns the
        full structured evaluation. It exists so this provider satisfies
        :class:`AIProvider` for the code that still speaks that language, and the
        number it reports is confidence, not a match percentage.
        """
        evaluation = self.evaluate(job, candidate)
        return MatchResult(
            score=round(evaluation.confidence * 100),
            confidence=evaluation.confidence,
            recommendation={
                Decision.APPLY: Recommendation.APPLY,
                Decision.REVIEW: Recommendation.REVIEW,
                Decision.SKIP: Recommendation.SKIP,
            }[evaluation.decision],
            strengths=list(evaluation.major_strengths),
            missing_skills=[r.requirement for r in evaluation.blocking_gaps],
            disqualifiers=list(evaluation.major_risks) if evaluation.degraded else [],
            reasoning=evaluation.reasoning,
            provider=self.name,
            model=self.config.model,
        )

    # ------------------------------------------------------- cover letters

    def generate_cover_letter(
        self,
        job: NormalizedJob,
        candidate: CandidateSnapshot,
        *,
        language: Language = Language.EN,
        max_words: int = 180,
        cv_text: str | None = None,
    ) -> str:
        """Write a letter grounded in the profile, or nothing at all.

        Returns an empty string on any failure rather than a generic letter. A
        template letter that says nothing specific is worse than none: the
        caller falls back to the deterministic writer, which at least only ever
        states facts from the profile.
        """
        prompt = cover_letter_prompt()
        user = prompt.render_user(
            language={Language.BG: "Bulgarian", Language.EN: "English"}.get(language, "English"),
            max_words=str(max_words),
            candidate=render_candidate(candidate, cv_text=cv_text),
            job=render_job(job, max_description_chars=2500),
        )

        try:
            with httpx.Client(timeout=self.config.timeout_seconds) as client:
                response = client.post(
                    f"{self.config.host}/api/chat",
                    json={
                        "model": self.config.model,
                        "messages": [
                            {"role": "system", "content": prompt.system},
                            {"role": "user", "content": user},
                        ],
                        "stream": False,
                        "think": False,
                        "keep_alive": self.config.keep_alive,
                        "options": {
                            "temperature": 0.3,
                            "num_ctx": self.config.num_ctx,
                            "num_predict": max(300, max_words * 3),
                        },
                    },
                )
                response.raise_for_status()
                text = response.json().get("message", {}).get("content", "")
        except Exception as exc:
            log.warning("cover_letter_failed", error=str(exc), job=job.fingerprint)
            return ""

        return _strip_letter_furniture(text)

    # ---------------------------------------------------------- degrading

    def _degraded(
        self, job: NormalizedJob, candidate: CandidateSnapshot, reason: str
    ) -> JobEvaluation:
        """What the model returns when it cannot answer.

        REVIEW, never SKIP and never APPLY: a model failure says nothing about
        the job, so it must neither hide a good listing nor endorse an unseen one.
        """
        return JobEvaluation(
            decision=Decision.REVIEW,
            confidence=0.0,
            recommendation="Could not be evaluated automatically - needs a look.",
            reasoning=f"The local model did not produce a usable assessment ({reason}).",
            source=self.name,
            model=self.config.model,
            prompt_version=self.prompt.identity,
            job_content_hash=job_content_hash(job),
            profile_version=candidate.version,
            degraded=True,
            degraded_reason=reason,
        )

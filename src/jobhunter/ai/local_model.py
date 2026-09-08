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
import time
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import ValidationError

from jobhunter.domain.evaluation import Decision, JobEvaluation
from jobhunter.domain.schemas import CandidateSnapshot, NormalizedJob
from jobhunter.logging_setup import get_logger
from jobhunter.matching.job_context import job_content_hash, render_job
from jobhunter.profile.context import render_candidate
from jobhunter.prompts import PromptTemplate, job_evaluation_prompt

log = get_logger(__name__)

DEFAULT_HOST = "http://127.0.0.1:11434"


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
    num_ctx: int = 4096
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
            "is_it_role",
            "seniority",
            "seniority_reasoning",
            "location_fit",
            "location_reasoning",
            "experience_fit",
            "mandatory_requirements",
            "nice_to_have_requirements",
            "major_strengths",
            "major_risks",
            "reasoning",
            "decision",
            "confidence",
            "recommendation",
        ],
    }


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


class LocalModelProvider:
    """Evaluates one job against one candidate with a local instruct model."""

    name = "local"

    def __init__(self, config: LocalModelConfig | None = None) -> None:
        self.config = config or LocalModelConfig()
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
    ) -> JobEvaluation:
        """Evaluate one job. Never raises: failures come back marked degraded."""
        user = self.prompt.render_user(
            candidate=render_candidate(candidate, cv_text=cv_text),
            job=render_job(job, max_description_chars=self.config.max_description_chars),
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

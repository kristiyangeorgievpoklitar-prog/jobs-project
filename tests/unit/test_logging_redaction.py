"""Secrets must never reach the logs."""

from __future__ import annotations

from jobhunter.logging_setup import REDACTED, redact_processor


def redact(event: dict) -> dict:
    return redact_processor(None, "info", event)


class TestRedaction:
    def test_redacts_sensitive_keys(self) -> None:
        out = redact({"event": "login", "password": "hunter2", "user": "kris"})
        assert out["password"] == REDACTED
        assert out["user"] == "kris"

    def test_redacts_cookies_and_tokens(self) -> None:
        out = redact({"cookie": "abc", "api_key": "sk-123", "authorization": "Bearer x"})
        assert all(out[k] == REDACTED for k in ("cookie", "api_key", "authorization"))

    def test_redacts_nested_dicts(self) -> None:
        out = redact({"payload": {"api_key": "secret", "safe": 1}})
        assert out["payload"]["api_key"] == REDACTED
        assert out["payload"]["safe"] == 1

    def test_redacts_inline_secrets_in_free_text(self) -> None:
        out = redact({"note": "connecting with token=deadbeef123 now"})
        assert "deadbeef123" not in out["note"]
        assert REDACTED in out["note"]

    def test_redacts_inside_lists(self) -> None:
        out = redact({"items": [{"password": "x"}, "password=abc123"]})
        assert out["items"][0]["password"] == REDACTED
        assert "abc123" not in out["items"][1]

    def test_leaves_ordinary_values_alone(self) -> None:
        out = redact({"job_id": 42, "title": "Junior PHP Developer"})
        assert out["job_id"] == 42
        assert out["title"] == "Junior PHP Developer"

    def test_case_insensitive_keys(self) -> None:
        assert redact({"PASSWORD": "x", "Api-Key": "y"})["PASSWORD"] == REDACTED

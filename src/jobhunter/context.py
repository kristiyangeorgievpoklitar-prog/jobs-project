"""Application wiring.

One place that builds the database, AI provider, notifier and matching engine
from settings, so the CLI, the web app and the scheduler all share a config.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import cached_property

from sqlalchemy.orm import Session

from jobhunter.ai.base import AIProvider
from jobhunter.ai.factory import build_provider, scoring_config_from_settings
from jobhunter.ai.local_model import LocalModelConfig, LocalModelProvider
from jobhunter.config import Settings, get_settings
from jobhunter.db.base import Database
from jobhunter.logging_setup import configure_logging, get_logger
from jobhunter.matching.engine import MatchingEngine
from jobhunter.matching.evaluator import JobEvaluator
from jobhunter.matching.gate import Stage1Gate
from jobhunter.matching.policy import DecisionPolicy
from jobhunter.matching.rules import ScoringConfig
from jobhunter.notifications.manager import NotificationManager

log = get_logger(__name__)


class AppContext:
    """Holds the long-lived collaborators for one process."""

    def __init__(self, settings: Settings | None = None, *, configure_logs: bool = True) -> None:
        self.settings = settings or get_settings()
        self.settings.ensure_directories()
        if configure_logs:
            configure_logging(
                self.settings.log_level,
                json_logs=self.settings.log_json,
                logs_dir=self.settings.logs_dir,
            )
        self.db = Database(self.settings.resolved_database_url)
        self._base_settings = self.settings
        self.reload_settings()

    def reload_settings(self) -> None:
        """Re-apply database-stored overrides on top of the .env settings."""
        from jobhunter.settings_store import apply_overrides, load_overrides

        try:
            with self.db.session() as session:
                overrides = load_overrides(session)
        except Exception as exc:  # the table may not exist before migrations
            log.debug("settings_overrides_unavailable", error=str(exc))
            return

        self.settings = apply_overrides(self._base_settings, overrides)
        # Anything derived from settings must be rebuilt.
        for attribute in (
            "scoring_config",
            "provider",
            "engine",
            "notifier",
            "local_model",
            "evaluator",
            "decision_policy",
        ):
            self.__dict__.pop(attribute, None)

    @cached_property
    def scoring_config(self) -> ScoringConfig:
        return scoring_config_from_settings(self.settings)

    @cached_property
    def provider(self) -> AIProvider:
        """The provider used for prose (cover letters).

        Matching does not come through here — that is :attr:`evaluator`. When the
        system is configured local, the local model writes the letters too, so
        the CV never has to leave the machine for either job.
        """
        if self.settings.ai_provider == "local":
            return self.local_model  # type: ignore[return-value]
        return build_provider(self.settings, self.scoring_config)

    @cached_property
    def engine(self) -> MatchingEngine:
        return MatchingEngine(self.provider, self.scoring_config)

    @cached_property
    def local_model(self) -> LocalModelProvider:
        return LocalModelProvider(
            LocalModelConfig(
                model=self.settings.local_model,
                host=self.settings.local_model_host,
                timeout_seconds=self.settings.local_model_timeout_seconds,
                num_ctx=self.settings.local_model_num_ctx,
                num_predict=self.settings.local_model_num_predict,
                max_description_chars=self.settings.ai_max_description_chars,
            )
        )

    @cached_property
    def decision_policy(self) -> DecisionPolicy:
        return DecisionPolicy(accept_remote=True)

    @cached_property
    def evaluator(self) -> JobEvaluator:
        """The two-stage matcher that drives the scan."""
        return JobEvaluator(self.local_model, gate=Stage1Gate(), policy=self.decision_policy)

    @cached_property
    def notifier(self) -> NotificationManager:
        return NotificationManager.from_settings(self.settings, self.db.session)

    @contextmanager
    def session(self) -> Iterator[Session]:
        with self.db.session() as session:
            yield session

    def close(self) -> None:
        self.db.dispose()

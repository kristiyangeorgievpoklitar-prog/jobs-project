"""Runtime settings overrides and scheduling."""

from __future__ import annotations

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from jobhunter.config import Settings
from jobhunter.scheduler.scheduler import build_trigger
from jobhunter.settings_store import apply_overrides, load_overrides, save_overrides


class TestSettingsStore:
    def test_saves_and_loads(self, session) -> None:
        save_overrides(session, {"review_threshold": "70", "auto_apply": "true"})
        loaded = load_overrides(session)
        assert loaded["review_threshold"] == 70
        assert loaded["auto_apply"] is True

    def test_ignores_unknown_keys(self, session) -> None:
        save_overrides(session, {"not_a_setting": "x", "database_url": "hack"})
        assert load_overrides(session) == {}

    def test_secrets_are_not_editable(self) -> None:
        from jobhunter.settings_store import EDITABLE_KEYS

        for secret in ("anthropic_api_key", "openai_api_key", "telegram_bot_token", "database_url"):
            assert secret not in EDITABLE_KEYS

    def test_rejects_malformed_values(self, session) -> None:
        save_overrides(session, {"review_threshold": "abc"})
        assert "review_threshold" not in load_overrides(session)

    def test_boolean_coercion(self, session) -> None:
        for raw, expected in [("on", True), ("1", True), ("false", False), ("", False)]:
            save_overrides(session, {"auto_apply": raw})
            assert load_overrides(session)["auto_apply"] is expected

    def test_apply_overrides_returns_new_settings(self, tmp_path) -> None:
        settings = Settings(_env_file=None, data_dir=tmp_path)
        updated = apply_overrides(settings, {"review_threshold": 60})
        assert updated.review_threshold == 60
        assert settings.review_threshold == 75  # original untouched

    def test_invalid_override_is_ignored(self, tmp_path) -> None:
        settings = Settings(_env_file=None, data_dir=tmp_path)
        updated = apply_overrides(settings, {"review_threshold": 9999})
        assert updated.review_threshold == 75


class TestThresholdSanity:
    def test_detects_inverted_thresholds(self, tmp_path) -> None:
        settings = Settings(
            _env_file=None, data_dir=tmp_path, auto_apply_threshold=50, review_threshold=80
        )
        assert settings.thresholds_are_sane() is False

    def test_normal_thresholds_are_sane(self, tmp_path) -> None:
        assert Settings(_env_file=None, data_dir=tmp_path).thresholds_are_sane() is True


class TestScheduler:
    def test_daily_uses_cron_at_the_configured_hour(self) -> None:
        trigger = build_trigger(24, 9)
        assert isinstance(trigger, CronTrigger)
        assert "hour='9'" in str(trigger)

    def test_sub_daily_uses_interval(self) -> None:
        assert isinstance(build_trigger(6, 9), IntervalTrigger)

    def test_multi_day_interval(self) -> None:
        assert isinstance(build_trigger(72, 9), CronTrigger)

    def test_default_is_once_per_day(self, tmp_path) -> None:
        assert Settings(_env_file=None, data_dir=tmp_path).scan_interval_hours == 24.0

    def test_scheduler_defaults_prevent_pile_up(self) -> None:
        from jobhunter.scheduler.scheduler import JOB_DEFAULTS

        assert JOB_DEFAULTS["max_instances"] == 1
        assert JOB_DEFAULTS["coalesce"] is True


class TestPolitenessDefaults:
    def test_request_delay_is_conservative(self, tmp_path) -> None:
        settings = Settings(_env_file=None, data_dir=tmp_path)
        assert settings.request_delay_seconds >= 3.0
        assert settings.respect_robots_txt is True

    def test_auto_apply_is_off_by_default(self, tmp_path) -> None:
        assert Settings(_env_file=None, data_dir=tmp_path).auto_apply is False

    def test_browser_is_headed_by_default(self, tmp_path) -> None:
        assert Settings(_env_file=None, data_dir=tmp_path).browser_headless is False

"""Anti-bot challenge detection."""

from __future__ import annotations

from jobhunter.browser.challenge import ChallengeType, detect_challenge


class TestChallengeDetection:
    def test_clean_page_is_not_a_challenge(self) -> None:
        result = detect_challenge(
            title="IT JOBS - 94 Обяви за работа за Варна",
            visible_text="Job listings " * 200,
        )
        assert result.detected is False

    def test_english_interstitial(self) -> None:
        result = detect_challenge(title="Just a moment...", visible_text="")
        assert result.type is ChallengeType.CLOUDFLARE_INTERSTITIAL
        assert result.is_blocking

    def test_bulgarian_interstitial(self) -> None:
        result = detect_challenge(title="Един момент...", visible_text="")
        assert result.type is ChallengeType.CLOUDFLARE_INTERSTITIAL

    def test_challenge_script_on_empty_page(self) -> None:
        result = detect_challenge(title="", visible_text="", html="<script>cf_chl_opt</script>")
        assert result.type is ChallengeType.CLOUDFLARE_INTERSTITIAL

    def test_recaptcha_script_on_a_real_page_is_not_a_challenge(self, apply_form_html: str) -> None:
        """The live apply form loads reCAPTCHA; that alone must not block us."""
        result = detect_challenge(
            title="IT JOBS - Кандидатстване Въпросник",
            visible_text="Въпросник " * 200,
            html=apply_form_html,
        )
        assert result.detected is False

    def test_visible_widget_is_a_captcha(self) -> None:
        result = detect_challenge(
            title="IT JOBS", visible_text="please verify", visible_captcha_widget=True
        )
        assert result.type is ChallengeType.CAPTCHA
        assert result.is_blocking

    def test_rate_limited_on_an_empty_page(self) -> None:
        assert detect_challenge(status=429).type is ChallengeType.RATE_LIMITED

    def test_access_denied_on_an_empty_page(self) -> None:
        assert detect_challenge(status=403).type is ChallengeType.ACCESS_DENIED

    def test_error_status_on_a_fully_rendered_page_is_not_a_block(self) -> None:
        """Cloudflare answers a cold profile with 403, then the page resolves.

        Judging the final page by the first response's status blocked every
        first run until this was fixed.
        """
        result = detect_challenge(
            title="IT JOBS - 94 Обяви за работа за Варна",
            visible_text="IT Обяви " * 400,
            status=403,
        )
        assert result.detected is False

    def test_denied_text_blocks_regardless_of_page_size(self) -> None:
        result = detect_challenge(visible_text="Access denied " + ("filler " * 400))
        assert result.type is ChallengeType.ACCESS_DENIED

    def test_login_required_is_detected_but_not_blocking(self) -> None:
        result = detect_challenge(visible_text="Моля, влезте в профила си")
        assert result.type is ChallengeType.LOGIN_REQUIRED
        assert result.is_blocking is False

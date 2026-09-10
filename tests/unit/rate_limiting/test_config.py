"""Unit tests for the auth-server rate-limit config helpers.

The target classifier lives in ``auth_server.server``, whose import needs the
auth_server sys.path setup; those tests live in
``tests/auth_server/unit/test_rate_limit_classifier.py``.
"""

import pytest


@pytest.mark.unit
class TestConfigDefaults:
    """Tests for the auth-server rate-limit config parsing helpers."""

    def test_env_bool_parsing(self, monkeypatch):
        """_env_bool parses true/false case-insensitively."""
        from auth_server import rate_limiting_config as cfg

        monkeypatch.setenv("X_TEST_FLAG", "TRUE")
        assert cfg._env_bool("X_TEST_FLAG", "false") is True
        monkeypatch.setenv("X_TEST_FLAG", "no")
        assert cfg._env_bool("X_TEST_FLAG", "false") is False

    def test_env_int_fallback_on_bad_value(self, monkeypatch):
        """_env_int falls back to the default on a non-integer value."""
        from auth_server import rate_limiting_config as cfg

        monkeypatch.setenv("X_TEST_INT", "notanint")
        assert cfg._env_int("X_TEST_INT", 250) == 250

    def test_get_rate_limiter_is_singleton(self):
        """get_rate_limiter returns the same instance across calls."""
        from auth_server import rate_limiting_config as cfg

        first = cfg.get_rate_limiter()
        second = cfg.get_rate_limiter()
        assert first is second

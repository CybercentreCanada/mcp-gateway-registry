"""Tests for the RUM snippet resolver and its security guardrails (issue #1471).

The resolver decodes RUM_SNIPPET_B64 and, when RUM_ALLOWED_HOSTS is set, rejects
any snippet that references a host outside the allowlist. It fails closed: on
invalid base64 or a disallowed host it returns a stub rather than the snippet.

These tests also assert the trust-boundary invariant: rum_snippet_b64 is an
env-sourced Settings field with no writable API surface, so it can only be set at
deploy time (same trust tier as SECRET_KEY).
"""

import base64

import pytest

from registry.utils.rum_snippet import (
    EMPTY_STUB,
    INVALID_STUB,
    resolve_rum_snippet,
)


def _b64(
    text: str,
) -> str:
    """Base64-encode a snippet string for the resolver.

    Args:
        text: Raw snippet text.

    Returns:
        Base64-encoded string.
    """
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


SNIPPET_SIGNALFX: str = (
    '<script src="https://cdn.signalfx.com/o11y-gdi-rum/latest/splunk-otel-web.js"></script>'
)


@pytest.mark.unit
class TestResolveRumSnippet:
    """Tests for resolve_rum_snippet()."""

    def test_empty_returns_empty_stub(self) -> None:
        """An unconfigured snippet returns the empty stub (RUM disabled)."""
        assert resolve_rum_snippet("", "") == EMPTY_STUB

    def test_valid_snippet_no_allowlist_is_served(self) -> None:
        """A decodable snippet with no allowlist is served verbatim."""
        result = resolve_rum_snippet(_b64(SNIPPET_SIGNALFX), "")
        assert result == SNIPPET_SIGNALFX

    def test_invalid_base64_fails_closed(self) -> None:
        """Non-base64 input returns the invalid stub, never raises."""
        assert resolve_rum_snippet("!!! not base64 !!!", "") == INVALID_STUB

    def test_allowed_host_is_served(self) -> None:
        """A snippet whose hosts are all on the allowlist is served."""
        result = resolve_rum_snippet(_b64(SNIPPET_SIGNALFX), "cdn.signalfx.com")
        assert result == SNIPPET_SIGNALFX

    def test_disallowed_host_fails_closed(self) -> None:
        """A snippet referencing an off-allowlist host returns the stub."""
        result = resolve_rum_snippet(_b64(SNIPPET_SIGNALFX), "example.com")
        assert result == INVALID_STUB

    def test_allowlist_is_case_insensitive(self) -> None:
        """Host matching ignores case on both the snippet and the allowlist."""
        snippet = '<script src="https://CDN.SignalFx.com/x.js"></script>'
        result = resolve_rum_snippet(_b64(snippet), "cdn.signalfx.com")
        assert result == snippet

    def test_allowlist_ignores_port_and_credentials(self) -> None:
        """Port and userinfo in the URL do not defeat the host allowlist."""
        snippet = '<script src="https://user:pass@cdn.signalfx.com:8443/x.js"></script>'
        # Host matches -> served.
        assert resolve_rum_snippet(_b64(snippet), "cdn.signalfx.com") == snippet
        # A different host with the same trick is still rejected.
        bad = '<script src="https://evil.example.com:443/x.js"></script>'
        assert resolve_rum_snippet(_b64(bad), "cdn.signalfx.com") == INVALID_STUB

    def test_multiple_hosts_all_must_be_allowed(self) -> None:
        """If any referenced host is off-list, the snippet fails closed."""
        snippet = (
            '<script src="https://cdn.signalfx.com/x.js"></script>'
            '<script>fetch("https://rum-ingest.us0.signalfx.com/v1/rum")</script>'
        )
        # Only one of the two hosts allowed -> rejected.
        assert resolve_rum_snippet(_b64(snippet), "cdn.signalfx.com") == INVALID_STUB
        # Both allowed -> served.
        both = "cdn.signalfx.com,rum-ingest.us0.signalfx.com"
        assert resolve_rum_snippet(_b64(snippet), both) == snippet


@pytest.mark.unit
class TestRumTrustBoundary:
    """Assert rum_snippet_b64 has no writable API surface (deploy-time only)."""

    def test_rum_snippet_is_env_sourced_settings_field(self) -> None:
        """rum_snippet_b64 lives on Settings (env-sourced), defaulting to empty.

        This is the trust-boundary guarantee: the value comes only from the
        process environment, never from a request body or a mutable store.
        """
        from registry.core.config import Settings

        assert "rum_snippet_b64" in Settings.model_fields
        assert Settings.model_fields["rum_snippet_b64"].default == ""
        assert "rum_allowed_hosts" in Settings.model_fields

    def test_no_config_route_writes_rum_snippet(self) -> None:
        """No config API route mutates rum_snippet_b64.

        The System Config page exposes it read-only and masked. Guard against a
        future regression that adds a write path by asserting the config routes
        module never assigns the field.
        """
        import inspect

        from registry.api import config_routes

        source = inspect.getsource(config_routes)
        # The field is exposed read-only as a CONFIG_GROUPS tuple entry
        # ("rum_snippet_b64", "RUM Snippet (base64)", True). It must never be
        # the target of an assignment (no write path).
        assert "settings.rum_snippet_b64 =" not in source
        assert "rum_snippet_b64 =" not in source
        # Confirm the read-only tuple registration is actually present.
        assert '"rum_snippet_b64"' in source

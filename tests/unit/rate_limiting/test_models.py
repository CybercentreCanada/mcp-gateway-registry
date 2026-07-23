"""Unit tests for rate-limit Pydantic models and their validation."""

import pytest
from pydantic import ValidationError

from registry.rate_limiting.models import RateLimitDecision, RateLimitDefinition


class TestRateLimitDefinition:
    """Tests for RateLimitDefinition validation and id construction."""

    def test_valid_caller_group_definition(self):
        """A caller/group definition with per-caller-type limits is accepted."""
        d = RateLimitDefinition(
            axis="caller",
            entity_type="group",
            name="developers",
            user_max_requests=100,
            agent_max_requests=50,
            window_seconds=60,
        )
        assert d.build_id() == "caller:group:developers:60"
        assert d.limit_for_caller_type("user") == 100
        assert d.limit_for_caller_type("agent") == 50

    def test_caller_group_requires_at_least_one_limit(self):
        """A group definition with neither user nor agent limit is rejected."""
        with pytest.raises(ValidationError, match="must set user_max_requests"):
            RateLimitDefinition(
                axis="caller", entity_type="group", name="developers", window_seconds=60
            )

    def test_caller_group_allows_user_only(self):
        """A group may set just the user limit (agent unset => agents ungated by it)."""
        d = RateLimitDefinition(
            axis="caller", entity_type="group", name="devs", user_max_requests=30
        )
        assert d.limit_for_caller_type("user") == 30
        assert d.limit_for_caller_type("agent") is None

    def test_valid_target_mcp_server_definition(self):
        """A target/mcp_server definition is accepted."""
        d = RateLimitDefinition(
            axis="target",
            entity_type="mcp_server",
            name="mcpgw",
            max_requests=500,
        )
        assert d.build_id() == "target:mcp_server:mcpgw:60"

    def test_valid_target_a2a_agent_definition(self):
        """An A2A agent target is a first-class entity type."""
        d = RateLimitDefinition(
            axis="target",
            entity_type="a2a_agent",
            name="booking-agent",
            max_requests=200,
            window_seconds=60,
        )
        assert d.entity_type == "a2a_agent"

    def test_daily_volume_window_is_allowed(self):
        """A full-day window (volume cap) is within bounds."""
        d = RateLimitDefinition(
            axis="caller",
            entity_type="group",
            name="developers",
            user_max_requests=5000,
            window_seconds=86400,
        )
        assert d.window_seconds == 86400

    def test_caller_axis_rejects_target_entity_type(self):
        """entity_type must be in the allowlist for its axis (fail closed)."""
        with pytest.raises(ValidationError, match="invalid for axis"):
            RateLimitDefinition(
                axis="caller",
                entity_type="mcp_server",
                name="x",
                max_requests=1,
            )

    def test_invalid_axis_rejected(self):
        """An unknown axis is rejected."""
        with pytest.raises(ValidationError, match="axis must be one of"):
            RateLimitDefinition(
                axis="bogus",
                entity_type="group",
                name="x",
                max_requests=1,
            )

    def test_window_over_one_day_rejected(self):
        """window_seconds above 86400 is out of bounds."""
        with pytest.raises(ValidationError):
            RateLimitDefinition(
                axis="caller",
                entity_type="group",
                name="x",
                user_max_requests=1,
                window_seconds=86401,
            )

    def test_limit_must_be_positive(self):
        """A limit below 1 is rejected."""
        with pytest.raises(ValidationError):
            RateLimitDefinition(
                axis="caller",
                entity_type="group",
                name="x",
                user_max_requests=0,
            )

    def test_empty_name_rejected(self):
        """An empty name is rejected."""
        with pytest.raises(ValidationError):
            RateLimitDefinition(
                axis="caller",
                entity_type="group",
                name="",
                user_max_requests=1,
            )


class TestRateLimitDecision:
    """Tests for decision construction and header rendering."""

    def test_allow_has_no_deny_headers(self):
        """An allow decision is allowed and carries best-effort remaining."""
        d = RateLimitDecision.allow(remaining=7)
        assert d.allowed is True
        assert d.remaining == 7

    def test_deny_builds_rate_limit_headers(self):
        """A deny decision renders the standard 429 headers."""
        d = RateLimitDecision(
            allowed=False,
            axis="clr",
            entity_type="group",
            limit=5,
            remaining=0,
            reset_epoch=1000,
            retry_after=30,
        )
        headers = d.headers()
        # X-RateLimit-Throttled is the marker nginx auth_request uses to tell a
        # throttle-403 apart from a genuine authorization 403 (issue #295): the
        # throttle leaves /validate as a 403 and nginx rewrites it into a 429.
        assert headers["X-RateLimit-Throttled"] == "1"
        assert headers["X-RateLimit-Limit"] == "5"
        assert headers["X-RateLimit-Remaining"] == "0"
        assert headers["X-RateLimit-Reset"] == "1000"
        assert headers["Retry-After"] == "30"
        assert headers["Connection"] == "close"

"""Unit tests for the /validate rate-limit target classifier (auth_server.server)."""

from unittest.mock import AsyncMock, patch

import pytest

from registry.rate_limiting.models import RateLimitDecision


@pytest.mark.unit
class TestResolveRateLimitCaller:
    """Caller classification must not treat a human's azp-derived client_id as an agent.

    Every OIDC token carries azp (the OAuth client), which the provider copies into
    validation_result['client_id']. A genuine machine token additionally has a
    top-level client_id CLAIM. The resolver must key humans on username (client_id
    None) and machines on their real client_id, so caller_type is correct.
    """

    def test_human_password_grant_is_user(self):
        """A browser/password-grant token (azp only, no client_id claim) -> user."""
        from auth_server.server import _resolve_rate_limit_caller

        validation_result = {
            "username": "rl-test-user",
            "client_id": "mcp-gateway-web",  # azp-derived; must NOT make it an agent
            "data": {"azp": "mcp-gateway-web", "preferred_username": "rl-test-user"},
        }
        username, client_id = _resolve_rate_limit_caller(validation_result)
        assert username == "rl-test-user"
        assert client_id is None  # dropped -> limiter takes the user branch

    def test_m2m_client_credentials_is_agent(self):
        """A client_credentials token (top-level client_id claim) -> agent."""
        from auth_server.server import _resolve_rate_limit_caller

        validation_result = {
            "username": "service-account-rl-test-m2m",
            "client_id": "rl-test-m2m",
            "data": {"azp": "rl-test-m2m", "client_id": "rl-test-m2m"},
        }
        username, client_id = _resolve_rate_limit_caller(validation_result)
        assert client_id == "rl-test-m2m"  # agent branch, keyed on the real client

    def test_no_claims_falls_back_to_user(self):
        """Missing data claims -> treated as a user keyed on username (no agent branch)."""
        from auth_server.server import _resolve_rate_limit_caller

        username, client_id = _resolve_rate_limit_caller(
            {"username": "someone", "client_id": "mcp-gateway-web"}
        )
        assert username == "someone"
        assert client_id is None

    def test_cognito_user_access_token_is_user(self):
        """Cognito user access token carries client_id AND username -> user.

        On Cognito both user and M2M access tokens carry a client_id claim, so
        the discriminator is the username claim (present only for a real user).
        Keying on client_id here would misclassify every Cognito user as an agent.
        """
        from auth_server.server import _resolve_rate_limit_caller

        validation_result = {
            "method": "cognito",
            "username": "74182448-f001-70bf-d6fa-062dabf0a2fa",
            "client_id": "7nostc9as6v2qbd44ar8ku42k3",
            "data": {
                "token_use": "access",
                "client_id": "7nostc9as6v2qbd44ar8ku42k3",
                "username": "74182448-f001-70bf-d6fa-062dabf0a2fa",
            },
        }
        username, client_id = _resolve_rate_limit_caller(validation_result)
        assert username == "74182448-f001-70bf-d6fa-062dabf0a2fa"
        assert client_id is None  # user branch, NOT keyed on the shared web client

    def test_cognito_m2m_token_is_agent(self):
        """Cognito client_credentials token has client_id but NO username -> agent."""
        from auth_server.server import _resolve_rate_limit_caller

        validation_result = {
            "method": "cognito",
            "username": None,
            "client_id": "rl-test-m2m-client",
            "data": {
                "token_use": "access",
                "client_id": "rl-test-m2m-client",
                "scope": "rl-test-api/invoke",
            },
        }
        username, client_id = _resolve_rate_limit_caller(validation_result)
        assert client_id == "rl-test-m2m-client"  # agent branch, keyed on the client

    def test_keycloak_service_account_subject_is_agent(self):
        """A Keycloak service-account-* subject with no top-level client_id -> agent."""
        from auth_server.server import _resolve_rate_limit_caller

        validation_result = {
            "method": "keycloak",
            "username": "service-account-rl-test-m2m",
            "client_id": "rl-test-m2m",
            "data": {"preferred_username": "service-account-rl-test-m2m"},
        }
        username, client_id = _resolve_rate_limit_caller(validation_result)
        assert client_id == "rl-test-m2m"  # agent branch


@pytest.mark.unit
class TestThrottlePassthrough:
    """The /validate throttle must surface as a 403 that nginx rewrites to 429.

    nginx's auth_request module only forwards 401/403 from the subrequest; a 429
    is turned into a 500 at the parent location ("auth request unexpected status:
    429"). So _enforce_rate_limit signals a throttle as a 403 carrying the
    X-RateLimit-* headers (incl. the X-RateLimit-Throttled marker), and the
    @forbidden_error nginx location rewrites it into a real 429 + Retry-After.
    This test is the regression guard for that exact status choice (issue #295).
    """

    async def test_throttle_raises_403_with_marker_header(self):
        """A denied decision raises HTTPException(403) with the throttle headers."""
        from fastapi import HTTPException

        from auth_server.server import _enforce_rate_limit

        denied = RateLimitDecision(
            allowed=False,
            axis="tgt",
            entity_type="mcp_server",
            limit=3,
            remaining=0,
            reset_epoch=1000,
            retry_after=42,
        )
        limiter = AsyncMock()
        limiter.check = AsyncMock(return_value=denied)

        # Force the enforcement path on: feature enabled, limiter returns a deny.
        with (
            patch("rate_limiting_config.RATE_LIMITING_ENABLED", True),
            patch("rate_limiting_config.get_rate_limiter", return_value=limiter),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await _enforce_rate_limit(
                    {"username": "alice", "is_admin": False},
                    "https://gw.example.com/mcpgw/mcp",
                    "mcpgw",
                )

        exc = exc_info.value
        # 403 (not 429) so nginx auth_request forwards it instead of 500-ing.
        assert exc.status_code == 403
        assert exc.headers["X-RateLimit-Throttled"] == "1"
        assert exc.headers["X-RateLimit-Limit"] == "3"
        assert exc.headers["Retry-After"] == "42"


@pytest.mark.unit
class TestClassifyTarget:
    """Tests for _classify_rate_limit_target."""

    def test_mcp_server_target(self):
        """A plain MCP server path classifies as mcp_server."""
        from auth_server.server import _classify_rate_limit_target

        entity_type, name = _classify_rate_limit_target("https://gw.example.com/mcpgw/mcp", "mcpgw")
        assert entity_type == "mcp_server"
        assert name == "mcpgw"

    def test_a2a_agent_target(self):
        """An /agent/ path classifies as a2a_agent with the agent path."""
        from auth_server.server import _classify_rate_limit_target

        entity_type, name = _classify_rate_limit_target(
            "https://gw.example.com/agent/booking-agent/", "booking-agent"
        )
        assert entity_type == "a2a_agent"
        assert name == "/booking-agent"

    def test_no_target(self):
        """A request with neither an agent path nor a server name yields (None, None)."""
        from auth_server.server import _classify_rate_limit_target

        entity_type, name = _classify_rate_limit_target(None, None)
        assert entity_type is None
        assert name is None

"""Unit tests for AuthMetricsMiddleware.classify_target_kind.

The auth-request metric carries a bounded ``target_kind`` label so routing
volume can be split by target type (A2A agent, virtual MCP server, MCP server)
without an unbounded per-target name. Classification is an ALLOWLIST derived
from the X-Original-URL path shape the nginx gateway forwards: a path is counted
as a data-plane target ONLY on an explicit match, so control-plane /api/* calls
are never miscounted as MCP-server traffic.
"""

import os
from unittest.mock import patch

from auth_server.metrics_middleware import AuthMetricsMiddleware


def _middleware() -> AuthMetricsMiddleware:
    # BaseHTTPMiddleware needs an app arg; a no-op placeholder is fine here.
    return AuthMetricsMiddleware(app=lambda *a, **k: None)


class TestClassifyA2AAgent:
    def test_agent_reverse_proxy_path(self) -> None:
        mw = _middleware()
        assert mw.classify_target_kind("http://localhost/agent/flight-booking/") == "a2a_agent"

    def test_agent_card_path(self) -> None:
        mw = _middleware()
        url = "http://localhost/agent/flight-booking/.well-known/agent-card.json"
        assert mw.classify_target_kind(url) == "a2a_agent"

    def test_multi_segment_agent_path(self) -> None:
        mw = _middleware()
        assert mw.classify_target_kind("http://localhost/agent/lob1/travel/") == "a2a_agent"

    def test_bare_agent_segment_is_not_agent(self) -> None:
        # "/agent" with no agent path fails the min-parts guard; it is not a
        # known target and (not being control plane) classifies as unknown.
        mw = _middleware()
        assert mw.classify_target_kind("http://localhost/agent") == "unknown"


class TestClassifyMcpServer:
    def test_mcp_transport_suffix(self) -> None:
        mw = _middleware()
        assert mw.classify_target_kind("http://localhost/mcpgw/mcp") == "mcp_server"

    def test_sse_transport_suffix(self) -> None:
        mw = _middleware()
        assert mw.classify_target_kind("http://localhost/currenttime/sse") == "mcp_server"

    def test_peer_registry_mcp_path(self) -> None:
        mw = _middleware()
        url = "http://localhost/peer-registry-lob-1/cloudflare-docs/mcp"
        assert mw.classify_target_kind(url) == "mcp_server"

    def test_server_without_transport_suffix_is_not_mcp_server(self) -> None:
        # No mcp/sse suffix -> not attributed to mcp_server (avoids counting a
        # bare/unknown path as server traffic).
        mw = _middleware()
        assert mw.classify_target_kind("http://localhost/some-server") == "unknown"


class TestClassifyVirtualServer:
    def test_virtual_server_path(self) -> None:
        mw = _middleware()
        url = "http://localhost/virtual/dev-essentials/mcp"
        assert mw.classify_target_kind(url) == "virtual_mcp_server"

    def test_virtual_server_without_transport(self) -> None:
        # The /virtual/{id} prefix identifies it regardless of transport suffix.
        mw = _middleware()
        assert mw.classify_target_kind("http://localhost/virtual/combined-tools/") == (
            "virtual_mcp_server"
        )


class TestControlPlaneNotMiscounted:
    """The core anti-miscounting guarantee: /api/* is never a data-plane target."""

    def test_api_prefix_is_control_plane(self) -> None:
        mw = _middleware()
        assert mw.classify_target_kind("http://localhost/api/agents") == "control_plane"

    def test_api_auth_me_is_control_plane(self) -> None:
        mw = _middleware()
        assert mw.classify_target_kind("http://localhost/api/auth/me") == "control_plane"

    def test_api_skills_crud_is_control_plane(self) -> None:
        # Skills have no data-plane proxy route; only /api/skills/... CRUD exists.
        mw = _middleware()
        assert mw.classify_target_kind("http://localhost/api/skills/docx") == "control_plane"

    def test_api_ard_is_control_plane(self) -> None:
        mw = _middleware()
        assert mw.classify_target_kind("http://localhost/api/ard/search") == "control_plane"

    def test_static_is_control_plane(self) -> None:
        mw = _middleware()
        assert mw.classify_target_kind("http://localhost/static/app.js") == "control_plane"

    def test_api_that_looks_like_server_is_still_control_plane(self) -> None:
        # A crafted /api/... path must not be attributed to mcp_server even if it
        # superficially resembles a server/transport shape.
        mw = _middleware()
        assert mw.classify_target_kind("http://localhost/api/mcp") == "control_plane"


class TestEdgeCases:
    def test_empty_path_is_unknown(self) -> None:
        mw = _middleware()
        assert mw.classify_target_kind("http://localhost/") == "unknown"

    def test_empty_url_is_unknown(self) -> None:
        mw = _middleware()
        assert mw.classify_target_kind("") == "unknown"


class TestRegistryRootPathPrefix:
    def test_prefix_stripped_for_agent(self) -> None:
        mw = _middleware()
        with patch.dict(os.environ, {"REGISTRY_ROOT_PATH": "/ai-registry"}):
            url = "http://localhost/ai-registry/agent/flight-booking/"
            assert mw.classify_target_kind(url) == "a2a_agent"

    def test_prefix_stripped_for_mcp(self) -> None:
        mw = _middleware()
        with patch.dict(os.environ, {"REGISTRY_ROOT_PATH": "/ai-registry"}):
            url = "http://localhost/ai-registry/mcpgw/mcp"
            assert mw.classify_target_kind(url) == "mcp_server"

    def test_prefix_stripped_for_control_plane(self) -> None:
        mw = _middleware()
        with patch.dict(os.environ, {"REGISTRY_ROOT_PATH": "/ai-registry"}):
            url = "http://localhost/ai-registry/api/agents"
            assert mw.classify_target_kind(url) == "control_plane"

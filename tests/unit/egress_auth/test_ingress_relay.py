"""ingress_relay egress mode: vend directive and configure write path.

ingress_relay tells auth_server to forward the caller's own bearer token to the
upstream. It is operator-controlled: the server path must be listed in
EGRESS_INGRESS_RELAY_ALLOWED_SERVERS (default: empty, deny everything), both when
it is configured and again when it is vended. Unlike the vault modes it needs no
per-user principal, so M2M and other non-per-user callers can use it.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import registry.api.egress_auth_routes as routes

_UPSTREAM = "https://rag.example.com/mcp"


class _StubRepo:
    def __init__(self, server):
        self._server = server

    async def get(self, path):
        return self._server


class _StubService:
    """Fails the test if the relay path touches the vault."""

    async def get_pat(self, **kwargs):
        raise AssertionError("ingress_relay must not read the vault")

    async def get_valid_token(self, **kwargs):
        raise AssertionError("ingress_relay must not read the vault")


def _server(**over):
    base = {
        "egress_auth_mode": "ingress_relay",
        "egress_oauth": None,
        "proxy_pass_url": _UPSTREAM,
        "versions": [],
    }
    base.update(over)
    return base


def _claims(**over):
    base = {
        "sub": "alice",
        "server": "rag",
        "version_id": "",
        "virtual_backend": False,
        "egress_user": "alice",
        "auth_method": "oauth2",
        "upstream_url": _UPSTREAM,
    }
    base.update(over)
    return base


@pytest.fixture
def vend_client(monkeypatch):
    def _build(claims, server, allowed="/rag"):
        monkeypatch.setattr(routes.settings, "egress_auth_enabled", True)
        monkeypatch.setattr(
            routes.settings, "egress_ingress_relay_allowed_servers", allowed, raising=False
        )
        monkeypatch.setattr(routes, "verify_mcp_proxy_token", lambda tok: claims)
        monkeypatch.setattr(routes, "get_server_repository", lambda: _StubRepo(server))
        monkeypatch.setattr(routes, "get_egress_auth_service", lambda: _StubService())

        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[routes.validate_internal_auth] = lambda: "auth-server"
        return TestClient(app)

    return _build


def _vend(client, server_path="/rag"):
    return client.post(
        "/internal/egress-token",
        json={"server_path": server_path},
        headers={"X-Internal-Token": "proxy-token"},
    )


@pytest.mark.unit
class TestIngressRelayVend:
    def test_allowlisted_server_gets_relay_directive(self, vend_client):
        r = _vend(vend_client(_claims(), _server()))
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "ingress_relay"
        assert body["access_token"] is None
        assert body["consent_required"] is False

    def test_non_per_user_caller_may_relay(self, vend_client):
        # M2M / non-user principals cannot use the vault, but they can relay.
        client = vend_client(_claims(auth_method="client_credentials", egress_user=None), _server())
        assert _vend(client).json()["mode"] == "ingress_relay"

    def test_allowlist_matches_without_leading_slash_and_ignoring_case(self, vend_client):
        client = vend_client(_claims(), _server(), allowed="RAG")
        assert _vend(client).json()["mode"] == "ingress_relay"

    def test_empty_allowlist_refuses(self, vend_client):
        assert _vend(vend_client(_claims(), _server(), allowed="")).status_code == 403

    def test_unlisted_server_refuses(self, vend_client):
        assert _vend(vend_client(_claims(), _server(), allowed="/other")).status_code == 403

    def test_upstream_mismatch_refuses(self, vend_client):
        client = vend_client(_claims(upstream_url="https://evil.example.com/mcp"), _server())
        assert _vend(client).status_code == 403

    def test_server_binding_mismatch_refuses(self, vend_client):
        assert _vend(vend_client(_claims(server="other"), _server())).status_code == 403

    def test_non_per_user_caller_still_gets_consent_for_vault_modes(self, vend_client):
        server = _server(egress_auth_mode="oauth_user", egress_oauth={"provider": "github"})
        client = vend_client(_claims(auth_method="client_credentials", egress_user=None), server)
        body = _vend(client).json()
        assert body["consent_required"] is True
        assert body["mode"] is None


class _StubServerService:
    def __init__(self):
        self.updated_with = None

    async def get_server_info(self, path, include_credentials=False):
        return {"path": "/rag", "egress_oauth": {"provider": "stale"}}

    async def update_server(self, path, server):
        self.updated_with = server
        return True


@pytest.fixture
def configure_client(monkeypatch):
    def _build(allowed):
        monkeypatch.setattr(routes.settings, "egress_auth_enabled", True)
        monkeypatch.setattr(
            routes.settings, "egress_ingress_relay_allowed_servers", allowed, raising=False
        )
        svc = _StubServerService()
        monkeypatch.setattr(routes, "server_service", svc)

        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[routes.nginx_proxied_auth] = lambda: {
            "username": "admin",
            "is_admin": True,
            "auth_method": "keycloak",
        }
        app.dependency_overrides[routes.verify_csrf_token_flexible] = lambda: None
        client = TestClient(app)
        client._svc = svc
        return client

    return _build


@pytest.mark.unit
class TestIngressRelayConfigure:
    def test_listed_server_is_configured_and_drops_stale_oauth(self, configure_client):
        client = configure_client("/rag")
        resp = client.post("/servers/rag/egress-auth", json={"egress_auth_mode": "ingress_relay"})
        assert resp.status_code == 200, resp.text
        assert client._svc.updated_with["egress_auth_mode"] == "ingress_relay"
        assert client._svc.updated_with["egress_oauth"] is None

    def test_unlisted_server_is_rejected_and_not_persisted(self, configure_client):
        client = configure_client("/other")
        resp = client.post("/servers/rag/egress-auth", json={"egress_auth_mode": "ingress_relay"})
        assert resp.status_code == 403
        assert client._svc.updated_with is None

    def test_empty_allowlist_rejects_every_server(self, configure_client):
        client = configure_client("")
        resp = client.post("/servers/rag/egress-auth", json={"egress_auth_mode": "ingress_relay"})
        assert resp.status_code == 403

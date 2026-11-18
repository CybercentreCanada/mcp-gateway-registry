"""Who may configure and read a server's egress auth.

Admins may do anything. The registrant of a server (with the modify permission)
may configure their own server, except obo_exchange: that mode re-audiences every
caller's gateway token to an audience the registrant picks, so it stays admin-only
(also when an admin already set it). Anyone else is refused, fail closed.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import registry.api.egress_auth_routes as routes

OWNER = "alice"


class _StubServerService:
    def __init__(self, server):
        self._server = server
        self.updated_with = None

    async def get_server_info(self, path, include_credentials=False):
        return dict(self._server)

    async def update_server(self, path, server):
        self.updated_with = server
        return True


def _user(username=OWNER, *, is_admin=False, can_modify=True):
    scope = {"modify_service": ["my-server"]} if can_modify else {}
    return {
        "username": username,
        "is_admin": is_admin,
        "auth_method": "keycloak",
        "ui_permissions": scope,
    }


@pytest.fixture
def make_client(monkeypatch):
    def _build(user, server=None):
        monkeypatch.setattr(routes.settings, "egress_auth_enabled", True)
        monkeypatch.setattr(
            routes.settings, "egress_ingress_relay_allowed_servers", "/my", raising=False
        )
        svc = _StubServerService(
            server
            or {
                "path": "/my",
                "server_name": "my-server",
                "registered_by": OWNER,
                "egress_auth_mode": "none",
                "egress_oauth": None,
            }
        )
        monkeypatch.setattr(routes, "server_service", svc)
        monkeypatch.setattr(routes, "encrypt_credential", lambda s: f"enc:{s}")

        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[routes.nginx_proxied_auth] = lambda: user
        app.dependency_overrides[routes.verify_csrf_token_flexible] = lambda: None
        client = TestClient(app)
        client._svc = svc
        return client

    return _build


def _configure(client, mode, **extra):
    return client.post("/servers/my/egress-auth", json={"egress_auth_mode": mode, **extra})


@pytest.mark.unit
class TestConfigure:
    @pytest.mark.parametrize("mode", ["none", "ingress_relay"])
    def test_registrant_may_set_simple_modes_on_own_server(self, make_client, mode):
        client = make_client(_user())
        assert _configure(client, mode).status_code == 200
        assert client._svc.updated_with["egress_auth_mode"] == mode

    def test_registrant_may_set_pat_on_own_server(self, make_client):
        client = make_client(_user())
        resp = _configure(client, "pat", egress_provider="github")
        assert resp.status_code == 200, resp.text

    def test_registrant_may_not_set_obo_exchange(self, make_client):
        client = make_client(_user())
        resp = _configure(client, "obo_exchange", target_audience="api://x")
        assert resp.status_code == 403
        assert client._svc.updated_with is None

    @pytest.mark.parametrize("mode", ["none", "pat", "ingress_relay"])
    def test_registrant_may_not_replace_an_admin_set_obo_config(self, make_client, mode):
        server = {
            "path": "/my",
            "server_name": "my-server",
            "registered_by": OWNER,
            "egress_auth_mode": "obo_exchange",
            "egress_oauth": {"target_audience": "api://x"},
        }
        client = make_client(_user(), server)
        assert _configure(client, mode, egress_provider="github").status_code == 403
        assert client._svc.updated_with is None

    def test_another_user_is_refused(self, make_client):
        client = make_client(_user("mallory"))
        assert _configure(client, "none").status_code == 403
        assert client._svc.updated_with is None

    def test_registrant_without_modify_permission_is_refused(self, make_client):
        client = make_client(_user(can_modify=False))
        assert _configure(client, "none").status_code == 403

    def test_server_without_registrant_is_refused_for_non_admins(self, make_client):
        server = {"path": "/my", "server_name": "my-server", "registered_by": None}
        client = make_client(_user(), server)
        assert _configure(client, "none").status_code == 403

    def test_admin_may_set_obo_exchange(self, make_client):
        client = make_client(_user("root", is_admin=True))
        resp = _configure(client, "obo_exchange", target_audience="api://outlook-mcp-server")
        assert resp.status_code == 200, resp.text

    def test_unknown_server_is_404(self, make_client):
        client = make_client(_user())
        client._svc.get_server_info = _none
        assert _configure(client, "none").status_code == 404


async def _none(*_args, **_kwargs):
    return None


@pytest.mark.unit
class TestRead:
    def test_registrant_may_read_own_server_config(self, make_client):
        assert make_client(_user()).get("/servers/my/egress-auth").status_code == 200

    def test_another_user_may_not_read(self, make_client):
        assert make_client(_user("mallory")).get("/servers/my/egress-auth").status_code == 403

    def test_admin_may_read(self, make_client):
        assert (
            make_client(_user("root", is_admin=True)).get("/servers/my/egress-auth").status_code
            == 200
        )

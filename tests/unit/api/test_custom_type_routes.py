"""Unit tests for registry/api/custom_type_routes.py.

Admin-only type-descriptor CRUD. A minimal FastAPI app mounts the router
directly (the production app only registers it behind a feature flag), with
the auth dependency overridden and the service patched out. Covers the admin
gate (403), happy-path create/list/get/delete, and the domain-error -> HTTP
status mapping (409 already-exists / has-records, 400 validation, 404 unknown).
"""

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from registry.api import custom_type_routes
from registry.api.custom_type_routes import router as custom_type_router
from registry.auth.dependencies import nginx_proxied_auth
from registry.schemas.custom_entity_models import (
    CustomFieldDescriptor,
    CustomFieldType,
    CustomTypeDescriptor,
)
from registry.services.custom_entity_errors import (
    CustomEntityValidationError,
    CustomTypeAlreadyExistsError,
    CustomTypeHasRecordsError,
)

logger = logging.getLogger(__name__)


ADMIN_CTX: dict[str, Any] = {"username": "admin", "is_admin": True, "groups": [], "scopes": []}
USER_CTX: dict[str, Any] = {"username": "bob", "is_admin": False, "groups": [], "scopes": []}


def _sample_descriptor(name: str = "workflow") -> CustomTypeDescriptor:
    return CustomTypeDescriptor(
        name=name,
        display_name="Workflow",
        fields=[CustomFieldDescriptor(name="title", datatype=CustomFieldType.STRING)],
    )


def _make_client(user_context: dict | None, service: MagicMock) -> TestClient:
    """Mount the router in a fresh app with auth + service stubbed."""
    app = FastAPI()
    app.include_router(custom_type_router, prefix="/api")
    app.dependency_overrides[nginx_proxied_auth] = lambda: user_context
    return TestClient(app)


@pytest.fixture
def service() -> MagicMock:
    svc = MagicMock()
    svc.list_types = AsyncMock(return_value=[_sample_descriptor()])
    svc.get_type = AsyncMock(return_value=_sample_descriptor())
    svc.create_type = AsyncMock(return_value=_sample_descriptor())
    svc.delete_type = AsyncMock(return_value=0)
    return svc


@pytest.fixture
def patched_scope_hooks():
    """Stub out the mint/cleanup/reload calls the type routes now make.

    Type create mints the per-type scope set (fatal on failure) and reloads the
    auth server; delete cleans up scopes and reloads. These tests focus on the
    route behavior, so the scope-store side effects are stubbed and their
    invocation asserted where relevant.
    """
    with (
        patch.object(
            custom_type_routes, "mint_custom_type_scopes", new=AsyncMock(return_value=True)
        ) as mint,
        patch.object(
            custom_type_routes, "cleanup_custom_type_scopes", new=AsyncMock(return_value=1)
        ) as cleanup,
        patch.object(
            custom_type_routes, "trigger_auth_server_reload", new=AsyncMock(return_value=True)
        ) as reload,
    ):
        yield {"mint": mint, "cleanup": cleanup, "reload": reload}


@pytest.fixture
def patched_service(service, patched_scope_hooks):
    with patch.object(custom_type_routes, "_get_service", return_value=service):
        yield service


@pytest.mark.unit
class TestListAndGet:
    def test_list_any_authed_user(self, patched_service):
        client = _make_client(USER_CTX, patched_service)
        resp = client.get("/api/custom-types")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_count"] == 1
        assert body["custom_types"][0]["name"] == "workflow"

    def test_get_existing(self, patched_service):
        client = _make_client(USER_CTX, patched_service)
        resp = client.get("/api/custom-types/workflow")
        assert resp.status_code == 200
        assert resp.json()["name"] == "workflow"

    def test_get_unknown_404(self, patched_service):
        patched_service.get_type = AsyncMock(return_value=None)
        client = _make_client(USER_CTX, patched_service)
        resp = client.get("/api/custom-types/missing")
        assert resp.status_code == 404

    def test_invalid_name_pattern_422(self, patched_service):
        client = _make_client(USER_CTX, patched_service)
        resp = client.get("/api/custom-types/Bad Name")
        assert resp.status_code == 422


@pytest.mark.unit
class TestCreate:
    def _payload(self) -> dict:
        return {
            "name": "workflow",
            "display_name": "Workflow",
            "fields": [{"name": "title", "datatype": "string"}],
        }

    def test_admin_creates(self, patched_service):
        client = _make_client(ADMIN_CTX, patched_service)
        resp = client.post("/api/custom-types", json=self._payload())
        assert resp.status_code == 201
        patched_service.create_type.assert_awaited_once()

    def test_create_mints_scopes(self, patched_service, patched_scope_hooks):
        client = _make_client(ADMIN_CTX, patched_service)
        resp = client.post("/api/custom-types", json=self._payload())
        assert resp.status_code == 201
        # The per-type scope set is minted for the created type name.
        patched_scope_hooks["mint"].assert_awaited_once_with("workflow")
        patched_scope_hooks["reload"].assert_awaited_once()

    def test_create_fails_when_mint_fails(self, patched_service, patched_scope_hooks):
        # Mint is FATAL: a type whose scopes could not be granted is unusable.
        from registry.services.scope_service import ScopeMintError

        patched_scope_hooks["mint"] = AsyncMock(side_effect=ScopeMintError("boom"))
        with patch.object(
            custom_type_routes, "mint_custom_type_scopes", patched_scope_hooks["mint"]
        ):
            client = _make_client(ADMIN_CTX, patched_service)
            resp = client.post("/api/custom-types", json=self._payload())
        assert resp.status_code == 500

    def test_create_rolls_back_descriptor_when_mint_fails(
        self, patched_service, patched_scope_hooks
    ):
        # A mint failure must not leave an orphaned descriptor behind (an
        # identical retry would otherwise 409). The route deletes the just-created
        # type so create stays atomic.
        from registry.services.scope_service import ScopeMintError

        patched_scope_hooks["mint"] = AsyncMock(side_effect=ScopeMintError("boom"))
        with patch.object(
            custom_type_routes, "mint_custom_type_scopes", patched_scope_hooks["mint"]
        ):
            client = _make_client(ADMIN_CTX, patched_service)
            resp = client.post("/api/custom-types", json=self._payload())
        assert resp.status_code == 500
        patched_service.delete_type.assert_awaited_once_with("workflow", force=False)

    def test_create_survives_rollback_failure(self, patched_service, patched_scope_hooks):
        # If the rollback delete itself fails, the route still returns 500 (it does
        # not mask the original mint failure with a rollback error).
        from registry.services.scope_service import ScopeMintError

        patched_service.delete_type = AsyncMock(side_effect=RuntimeError("db down"))
        patched_scope_hooks["mint"] = AsyncMock(side_effect=ScopeMintError("boom"))
        with patch.object(
            custom_type_routes, "mint_custom_type_scopes", patched_scope_hooks["mint"]
        ):
            client = _make_client(ADMIN_CTX, patched_service)
            resp = client.post("/api/custom-types", json=self._payload())
        assert resp.status_code == 500

    def test_non_admin_forbidden(self, patched_service):
        client = _make_client(USER_CTX, patched_service)
        resp = client.post("/api/custom-types", json=self._payload())
        assert resp.status_code == 403
        patched_service.create_type.assert_not_awaited()

    def test_duplicate_409(self, patched_service):
        patched_service.create_type = AsyncMock(
            side_effect=CustomTypeAlreadyExistsError("workflow")
        )
        client = _make_client(ADMIN_CTX, patched_service)
        resp = client.post("/api/custom-types", json=self._payload())
        assert resp.status_code == 409

    def test_validation_error_400(self, patched_service):
        patched_service.create_type = AsyncMock(
            side_effect=CustomEntityValidationError(errors=[{"field": "x", "message": "bad"}])
        )
        client = _make_client(ADMIN_CTX, patched_service)
        resp = client.post("/api/custom-types", json=self._payload())
        assert resp.status_code == 400


@pytest.mark.unit
class TestDelete:
    def test_admin_deletes(self, patched_service):
        client = _make_client(ADMIN_CTX, patched_service)
        resp = client.delete("/api/custom-types/workflow")
        assert resp.status_code == 204
        patched_service.delete_type.assert_awaited_once_with("workflow", force=False)

    def test_delete_cleans_up_scopes(self, patched_service, patched_scope_hooks):
        client = _make_client(ADMIN_CTX, patched_service)
        resp = client.delete("/api/custom-types/workflow")
        assert resp.status_code == 204
        patched_scope_hooks["cleanup"].assert_awaited_once_with("workflow")
        patched_scope_hooks["reload"].assert_awaited_once()

    def test_non_admin_forbidden(self, patched_service):
        client = _make_client(USER_CTX, patched_service)
        resp = client.delete("/api/custom-types/workflow")
        assert resp.status_code == 403

    def test_has_records_without_force_409(self, patched_service):
        patched_service.delete_type = AsyncMock(
            side_effect=CustomTypeHasRecordsError("workflow", 3)
        )
        client = _make_client(ADMIN_CTX, patched_service)
        resp = client.delete("/api/custom-types/workflow")
        assert resp.status_code == 409

    def test_force_passes_through(self, patched_service):
        client = _make_client(ADMIN_CTX, patched_service)
        resp = client.delete("/api/custom-types/workflow?force=true")
        assert resp.status_code == 204
        patched_service.delete_type.assert_awaited_once_with("workflow", force=True)

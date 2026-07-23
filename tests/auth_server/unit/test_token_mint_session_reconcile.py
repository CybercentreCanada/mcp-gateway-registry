"""Unit tests for the internal token-mint context reconciliation.

The internal /tokens endpoint stamps groups/scopes from the request body into
the minted JWT. When the body carries a session_id, the groups/scopes must be
reconciled against the authoritative session store rather than trusted from the
body, so a forged context cannot inject privileges the session never granted.
The body's shape is also validated fail-closed.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from auth_server.server import (
    _reconcile_context_against_session,
    _validate_context_group_scope_shape,
)

pytestmark = [pytest.mark.unit, pytest.mark.auth]


class TestContextShapeValidation:
    """Malformed groups/scopes shapes fail closed with a 400."""

    def test_valid_lists_pass(self):
        _validate_context_group_scope_shape({"groups": ["a", "b"], "scopes": ["s/read"]})

    def test_missing_keys_allowed(self):
        _validate_context_group_scope_shape({"username": "alice"})

    @pytest.mark.parametrize(
        "ctx",
        [
            {"groups": "admin"},  # scalar, not a list
            {"scopes": "s/read"},
            {"groups": ["ok", None]},  # non-string element
            {"groups": ["ok", ""]},  # empty-string element
            {"scopes": [1, 2]},  # non-string elements
        ],
    )
    def test_malformed_rejected(self, ctx):
        with pytest.raises(HTTPException) as exc:
            _validate_context_group_scope_shape(ctx)
        assert exc.value.status_code == 400


class TestSessionReconciliation:
    @pytest.mark.asyncio
    async def test_no_session_id_uses_body_as_is(self):
        ctx = {"groups": ["developers"], "scopes": ["srv/read"]}
        groups, scopes = await _reconcile_context_against_session(ctx)
        assert groups == ["developers"]
        assert scopes == ["srv/read"]

    @pytest.mark.asyncio
    async def test_session_id_that_does_not_resolve_fails_closed(self):
        ctx = {"groups": ["developers"], "scopes": ["srv/read"], "session_id": "s1"}
        with patch("session_store.resolve_session", AsyncMock(return_value=None)):
            with pytest.raises(HTTPException) as exc:
                await _reconcile_context_against_session(ctx)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_forged_privileged_group_rejected(self):
        # Body claims registry-admins but the session only holds developers.
        ctx = {
            "groups": ["registry-admins"],
            "scopes": [],
            "session_id": "s1",
        }
        session = {"username": "alice", "groups": ["developers"]}
        with patch("session_store.resolve_session", AsyncMock(return_value=session)):
            with pytest.raises(HTTPException) as exc:
                await _reconcile_context_against_session(ctx)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_groups_intersected_with_session(self):
        # Body claims two groups; only the session-held one survives.
        ctx = {
            "groups": ["developers", "testers"],
            "scopes": [],
            "session_id": "s1",
        }
        session = {"username": "alice", "groups": ["developers"]}
        with (
            patch("session_store.resolve_session", AsyncMock(return_value=session)),
            patch(
                "auth_server.server.map_groups_to_scopes",
                AsyncMock(return_value=["srv/read"]),
            ),
        ):
            groups, scopes = await _reconcile_context_against_session(ctx)
        assert groups == ["developers"]
        assert scopes == ["srv/read"]

    @pytest.mark.asyncio
    async def test_privileged_group_allowed_when_session_holds_it(self):
        ctx = {
            "groups": ["registry-admins"],
            "scopes": [],
            "session_id": "s1",
        }
        session = {"username": "alice", "groups": ["registry-admins", "developers"]}
        with (
            patch("session_store.resolve_session", AsyncMock(return_value=session)),
            patch(
                "auth_server.server.map_groups_to_scopes",
                AsyncMock(return_value=["admin/all"]),
            ),
        ):
            groups, scopes = await _reconcile_context_against_session(ctx)
        assert groups == ["registry-admins"]
        assert scopes == ["admin/all"]

    @pytest.mark.asyncio
    async def test_empty_body_groups_defaults_to_session_groups(self):
        ctx = {"groups": [], "scopes": [], "session_id": "s1"}
        session = {"username": "alice", "groups": ["developers"]}
        with (
            patch("session_store.resolve_session", AsyncMock(return_value=session)),
            patch(
                "auth_server.server.map_groups_to_scopes",
                AsyncMock(return_value=["srv/read"]),
            ),
        ):
            groups, scopes = await _reconcile_context_against_session(ctx)
        assert groups == ["developers"]
        assert scopes == ["srv/read"]

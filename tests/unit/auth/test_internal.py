"""Unit tests for registry/auth/internal.py header-parsing helper.

The public ``validate_internal_auth`` FastAPI dependency is exercised
end-to-end by ``tests/auth_server/unit/test_server.py::TestInternalRouterGate``
which calls through the full HTTP stack and the router-level gate.

These tests hit the private helper ``_validate_authorization_header``
directly so a regression in the header-parsing logic fails a focused
unit test rather than an HTTP meta-test. Localized, fast, and targeted.

Issue #998.
"""

import os
from unittest.mock import patch

import jwt as pyjwt
import pytest
from fastapi import HTTPException

from registry.auth.internal import (
    _derive_internal_signing_key,
    _validate_authorization_header,
    _validate_bearer_token,
    generate_internal_token,
)


class TestValidateAuthorizationHeader:
    """Direct tests for the header-parsing helper."""

    def test_none_raises_401_missing_header(self) -> None:
        """When no Authorization header is present on the request, the
        helper must reject with 401 and the "Missing authorization header"
        detail (so the router-level dependency returns a consistent error
        to callers)."""
        with pytest.raises(HTTPException) as exc_info:
            _validate_authorization_header(None)
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Missing authorization header"
        assert exc_info.value.headers == {"WWW-Authenticate": "Bearer"}

    def test_empty_string_raises_401_missing_header(self) -> None:
        """An empty string is semantically the same as no header; the
        helper must treat it the same way. Otherwise an upstream bug
        that substitutes '' for None could silently leak through."""
        with pytest.raises(HTTPException) as exc_info:
            _validate_authorization_header("")
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Missing authorization header"

    def test_basic_auth_scheme_raises_401_unsupported(self) -> None:
        """Non-Bearer schemes must be rejected. This is the defense against
        a caller mistakenly sending HTTP Basic to an endpoint that requires
        the signed internal JWT."""
        with pytest.raises(HTTPException) as exc_info:
            _validate_authorization_header("Basic YWxpY2U6cGFzcw==")
        assert exc_info.value.status_code == 401
        assert "Unsupported authentication scheme" in exc_info.value.detail

    def test_bearer_with_empty_token_raises_401_invalid(self) -> None:
        """'Bearer ' (trailing space, empty token) passes the ``startswith``
        check but fails inside ``_validate_bearer_token`` because pyjwt
        cannot decode an empty string. Must surface as a 401, not a 500.

        Needs SECRET_KEY set for ``_validate_bearer_token`` to proceed
        past its own config check; the actual JWT decode is what fails."""
        with patch.dict(os.environ, {"SECRET_KEY": "x" * 32}):
            with pytest.raises(HTTPException) as exc_info:
                _validate_authorization_header("Bearer ")
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Invalid token"


class TestInternalTokenAttribution:
    """Internal tokens must be attributable to a specific replica/instance.

    Regression guard for the repudiation-hardening change: internal service
    tokens historically all carried the same ``sub`` (``registry-service``), so
    an action performed by one replica was indistinguishable from any other's.
    The mint now qualifies ``sub`` with a per-instance id and carries a separate
    ``instance_id`` claim, and the validator returns that attributable subject
    (which flows into the audit trail's ``internal_caller`` field).
    """

    _SECRET = "test-secret-key-that-is-definitely-long-enough-32b"

    def _decode(self, token: str) -> dict:
        signing_key = _derive_internal_signing_key(self._SECRET)
        return pyjwt.decode(
            token,
            signing_key,
            algorithms=["HS256"],
            issuer="mcp-auth-server",
            audience="mcp-internal",
        )

    def test_sub_is_qualified_by_instance_id(self) -> None:
        """``sub`` embeds the per-instance id so the caller replica is identifiable."""
        with patch.dict(
            os.environ,
            {"SECRET_KEY": self._SECRET, "AUDIT_INSTANCE_ID": "registry-blue-3"},
        ):
            token = generate_internal_token(subject="registry-service", purpose="reload-scopes")
            claims = self._decode(token)

        assert claims["sub"] == "registry-service@registry-blue-3"
        assert claims["service"] == "registry-service"
        assert claims["instance_id"] == "registry-blue-3"
        assert claims["purpose"] == "reload-scopes"

    def test_distinct_instances_produce_distinct_subjects(self) -> None:
        """Two replicas minting the same logical token get distinct ``sub`` values.

        This is the property the vulnerable code lacked: with a shared
        ``sub='registry-service'`` both tokens were identical and unattributable.
        """
        with patch.dict(os.environ, {"SECRET_KEY": self._SECRET, "AUDIT_INSTANCE_ID": "inst-a"}):
            claims_a = self._decode(
                generate_internal_token(subject="registry-service", purpose="generate-token")
            )
        with patch.dict(os.environ, {"SECRET_KEY": self._SECRET, "AUDIT_INSTANCE_ID": "inst-b"}):
            claims_b = self._decode(
                generate_internal_token(subject="registry-service", purpose="generate-token")
            )

        assert claims_a["sub"] != claims_b["sub"]
        assert claims_a["sub"] == "registry-service@inst-a"
        assert claims_b["sub"] == "registry-service@inst-b"

    def test_validate_returns_attributable_subject(self) -> None:
        """The validator surfaces the instance-qualified subject to callers.

        This is what lands in the token-mint audit record's ``internal_caller``.
        """
        with patch.dict(
            os.environ,
            {"SECRET_KEY": self._SECRET, "AUDIT_INSTANCE_ID": "registry-green-9"},
        ):
            token = generate_internal_token(subject="auth-server", purpose="egress-token-vend")
            caller, claims = _validate_bearer_token(f"Bearer {token}")

        assert caller == "auth-server@registry-green-9"
        assert claims["instance_id"] == "registry-green-9"

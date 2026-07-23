"""Unit tests for auth_server._emit_token_mint_audit (#1308).

Verifies the metric labels, the audit record fields (including username
hashing), and the best-effort contract: a failure in the metric or audit sink
must never propagate out of the emit helper, so token minting is never broken
by observability.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auth_server.server import _emit_token_mint_audit, hash_username

pytestmark = [pytest.mark.unit, pytest.mark.auth]


_COMMON = {
    "request_id": "req-1",
    "correlation_id": "corr-1",
    "username": "alice@example.com",
    "auth_method": "oauth2",
    "provider": "keycloak",
    "internal_caller": "registry",
    "requested_scopes": ["mcp-servers-unrestricted/read"],
    "expires_in_seconds": 3600,
}


@patch("auth_server.server.get_audit_logger", return_value=None)
@patch("auth_server.server.emit_audit_event")
@patch("auth_server.server.token_mint_total")
async def test_success_increments_metric_with_labels(mock_metric, mock_emit, _mock_logger):
    await _emit_token_mint_audit(
        token_kind="user",
        resource_type=None,
        resource_id=None,
        token_path="self_signed",
        outcome="success",
        **_COMMON,
    )
    # resource_type None must collapse to the "none" label (not an empty string).
    mock_metric.add.assert_called_once_with(
        1,
        {
            "token_kind": "user",
            "resource_type": "none",
            "token_path": "self_signed",
            "outcome": "success",
        },
    )
    mock_emit.assert_called_once()


@patch("auth_server.server.get_audit_logger", return_value=None)
@patch("auth_server.server.emit_audit_event")
@patch("auth_server.server.token_mint_total")
async def test_resource_label_uses_value_when_set(mock_metric, _mock_emit, _mock_logger):
    await _emit_token_mint_audit(
        token_kind="resource",
        resource_type="server",
        resource_id="fininfo",
        token_path="self_signed",
        outcome="success",
        **_COMMON,
    )
    labels = mock_metric.add.call_args.args[1]
    assert labels["resource_type"] == "server"
    assert labels["token_kind"] == "resource"


@patch("auth_server.server.get_audit_logger", return_value=None)
@patch("auth_server.server.emit_audit_event")
@patch("auth_server.server.token_mint_total")
async def test_record_username_is_hashed_never_raw(_mock_metric, mock_emit, _mock_logger):
    await _emit_token_mint_audit(
        token_kind="user",
        resource_type=None,
        resource_id=None,
        token_path="m2m",
        outcome="success",
        **_COMMON,
    )
    record = mock_emit.call_args.args[0]
    assert record.username_hash == hash_username("alice@example.com")
    assert record.username_hash.startswith("user_")
    assert "alice@example.com" not in record.username_hash
    assert record.correlation_id == "corr-1"
    assert record.outcome == "success"


@patch("auth_server.server.emit_audit_event")
@patch("auth_server.server.token_mint_total")
async def test_record_written_to_audit_logger_when_present(mock_metric, mock_emit):
    audit_logger = MagicMock()
    audit_logger.log_event = AsyncMock()
    with patch("auth_server.server.get_audit_logger", return_value=audit_logger):
        await _emit_token_mint_audit(
            token_kind="user",
            resource_type=None,
            resource_id=None,
            token_path="self_signed",
            outcome="failure",
            failure_reason="rate_limited",
            **_COMMON,
        )
    audit_logger.log_event.assert_awaited_once()
    written = audit_logger.log_event.call_args.args[0]
    assert written.outcome == "failure"
    assert written.failure_reason == "rate_limited"


@patch("auth_server.server.get_audit_logger", return_value=None)
@patch("auth_server.server.emit_audit_event")
@patch("auth_server.server.token_mint_total")
async def test_metric_failure_is_swallowed_and_audit_still_emitted(
    mock_metric, mock_emit, _mock_logger
):
    # A broken metric backend must not stop the audit record nor raise.
    mock_metric.add.side_effect = RuntimeError("otel down")
    await _emit_token_mint_audit(
        token_kind="user",
        resource_type=None,
        resource_id=None,
        token_path="self_signed",
        outcome="success",
        **_COMMON,
    )
    mock_emit.assert_called_once()


@patch("auth_server.server.get_audit_logger", return_value=None)
@patch("auth_server.server.emit_audit_event")
@patch("auth_server.server.token_mint_total")
async def test_audit_sink_failure_is_swallowed(_mock_metric, mock_emit, _mock_logger):
    # A broken audit sink must be swallowed; the helper returns None, never raises.
    mock_emit.side_effect = RuntimeError("sink down")
    result = await _emit_token_mint_audit(
        token_kind="user",
        resource_type=None,
        resource_id=None,
        token_path="self_signed",
        outcome="success",
        **_COMMON,
    )
    assert result is None


@patch("auth_server.server.get_audit_logger", return_value=None)
@patch("auth_server.server.emit_audit_event")
@patch("auth_server.server.token_mint_total")
async def test_display_username_stored_raw_and_hash_still_populated(
    _mock_metric, mock_emit, _mock_logger
):
    # The raw human-readable field is stored verbatim while the deprecated
    # username_hash keeps being derived from `username` (back-compat).
    await _emit_token_mint_audit(
        token_kind="user",
        resource_type=None,
        resource_id=None,
        token_path="self_signed",
        outcome="success",
        display_username="alice@example.com",
        **_COMMON,
    )
    record = mock_emit.call_args.args[0]
    assert record.username == "alice@example.com"  # raw, human-readable
    assert record.username_hash == hash_username("alice@example.com")  # still hashed
    assert record.username_hash.startswith("user_")


@patch("auth_server.server.get_audit_logger", return_value=None)
@patch("auth_server.server.emit_audit_event")
@patch("auth_server.server.token_mint_total")
async def test_display_username_falls_back_to_username(_mock_metric, mock_emit, _mock_logger):
    # When no display identity is supplied, the raw field falls back to the
    # (possibly opaque) username so a record is never left blank.
    await _emit_token_mint_audit(
        token_kind="user",
        resource_type=None,
        resource_id=None,
        token_path="self_signed",
        outcome="success",
        **_COMMON,
    )
    record = mock_emit.call_args.args[0]
    assert record.username == "alice@example.com"


class TestMcpLoggerDurabilityGuard:
    """The auth-server owns the token-mint audit trail — the most forensically
    critical records — so its MCP audit logger must obey the same durable-sink
    fail-closed guard as the registry process (registry/main.py). Without it a
    MongoDB misconfiguration on the auth-server would silently drop every
    token-issuance record while the operator believes AUDIT_LOG_REQUIRE_DURABLE
    is protecting them.
    """

    def _reset_mcp_logger_globals(self) -> None:
        import auth_server.server as srv

        srv._mcp_logger = None
        srv._mcp_audit_logger = None
        srv._mcp_audit_repository = None

    def test_no_durable_sink_fails_closed_by_default(self) -> None:
        """Audit enabled + no durable sink + require_durable=True -> refuse to init."""
        import auth_server.server as srv
        from registry.audit.service import NonDurableAuditError

        self._reset_mcp_logger_globals()
        with (
            patch.object(srv.settings, "audit_log_enabled", True),
            patch.object(srv.settings, "audit_log_mongodb_enabled", False),
            patch.object(srv.settings, "audit_log_require_durable", True),
        ):
            with pytest.raises(NonDurableAuditError):
                srv.get_mcp_logger()

        # Fail-closed must not leave a half-initialized logger cached.
        assert srv._mcp_logger is None
        self._reset_mcp_logger_globals()

    def test_no_durable_sink_allowed_on_explicit_optout(self) -> None:
        """require_durable=False lets it degrade to a best-effort logger, no raise."""
        import auth_server.server as srv

        self._reset_mcp_logger_globals()
        with (
            patch.object(srv.settings, "audit_log_enabled", True),
            patch.object(srv.settings, "audit_log_mongodb_enabled", False),
            patch.object(srv.settings, "audit_log_require_durable", False),
        ):
            logger_obj = srv.get_mcp_logger()

        assert logger_obj is not None
        self._reset_mcp_logger_globals()

    def test_disabled_audit_never_triggers_guard(self) -> None:
        """When audit logging is disabled the guard is not reached (no raise)."""
        import auth_server.server as srv

        self._reset_mcp_logger_globals()
        with (
            patch.object(srv.settings, "audit_log_enabled", False),
            patch.object(srv.settings, "audit_log_require_durable", True),
        ):
            # Disabled -> returns None without raising.
            assert srv.get_mcp_logger() is None
        self._reset_mcp_logger_globals()

"""Unit tests for the per-instance identity helper.

``resolve_instance_id`` is the single blessed source of the per-replica label
used to attribute internal actions and audit records to a specific caller. It
must never raise and must always return a non-empty value so attribution
degrades gracefully rather than breaking token minting or audit logging.
"""

from unittest.mock import patch

from registry.common.instance import resolve_instance_id


class TestResolveInstanceId:
    """Tests for resolve_instance_id."""

    def test_prefers_explicit_override(self) -> None:
        """AUDIT_INSTANCE_ID wins over everything else."""
        with patch.dict(
            "os.environ",
            {"AUDIT_INSTANCE_ID": "registry-blue-7", "HOSTNAME": "pod-xyz"},
            clear=False,
        ):
            assert resolve_instance_id() == "registry-blue-7"

    def test_falls_back_to_hostname_env(self) -> None:
        """HOSTNAME (Docker/K8s per-container) is used when no override is set."""
        with patch.dict("os.environ", {"HOSTNAME": "pod-xyz"}, clear=False):
            with patch.dict("os.environ", {"AUDIT_INSTANCE_ID": ""}, clear=False):
                assert resolve_instance_id() == "pod-xyz"

    def test_strips_whitespace(self) -> None:
        """Surrounding whitespace on the label is trimmed."""
        with patch.dict(
            "os.environ",
            {"AUDIT_INSTANCE_ID": "  spaced-id  "},
            clear=False,
        ):
            assert resolve_instance_id() == "spaced-id"

    def test_blank_override_is_ignored(self) -> None:
        """A whitespace-only override does not shadow the hostname fallback."""
        with patch.dict(
            "os.environ",
            {"AUDIT_INSTANCE_ID": "   ", "HOSTNAME": "pod-abc"},
            clear=False,
        ):
            assert resolve_instance_id() == "pod-abc"

    def test_falls_back_to_socket_hostname(self) -> None:
        """With no env vars set, socket.gethostname() supplies the label."""
        with patch.dict(
            "os.environ",
            {"AUDIT_INSTANCE_ID": "", "HOSTNAME": ""},
            clear=False,
        ):
            with patch(
                "registry.common.instance.socket.gethostname",
                return_value="bare-metal-host",
            ):
                assert resolve_instance_id() == "bare-metal-host"

    def test_never_empty_when_all_unavailable(self) -> None:
        """When nothing resolves, a stable 'unknown' label is returned (never empty)."""
        with patch.dict(
            "os.environ",
            {"AUDIT_INSTANCE_ID": "", "HOSTNAME": ""},
            clear=False,
        ):
            with patch(
                "registry.common.instance.socket.gethostname",
                return_value="",
            ):
                assert resolve_instance_id() == "unknown"

    def test_never_raises_on_socket_error(self) -> None:
        """A socket failure must not propagate — attribution degrades to 'unknown'."""
        with patch.dict(
            "os.environ",
            {"AUDIT_INSTANCE_ID": "", "HOSTNAME": ""},
            clear=False,
        ):
            with patch(
                "registry.common.instance.socket.gethostname",
                side_effect=OSError("no hostname"),
            ):
                assert resolve_instance_id() == "unknown"

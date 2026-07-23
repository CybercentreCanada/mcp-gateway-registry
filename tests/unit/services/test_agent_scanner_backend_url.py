"""Unit tests for A2A security-scanner backend-URL selection.

In A2A reverse-proxy mode the agent card's advertised ``url`` is the gateway
address, so the scanner must scan the real backend (``proxy_pass_url``) instead.
When ``proxy_pass_url`` is absent (agent registered before the flag was on) the
scanner falls back to ``url`` -- backwards compatible.
"""

import json
from unittest.mock import MagicMock, patch

import pytest


def _make_service():
    """Build an AgentScannerService with its repository dependency mocked out."""
    with patch("registry.services.agent_scanner.get_security_scan_repository"):
        from registry.services.agent_scanner import AgentScannerService

        return AgentScannerService()


def _run_and_capture_scanned_card(agent_card: dict) -> dict:
    """Run _run_a2a_scanner with subprocess mocked; return the card it wrote to disk."""
    service = _make_service()
    captured: dict = {}

    real_open = open

    def _capturing_open(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        return handle

    # Mock subprocess.run to succeed with empty JSON, and capture the temp file
    # the scanner wrote by reading it back inside the mock.
    def _fake_run(cmd, *args, **kwargs):
        # cmd = ["a2a-scanner", "scan-card", <tmp_file_path>, ...]
        tmp_path = cmd[2]
        with real_open(tmp_path) as f:
            captured.update(json.load(f))
        result = MagicMock()
        result.stdout = "{}"
        result.stderr = ""
        result.returncode = 0
        return result

    with patch("registry.services.agent_scanner.subprocess.run", side_effect=_fake_run):
        service._run_a2a_scanner(
            agent_card=agent_card,
            agent_path="/flight-booking-agent",
            analyzers="spec",
        )
    return captured


class TestScannerBackendUrlSelection:
    """The scanned card's url must target the real backend."""

    def test_scans_proxy_pass_url_when_present(self):
        card = {
            "name": "Flight Booking Agent",
            "url": "https://gateway.example.com/agent/flight-booking-agent/",
            "proxy_pass_url": "http://flight-booking-agent:9000/",
        }
        scanned = _run_and_capture_scanned_card(card)
        # The scanner sees the real backend, not the advertised gateway url.
        assert scanned["url"] == "http://flight-booking-agent:9000/"

    def test_falls_back_to_url_when_proxy_pass_url_absent(self):
        card = {
            "name": "Legacy Agent",
            "url": "http://legacy-agent:9000/",
        }
        scanned = _run_and_capture_scanned_card(card)
        assert scanned["url"] == "http://legacy-agent:9000/"

    def test_does_not_mutate_caller_card(self):
        card = {
            "url": "https://gateway.example.com/agent/x/",
            "proxy_pass_url": "http://x:9000/",
        }
        _run_and_capture_scanned_card(card)
        # Original dict is untouched (scanner copies before rewriting).
        assert card["url"] == "https://gateway.example.com/agent/x/"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

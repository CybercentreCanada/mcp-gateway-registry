"""The skill security scanner downloads SKILL.md with the skill's own credentials.

A skill hosted on a private GitHub Enterprise Server (GITHUB_EXTRA_HOSTS) has to be
scanned with the same headers the content endpoint uses, on both scan paths:
registration and the admin rescan. Otherwise the scanner gets a 404/401 for a
private repo and reports a failed scan.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from registry.api.skill_routes import (
    _perform_skill_security_scan_on_registration,
    rescan_skill,
)

GHES_RAW_URL = "https://ghes.example.com/raw/o/r/main/SKILL.md"
GHES_HEADERS = {"Authorization": "Bearer ghes-shared-token"}


def _skill(auth_scheme: str, *, encrypted: str | None = None) -> MagicMock:
    skill = MagicMock()
    skill.path = "/skills/demo"
    skill.skill_md_raw_url = GHES_RAW_URL
    skill.skill_md_url = "https://ghes.example.com/o/r/blob/main/SKILL.md"
    skill.tags = []
    skill.auth_scheme = auth_scheme
    skill.auth_credential_encrypted = encrypted
    skill.auth_header_name = None
    skill.model_dump.return_value = {"path": skill.path}
    return skill


def _scanner() -> MagicMock:
    scanner = MagicMock()
    config = MagicMock(enabled=True, scan_on_registration=True, add_security_pending_tag=False)
    scanner.get_scan_config.return_value = config
    result = MagicMock(is_safe=True, scan_failed=False)
    result.model_dump.return_value = {"is_safe": True}
    scanner.scan_skill = AsyncMock(return_value=result)
    return scanner


@pytest.fixture
def github_auth():
    with patch("registry.services.skill_service._github_auth") as auth:
        auth.get_auth_headers = AsyncMock(return_value=GHES_HEADERS)
        yield auth


async def _register_scan(skill: MagicMock, scanner: MagicMock) -> None:
    with (
        patch("registry.services.skill_scanner.skill_scanner_service", scanner),
        patch("registry.api.skill_routes.fire_scan_complete_event"),
    ):
        await _perform_skill_security_scan_on_registration(skill, AsyncMock())


async def _rescan(skill: MagicMock, scanner: MagicMock) -> None:
    service = AsyncMock()
    service.get_skill.return_value = skill
    with (
        patch("registry.services.skill_scanner.skill_scanner_service", scanner),
        patch("registry.api.skill_routes.get_skill_service", return_value=service),
        patch("registry.api.skill_routes.set_audit_action"),
    ):
        await rescan_skill(
            http_request=MagicMock(),
            user_context={"is_admin": True},
            skill_path="skills/demo",
            _csrf=None,
        )


@pytest.mark.unit
@pytest.mark.parametrize("scan", [_register_scan, _rescan], ids=["registration", "rescan"])
class TestSkillScanFetchAuth:
    async def test_global_credentials_scan_uses_the_shared_github_headers(self, scan, github_auth):
        scanner = _scanner()

        await scan(_skill("global_credentials"), scanner)

        github_auth.get_auth_headers.assert_awaited_once_with(
            GHES_RAW_URL, allow_global_credentials=True
        )
        kwargs = scanner.scan_skill.await_args.kwargs
        assert kwargs["skill_md_url"] == GHES_RAW_URL
        assert kwargs["headers"] == GHES_HEADERS

    async def test_bearer_scan_uses_only_the_skill_credential(self, scan, github_auth):
        scanner = _scanner()
        skill = _skill("bearer", encrypted="enc")

        with patch(
            "registry.utils.credential_encryption.decrypt_credential", return_value="skill-pat"
        ):
            await scan(skill, scanner)

        github_auth.get_auth_headers.assert_not_awaited()
        assert scanner.scan_skill.await_args.kwargs["headers"] == {
            "Authorization": "Bearer skill-pat"
        }

    async def test_none_scheme_scan_sends_no_credentials(self, scan, github_auth):
        scanner = _scanner()

        await scan(_skill("none"), scanner)

        github_auth.get_auth_headers.assert_not_awaited()
        assert scanner.scan_skill.await_args.kwargs["headers"] is None

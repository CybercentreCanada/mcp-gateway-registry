"""Unit tests for skill-scoped visibility filtering on the list endpoint.

These tests guard against a permission-confusion bug where the ``GET
/api/skills`` list endpoint decided whether to bypass per-skill visibility
filtering based on an AGENT-scoped grant (``"all" in accessible_agents``). A
non-admin holding broad agent access could then see private/group skills they
had no skill-level access to.

The correct behaviour: only an admin bypasses per-skill visibility filtering
(the DB-level fast path). Every non-admin -- regardless of agent/server grants
-- must go through the filtered fallback path (``list_skills_for_user``), which
applies public / private-owner / group rules. Anonymous callers see only public
skills. This is verified by asserting WHICH service method the endpoint invokes
and WHICH skills come back.
"""

import logging
import uuid
from typing import Any
from unittest.mock import (
    AsyncMock,
    MagicMock,
    patch,
)

import pytest
from fastapi.testclient import TestClient

from registry.schemas.skill_models import SkillInfo, VisibilityEnum

logger = logging.getLogger(__name__)


# =============================================================================
# HELPERS
# =============================================================================


def _make_skill_info(
    path: str,
    name: str,
    visibility: VisibilityEnum,
    owner: str = "someone-else",
    allowed_groups: list[str] | None = None,
) -> SkillInfo:
    """Build a minimal SkillInfo for list-endpoint assertions."""
    return SkillInfo(
        id=str(uuid.uuid4()),
        path=path,
        name=name,
        description="test skill",
        skill_md_url="https://example.com/SKILL.md",
        skill_md_raw_url=None,
        repository_url=None,
        tags=[],
        author=None,
        version=None,
        metadata=None,
        compatibility=None,
        target_agents=[],
        is_enabled=True,
        visibility=visibility,
        allowed_groups=allowed_groups or [],
        registry_name="local",
        owner=owner,
        auth_scheme="none",
        auth_header_name=None,
        num_stars=0,
        rating_details=[],
        health_status="unknown",
        last_checked_time=None,
        status="active",
    )


def _admin_context() -> dict[str, Any]:
    return {
        "username": "admin",
        "is_admin": True,
        "groups": ["mcp-registry-admin"],
        "scopes": [],
        "accessible_servers": ["all"],
        "accessible_services": ["all"],
        "accessible_agents": ["all"],
        "auth_method": "session",
    }


def _non_admin_all_agents_context() -> dict[str, Any]:
    """Non-admin whose ONLY broad grant is agent-scoped.

    This is the exploit context: ``"all" in accessible_agents`` previously
    unlocked the unfiltered fast path even though the user is not an admin.
    """
    return {
        "username": "bob",
        "is_admin": False,
        "groups": [],
        "scopes": [],
        "accessible_servers": [],
        "accessible_services": [],
        "accessible_agents": ["all"],
        "auth_method": "session",
    }


def _make_service_with_filtering(
    all_skills: list[SkillInfo],
) -> MagicMock:
    """Build a mock skill service whose fallback path applies real filtering.

    ``list_skills_for_user`` mirrors the production semantics: admins see
    everything, non-admins see public + owned-private + matching-group skills.
    ``get_skills_paginated`` (fast path) is a separate mock so we can assert it
    is only reached by admins.
    """

    async def _list_for_user(
        user_context: dict[str, Any] | None,
        include_disabled: bool = False,
        tag: str | None = None,
    ) -> list[SkillInfo]:
        if not user_context:
            return [s for s in all_skills if s.visibility == VisibilityEnum.PUBLIC]
        if user_context.get("is_admin"):
            return list(all_skills)
        username = user_context.get("username", "")
        groups = set(user_context.get("groups", []))
        visible = []
        for s in all_skills:
            if s.visibility == VisibilityEnum.PUBLIC:
                visible.append(s)
            elif s.visibility == VisibilityEnum.PRIVATE and s.owner == username:
                visible.append(s)
            elif s.visibility == VisibilityEnum.GROUP and (groups & set(s.allowed_groups)):
                visible.append(s)
        return visible

    service = MagicMock()
    service.list_skills_for_user = AsyncMock(side_effect=_list_for_user)
    # Fast path returns SkillCard-like objects; only admins should reach it.
    service.get_skills_paginated = AsyncMock(return_value=(list(all_skills), len(all_skills)))
    return service


def _client(
    service: MagicMock,
    user_context: dict[str, Any],
):
    """Yield a TestClient wired to the mock service and auth context."""
    from registry.auth.dependencies import nginx_proxied_auth
    from registry.main import app

    app.dependency_overrides[nginx_proxied_auth] = lambda: user_context

    scanner = MagicMock()
    scanner.get_scan_summaries = AsyncMock(return_value={})

    with (
        patch(
            "registry.api.skill_routes.get_skill_service",
            return_value=service,
        ),
        patch("registry.services.skill_scanner.skill_scanner_service", scanner),
        patch("registry.health.service.health_service", MagicMock()),
        patch("registry.core.nginx_service.nginx_service", MagicMock()),
    ):
        client = TestClient(app, cookies={"mcp_gateway_session": "test-session"})
        yield client

    app.dependency_overrides.clear()


# =============================================================================
# FIXTURES
# =============================================================================


@pytest.fixture
def sample_skills() -> list[SkillInfo]:
    """One public, one private (owned by alice), one group-restricted skill."""
    return [
        _make_skill_info("/skills/public-one", "public-one", VisibilityEnum.PUBLIC),
        _make_skill_info(
            "/skills/private-alice",
            "private-alice",
            VisibilityEnum.PRIVATE,
            owner="alice",
        ),
        _make_skill_info(
            "/skills/group-secret",
            "group-secret",
            VisibilityEnum.GROUP,
            owner="alice",
            allowed_groups=["secret-group"],
        ),
    ]


# =============================================================================
# TESTS
# =============================================================================


@pytest.mark.unit
class TestSkillListVisibility:
    """The list endpoint must filter by SKILL access, never agent access."""

    def test_non_admin_with_all_agents_cannot_see_restricted_skills(
        self,
        mock_settings,
        sample_skills,
    ):
        """A non-admin with ``accessible_agents=['all']`` sees only public skills.

        This is the regression guard: before the fix, the endpoint took the
        unfiltered DB fast path for this user and leaked the private + group
        skills.
        """
        service = _make_service_with_filtering(sample_skills)
        context = _non_admin_all_agents_context()

        gen = _client(service, context)
        client = next(gen)
        try:
            response = client.get("/api/skills")
        finally:
            gen.close()

        assert response.status_code == 200
        data = response.json()
        returned_paths = {s["path"] for s in data["skills"]}

        # Only the public skill is visible; restricted skills are omitted.
        assert returned_paths == {"/skills/public-one"}
        assert "/skills/private-alice" not in returned_paths
        assert "/skills/group-secret" not in returned_paths

        # The filtered fallback path was used, NOT the unfiltered fast path.
        service.list_skills_for_user.assert_awaited()
        service.get_skills_paginated.assert_not_awaited()

    def test_non_admin_all_agents_uses_filtered_path_when_no_field_filters(
        self,
        mock_settings,
        sample_skills,
    ):
        """Even with include_disabled=True and no tag, a non-admin is filtered.

        ``include_disabled=True`` with no tag removes field filters, which was
        exactly the condition the old fast-path bypass triggered on. A non-admin
        must still be routed through per-skill filtering.
        """
        service = _make_service_with_filtering(sample_skills)
        context = _non_admin_all_agents_context()

        gen = _client(service, context)
        client = next(gen)
        try:
            response = client.get("/api/skills?include_disabled=true")
        finally:
            gen.close()

        assert response.status_code == 200
        data = response.json()
        returned_paths = {s["path"] for s in data["skills"]}
        assert returned_paths == {"/skills/public-one"}
        service.list_skills_for_user.assert_awaited()
        service.get_skills_paginated.assert_not_awaited()

    def test_owner_sees_own_private_skill(
        self,
        mock_settings,
        sample_skills,
    ):
        """A user WITH skill-level access still sees the restricted skill."""
        service = _make_service_with_filtering(sample_skills)
        context = _non_admin_all_agents_context()
        context["username"] = "alice"
        context["groups"] = ["secret-group"]

        gen = _client(service, context)
        client = next(gen)
        try:
            response = client.get("/api/skills")
        finally:
            gen.close()

        assert response.status_code == 200
        data = response.json()
        returned_paths = {s["path"] for s in data["skills"]}
        # Alice owns the private skill and is in the group -> sees all three.
        assert returned_paths == {
            "/skills/public-one",
            "/skills/private-alice",
            "/skills/group-secret",
        }
        service.list_skills_for_user.assert_awaited()
        service.get_skills_paginated.assert_not_awaited()

    def test_admin_sees_all_skills_via_fast_path(
        self,
        mock_settings,
        sample_skills,
    ):
        """An admin bypasses filtering and takes the unfiltered fast path."""
        service = _make_service_with_filtering(sample_skills)
        context = _admin_context()

        gen = _client(service, context)
        client = next(gen)
        try:
            # include_disabled=True + no tag removes field filters so the admin
            # exercises the DB-level fast path.
            response = client.get("/api/skills?include_disabled=true")
        finally:
            gen.close()

        assert response.status_code == 200
        data = response.json()
        returned_paths = {s["path"] for s in data["skills"]}
        assert returned_paths == {
            "/skills/public-one",
            "/skills/private-alice",
            "/skills/group-secret",
        }
        # Admin took the fast path.
        service.get_skills_paginated.assert_awaited()
        service.list_skills_for_user.assert_not_awaited()

    def test_public_skills_still_public_for_non_admin(
        self,
        mock_settings,
        sample_skills,
    ):
        """Public skills remain visible to a plain non-admin with no grants."""
        service = _make_service_with_filtering(sample_skills)
        context = _non_admin_all_agents_context()
        context["accessible_agents"] = []

        gen = _client(service, context)
        client = next(gen)
        try:
            response = client.get("/api/skills")
        finally:
            gen.close()

        assert response.status_code == 200
        data = response.json()
        returned_paths = {s["path"] for s in data["skills"]}
        assert returned_paths == {"/skills/public-one"}
        service.get_skills_paginated.assert_not_awaited()

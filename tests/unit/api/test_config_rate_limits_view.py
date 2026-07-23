"""Unit tests for the read-only rate-limit definitions group in System Config (#295).

The System Config page shows a live, read-only view of the current rate-limit
definitions. These verify the group builder produces the ConfigField/ConfigGroup
shape the frontend already renders, and that it fails soft.
"""

from unittest.mock import AsyncMock, patch

import pytest

from registry.rate_limiting.models import RateLimitDefinition


async def _build_group(definitions):
    """Invoke the group builder with a patched repository returning ``definitions``."""
    from registry.api import config_routes

    with (
        patch(
            "registry.rate_limiting.definitions_repository.DefinitionsRepository.list_all",
            new=AsyncMock(return_value=definitions),
        ),
        patch(
            "registry.rate_limiting.definitions_repository.DefinitionsRepository._get_collection",
            new=AsyncMock(),
        ),
    ):
        return await config_routes._build_rate_limit_definitions_group()


@pytest.mark.unit
class TestRateLimitDefinitionsGroup:
    """Tests for _build_rate_limit_definitions_group."""

    async def test_group_lists_definitions_in_config_field_shape(self):
        """Each definition becomes one read-only ConfigField (key/label/value)."""
        defs = [
            RateLimitDefinition(
                axis="caller",
                entity_type="group",
                name="developers",
                user_max_requests=25,
                agent_max_requests=15,
                window_seconds=60,
            ),
            RateLimitDefinition(
                axis="target",
                entity_type="mcp_server",
                name="mcpgw",
                max_requests=500,
                window_seconds=60,
                enabled=False,
            ),
        ]
        group = await _build_group(defs)

        assert group["id"] == "rate_limit_definitions"
        assert "read-only" in group["title"].lower()
        assert len(group["fields"]) == 2

        by_key = {f["key"]: f for f in group["fields"]}
        developers = by_key["caller:group:developers:60"]
        assert developers["label"] == "caller:group:developers:60"
        assert "user 25" in developers["value"]
        assert "agent 15" in developers["value"]
        assert "enabled" in developers["value"]
        # Read-only: not copyable, not masked.
        assert developers["raw_value"] is None
        assert developers["is_masked"] is False

        mcpgw = by_key["target:mcp_server:mcpgw:60"]
        assert "disabled" in mcpgw["value"]

    async def test_fail_closed_definition_annotated(self):
        """A fail_closed definition is annotated in its summary."""
        defs = [
            RateLimitDefinition(
                axis="target",
                entity_type="a2a_agent",
                name="critical",
                max_requests=10,
                window_seconds=60,
                fail_closed=True,
            )
        ]
        group = await _build_group(defs)
        assert "fail-closed" in group["fields"][0]["value"]

    async def test_empty_when_no_definitions(self):
        """With no definitions the group renders with an empty fields list."""
        group = await _build_group([])
        assert group["fields"] == []

    async def test_fails_soft_on_repository_error(self):
        """A repository error returns None so the config view still renders."""
        from registry.api import config_routes

        with (
            patch(
                "registry.rate_limiting.definitions_repository.DefinitionsRepository.list_all",
                new=AsyncMock(side_effect=RuntimeError("db down")),
            ),
            patch(
                "registry.rate_limiting.definitions_repository.DefinitionsRepository._get_collection",
                new=AsyncMock(),
            ),
        ):
            group = await config_routes._build_rate_limit_definitions_group()
        assert group is None

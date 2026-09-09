"""Unit tests for scripts/backfill-custom-entity-scopes.py.

The backfill grants each existing custom type's scope set to mcp-registry-admin
and triggers a reload. It must be idempotent (re-running mints the same scopes)
and a dry run must not mint. The module is loaded by path because the filename
uses hyphens (not importable as a package).
"""

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "backfill-custom-entity-scopes.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("backfill_custom_entity_scopes", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _descriptor(name: str) -> MagicMock:
    d = MagicMock()
    d.name = name
    return d


@pytest.mark.unit
class TestBackfill:
    @pytest.mark.asyncio
    async def test_apply_mints_every_type(self):
        module = _load_module()
        service = MagicMock()
        service.list_types = AsyncMock(
            return_value=[_descriptor("dataset"), _descriptor("workflow")]
        )
        mint = AsyncMock(return_value=True)
        reload = AsyncMock(return_value=True)

        with (
            patch("registry.repositories.factory.get_custom_entity_service", return_value=service),
            patch("registry.services.scope_service.mint_custom_type_scopes", mint),
            patch("registry.services.scope_service.trigger_auth_server_reload", reload),
        ):
            result = await module._run_backfill(dry_run=False)

        assert result == {"types_found": 2, "types_minted": 2}
        assert {c.args[0] for c in mint.await_args_list} == {"dataset", "workflow"}
        reload.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dry_run_does_not_mint(self):
        module = _load_module()
        service = MagicMock()
        service.list_types = AsyncMock(return_value=[_descriptor("dataset")])
        mint = AsyncMock(return_value=True)

        with (
            patch("registry.repositories.factory.get_custom_entity_service", return_value=service),
            patch("registry.services.scope_service.mint_custom_type_scopes", mint),
        ):
            result = await module._run_backfill(dry_run=True)

        assert result == {"types_found": 1, "types_minted": 0}
        mint.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_idempotent_rerun_mints_again(self):
        # Re-running is safe: mint is an idempotent $set merge, so a second run
        # over the same type set mints the same scopes without error.
        module = _load_module()
        service = MagicMock()
        service.list_types = AsyncMock(return_value=[_descriptor("dataset")])
        mint = AsyncMock(return_value=True)
        reload = AsyncMock(return_value=True)

        with (
            patch("registry.repositories.factory.get_custom_entity_service", return_value=service),
            patch("registry.services.scope_service.mint_custom_type_scopes", mint),
            patch("registry.services.scope_service.trigger_auth_server_reload", reload),
        ):
            first = await module._run_backfill(dry_run=False)
            second = await module._run_backfill(dry_run=False)

        assert first == second == {"types_found": 1, "types_minted": 1}
        assert mint.await_count == 2

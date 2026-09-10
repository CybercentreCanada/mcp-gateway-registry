"""Unit tests for per-type custom-entity scope naming and provisioning.

Covers the naming single-source-of-truth (custom_entity_scopes), the
admin-derivation exclusion predicate (privileged_constants), and the mint /
cleanup service helpers (scope_service) including the fatal-on-failure contract.
"""

from unittest.mock import AsyncMock, patch

import pytest

from registry.auth.privileged_constants import is_admin_conferring_action
from registry.services import scope_service
from registry.services.custom_entity_scopes import (
    all_entity_scopes,
    entity_scope,
    list_grant_allows_type,
    list_grant_record_paths,
)
from registry.services.scope_service import (
    ADMIN_GROUP_NAME,
    ScopeMintError,
    cleanup_custom_type_scopes,
    mint_custom_type_scopes,
)


@pytest.mark.unit
class TestNaming:
    def test_entity_scope_format(self):
        assert entity_scope("create", "dataset") == "create_dataset_entity"
        assert entity_scope("list", "n8n_workflow") == "list_n8n_workflow_entity"

    def test_all_entity_scopes_full_set(self):
        scopes = all_entity_scopes("dataset")
        assert scopes == {
            "list_dataset_entity": ["all"],
            "create_dataset_entity": ["all"],
            "modify_dataset_entity": ["all"],
            "delete_dataset_entity": ["all"],
        }


@pytest.mark.unit
class TestListGrantTiers:
    """The list_<type>_entity grant is interpreted in three tiers."""

    def test_all_opens_whole_type(self):
        assert list_grant_allows_type("n8n", ["all"]) is True

    def test_bare_type_name_opens_whole_type(self):
        # Backward-compatible with the original per-type semantics.
        assert list_grant_allows_type("n8n", ["n8n"]) is True

    def test_record_path_does_not_open_whole_type(self):
        # A record-scoped grant is NOT whole-type: it must not surface every
        # (public) record — only the named record via list_grant_record_paths.
        grant = ["/n8n/6aac5d9c-c002-4761-b614-58c21c4adb9a"]
        assert list_grant_allows_type("n8n", grant) is False

    def test_empty_grant_opens_nothing(self):
        assert list_grant_allows_type("n8n", []) is False

    def test_record_paths_extracted_for_type_only(self):
        grant = [
            "/n8n/1111aaaa-c002-4761-b614-58c21c4adb9a",
            "/n8n/2222bbbb-c002-4761-b614-58c21c4adb9a",
            "/policy/3333cccc-c002-4761-b614-58c21c4adb9a",  # other type
            "all",  # whole-type token, not a path
        ]
        paths = list_grant_record_paths("n8n", grant)
        assert paths == [
            "/n8n/1111aaaa-c002-4761-b614-58c21c4adb9a",
            "/n8n/2222bbbb-c002-4761-b614-58c21c4adb9a",
        ]

    def test_record_paths_empty_for_whole_type_grant(self):
        assert list_grant_record_paths("n8n", ["all"]) == []
        assert list_grant_record_paths("n8n", ["n8n"]) == []


@pytest.mark.unit
class TestAdminConferringExclusion:
    """The boundary predicate: entity mutation scopes are non-conferring."""

    def test_real_admin_actions_confer(self):
        assert is_admin_conferring_action("register_service") is True
        assert is_admin_conferring_action("create_virtual_server") is True
        assert is_admin_conferring_action("delete_agent") is True

    def test_read_only_never_confers(self):
        assert is_admin_conferring_action("list_service") is False
        assert is_admin_conferring_action("get_agent") is False
        assert is_admin_conferring_action("list_dataset_entity") is False

    def test_per_type_mutation_scopes_excluded(self):
        assert is_admin_conferring_action("create_dataset_entity") is False
        assert is_admin_conferring_action("modify_dataset_entity") is False
        assert is_admin_conferring_action("delete_dataset_entity") is False

    def test_every_minted_scope_is_non_conferring(self):
        # No scope the feature mints may ever confer admin.
        for action in all_entity_scopes("dataset"):
            assert is_admin_conferring_action(action) is False


@pytest.mark.unit
class TestMintAndCleanup:
    @pytest.mark.asyncio
    async def test_mint_persists_full_scope_set(self):
        repo = AsyncMock()
        repo.merge_ui_permissions = AsyncMock(return_value=True)
        with patch.object(scope_service, "get_scope_repository", return_value=repo):
            result = await mint_custom_type_scopes("dataset")
        assert result is True
        repo.merge_ui_permissions.assert_awaited_once_with(
            ADMIN_GROUP_NAME, all_entity_scopes("dataset")
        )

    @pytest.mark.asyncio
    async def test_mint_raises_when_group_missing(self):
        repo = AsyncMock()
        repo.merge_ui_permissions = AsyncMock(return_value=False)
        with patch.object(scope_service, "get_scope_repository", return_value=repo):
            with pytest.raises(ScopeMintError):
                await mint_custom_type_scopes("dataset")

    @pytest.mark.asyncio
    async def test_mint_raises_when_repo_errors(self):
        repo = AsyncMock()
        repo.merge_ui_permissions = AsyncMock(side_effect=RuntimeError("db down"))
        with patch.object(scope_service, "get_scope_repository", return_value=repo):
            with pytest.raises(ScopeMintError):
                await mint_custom_type_scopes("dataset")

    @pytest.mark.asyncio
    async def test_cleanup_sweeps_all_groups(self):
        repo = AsyncMock()
        repo.remove_ui_permission_keys_from_all_groups = AsyncMock(return_value=2)
        with patch.object(scope_service, "get_scope_repository", return_value=repo):
            modified = await cleanup_custom_type_scopes("dataset")
        assert modified == 2
        repo.remove_ui_permission_keys_from_all_groups.assert_awaited_once_with(
            list(all_entity_scopes("dataset").keys())
        )

    @pytest.mark.asyncio
    async def test_cleanup_non_fatal_on_error(self):
        repo = AsyncMock()
        repo.remove_ui_permission_keys_from_all_groups = AsyncMock(
            side_effect=RuntimeError("db down")
        )
        with patch.object(scope_service, "get_scope_repository", return_value=repo):
            modified = await cleanup_custom_type_scopes("dataset")
        assert modified == 0  # swallowed, not raised

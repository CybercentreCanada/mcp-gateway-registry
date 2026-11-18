"""A "*" grant is "my own resources", never "everything" and never admin.

The Trinity developer scopes grant modify_*/toggle_* with "*". An owner passed by
the call site makes the grant apply to resources the caller registered; without it
the grant does nothing.
"""

import pytest

from registry.auth.asset_permissions import user_has_asset_permission
from registry.auth.dependencies import _user_is_admin

DEV_PERMISSIONS = {
    "publish_agent": ["*"],
    "list_agents": ["all"],
    "get_agent": ["all"],
    "modify_service": ["*"],
    "modify_agent": ["*"],
    "publish_skill": ["*"],
    "health_check_service": ["all"],
    "toggle_service": ["*"],
    "toggle_agent": ["*"],
}


def _dev(**over):
    return {"username": "dev1", "is_admin": False, "ui_permissions": DEV_PERMISSIONS, **over}


@pytest.mark.unit
class TestOwnerWildcard:
    @pytest.mark.parametrize(
        ("family", "action"),
        [("server", "modify"), ("server", "toggle"), ("agent", "modify"), ("agent", "toggle")],
    )
    def test_owner_may_act_on_own_resource(self, family, action):
        assert user_has_asset_permission(family, action, "mine", _dev(), owner="dev1")

    @pytest.mark.parametrize(
        ("family", "action"),
        [("server", "modify"), ("server", "toggle"), ("agent", "modify"), ("agent", "toggle")],
    )
    def test_other_users_resource_is_denied(self, family, action):
        assert not user_has_asset_permission(family, action, "theirs", _dev(), owner="someone")

    def test_without_an_owner_the_wildcard_grants_nothing(self):
        assert not user_has_asset_permission("agent", "toggle", "mine", _dev())

    def test_unowned_resource_is_denied(self):
        assert not user_has_asset_permission("server", "modify", "legacy", _dev(), owner=None)

    def test_anonymous_never_matches_an_empty_owner(self):
        user = _dev(username="")
        assert not user_has_asset_permission("server", "modify", "x", user, owner="")

    @pytest.mark.parametrize("family", ["agent", "skill"])
    def test_create_needs_no_owner(self, family):
        assert user_has_asset_permission(family, "create", "new-thing", _dev())

    def test_wildcard_does_not_widen_read_actions(self):
        user = _dev(ui_permissions={"list_agents": ["*"], "health_check_service": ["*"]})
        assert not user_has_asset_permission("agent", "list", "a", user, owner="dev1")
        assert not user_has_asset_permission("server", "health_check", "s", user, owner="dev1")

    def test_wildcard_does_not_confer_admin(self):
        assert _user_is_admin(DEV_PERMISSIONS) is False

    def test_all_and_exact_grants_still_work_without_an_owner(self):
        user = _dev(ui_permissions={"toggle_agent": ["all"], "modify_agent": ["named"]})
        assert user_has_asset_permission("agent", "toggle", "any", user)
        assert user_has_asset_permission("agent", "modify", "named", user)
        assert not user_has_asset_permission("agent", "modify", "other", user)

    def test_admin_bypasses(self):
        assert user_has_asset_permission("agent", "toggle", "x", {"is_admin": True})

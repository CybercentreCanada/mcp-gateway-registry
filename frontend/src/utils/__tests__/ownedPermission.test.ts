import { hasOwnedPermission } from '../ownedPermission';

const developer = {
  username: 'dev1',
  is_admin: false,
  ui_permissions: { toggle_agent: ['*'], modify_agent: ['*'], list_agents: ['all'] },
};

describe('hasOwnedPermission', () => {
  it('lets a "*" grant act on the user\'s own resources', () => {
    expect(hasOwnedPermission(developer, 'toggle_agent', '/my-agent', 'dev1')).toBe(true);
  });

  it('does not let a "*" grant reach another owner\'s resource', () => {
    expect(hasOwnedPermission(developer, 'toggle_agent', '/their-agent', 'someone')).toBe(false);
  });

  it('does not let a "*" grant reach a resource without an owner', () => {
    expect(hasOwnedPermission(developer, 'toggle_agent', '/orphan', null)).toBe(false);
    expect(hasOwnedPermission(developer, 'toggle_agent', '/orphan', undefined)).toBe(false);
  });

  it('never matches an empty username against a missing owner', () => {
    const anon = { ...developer, username: '' };
    expect(hasOwnedPermission(anon, 'toggle_agent', '/x', '')).toBe(false);
  });

  it('honors "all" and an exact resource name regardless of owner', () => {
    const user = { username: 'u', ui_permissions: { toggle_agent: ['all'], modify_agent: ['named'] } };
    expect(hasOwnedPermission(user, 'toggle_agent', '/any', 'x')).toBe(true);
    expect(hasOwnedPermission(user, 'modify_agent', '/named', 'x')).toBe(true);
    expect(hasOwnedPermission(user, 'modify_agent', '/other', 'x')).toBe(false);
  });

  it('is false without the permission, and true for admins', () => {
    expect(hasOwnedPermission(developer, 'delete_agent', '/my-agent', 'dev1')).toBe(false);
    expect(hasOwnedPermission({ is_admin: true }, 'delete_agent', '/x', null)).toBe(true);
    expect(hasOwnedPermission(null, 'toggle_agent', '/x', 'dev1')).toBe(false);
  });
});

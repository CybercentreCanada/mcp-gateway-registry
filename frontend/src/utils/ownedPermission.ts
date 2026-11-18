/**
 * Whether the user's scope grants an action on a resource, for button visibility.
 * Mirrors registry/auth/asset_permissions.py (the backend decides for real):
 * "all" or the resource's own name grants it; "*" grants it only on resources
 * the user registered. "*" never applies to a resource without an owner.
 */
interface PermissionUser {
  username?: string;
  is_admin?: boolean;
  ui_permissions?: Record<string, string[] | undefined>;
}

export function hasOwnedPermission(
  user: PermissionUser | null | undefined,
  permission: string,
  resourcePath: string,
  owner: string | null | undefined,
): boolean {
  if (!user) return false;
  if (user.is_admin) return true;
  const granted = user.ui_permissions?.[permission];
  if (!granted) return false;
  const name = resourcePath.replace(/^\//, '');
  if (granted.includes('all') || granted.includes(name)) return true;
  return granted.includes('*') && !!user.username && owner === user.username;
}

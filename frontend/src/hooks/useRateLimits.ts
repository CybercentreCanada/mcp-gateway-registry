import axios from 'axios';
import { useIAMResource } from './useIAMResource';

// ─── Types ──────────────────────────────────────────────────────

// A rate-limit definition. Caller (group) definitions carry per-caller-type
// limits (user_max_requests / agent_max_requests); target definitions carry a
// single max_requests. Mirrors registry/rate_limiting/models.py.
export interface RateLimitDefinition {
  axis: 'caller' | 'target';
  entity_type: string;
  name: string;
  max_requests?: number | null;
  user_max_requests?: number | null;
  agent_max_requests?: number | null;
  window_seconds: number;
  fail_closed: boolean;
  enabled: boolean;
}

// A rate-limit membership: maps a user/client to rate-limit group name(s).
export interface RateLimitMembership {
  subject_type: 'user' | 'client';
  subject: string;
  groups: string[];
}

export const CALLER_ENTITY_TYPE = 'group';
export const TARGET_ENTITY_TYPES = ['mcp_server', 'a2a_agent'];

// Build the server-derived definition id: '<axis>:<entity_type>:<name>:<window_seconds>'.
export function definitionId(d: RateLimitDefinition): string {
  return `${d.axis}:${d.entity_type}:${d.name}:${d.window_seconds}`;
}

export function membershipId(m: RateLimitMembership): string {
  return `${m.subject_type}:${m.subject}`;
}

// ─── Definitions ────────────────────────────────────────────────

export function useRateLimitDefinitions() {
  const { data, isLoading, error, refetch } = useIAMResource<RateLimitDefinition>(
    async () => {
      const res = await axios.get('/api/rate-limits');
      return res.data.definitions || [];
    },
    'Failed to load rate-limit definitions',
  );
  return { definitions: data, isLoading, error, refetch };
}

export async function setRateLimitDefinition(d: RateLimitDefinition): Promise<RateLimitDefinition> {
  const res = await axios.put(
    `/api/rate-limits/${encodeURIComponent(definitionId(d))}`,
    d,
  );
  return res.data;
}

export async function deleteRateLimitDefinition(id: string): Promise<void> {
  await axios.delete(`/api/rate-limits/${encodeURIComponent(id)}`);
}

export async function setRateLimitEnabled(id: string, enabled: boolean): Promise<RateLimitDefinition> {
  const res = await axios.post(
    `/api/rate-limits-enabled/${encodeURIComponent(id)}`,
    null,
    { params: { enabled } },
  );
  return res.data;
}

// ─── Memberships ────────────────────────────────────────────────

export function useRateLimitMemberships() {
  const { data, isLoading, error, refetch } = useIAMResource<RateLimitMembership>(
    async () => {
      const res = await axios.get('/api/rate-limit-memberships');
      return res.data.memberships || [];
    },
    'Failed to load rate-limit memberships',
  );
  return { memberships: data, isLoading, error, refetch };
}

// Set (create/replace) a caller's rate-limit group membership. An empty groups
// list is stored as a membership with no groups; callers should delete instead
// when they want to remove the record entirely.
export async function setRateLimitMembership(
  subjectType: 'user' | 'client',
  subject: string,
  groups: string[],
): Promise<RateLimitMembership> {
  const id = `${subjectType}:${subject}`;
  const res = await axios.put(
    `/api/rate-limit-memberships/${encodeURIComponent(id)}`,
    { subject_type: subjectType, subject, groups },
  );
  return res.data;
}

export async function deleteRateLimitMembership(id: string): Promise<void> {
  await axios.delete(`/api/rate-limit-memberships/${encodeURIComponent(id)}`);
}

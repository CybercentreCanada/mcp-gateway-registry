/**
 * Hook for fetching the list of agents with descriptions.
 *
 * Provides agent names and descriptions for scope configuration
 * in IAM Groups form using searchable select components.
 */

import { useState, useEffect, useCallback } from 'react';
import { fetchAllPages } from '../utils/fetchAllPages';


export interface AgentInfo {
  name: string;
  path: string;
  description: string;
}

interface UseAgentListReturn {
  agents: AgentInfo[];
  isLoading: boolean;
  error: string | null;
  refetch: () => Promise<void>;
}


export function useAgentList(): UseAgentListReturn {
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchAgents = useCallback(async () => {
    setIsLoading(true);
    setError(null);

    try {
      // Issue #880: default API limit is 20; page through all agents.
      const rawAgents = await fetchAllPages<{
        name: string;
        path: string;
        description?: string;
      }>({
        url: '/api/agents',
        itemsKey: 'agents',
      });

      const agentList: AgentInfo[] = rawAgents.map((agent) => ({
        name: agent.name,
        path: agent.path,
        description: agent.description || '',
      }));

      // Sort by name
      agentList.sort((a, b) => a.name.localeCompare(b.name));

      setAgents(agentList);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to fetch agents';
      setError(message);
      setAgents([]);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchAgents();
  }, [fetchAgents]);

  return {
    agents,
    isLoading,
    error,
    refetch: fetchAgents,
  };
}

/**
 * Multi-page collection fetcher for registry list APIs.
 *
 * Backend endpoints (/api/servers, /api/agents, /api/skills, /api/custom/{type})
 * enforce a max page size (typically 2000 or 1000). Issue #880: the UI must not
 * hard-cap visible items to a single page — loop with limit/offset (or skip)
 * until all rows are collected using total_count when present.
 */

import axios, { AxiosRequestConfig } from 'axios';

/** API max for servers, agents, and skills list endpoints. */
export const REGISTRY_LIST_PAGE_SIZE = 2000;

/** API max for custom entity list endpoints. */
export const CUSTOM_ENTITY_PAGE_SIZE = 1000;

/** Safety cap: max pages to fetch (prevents infinite loops on bad total_count). */
export const DEFAULT_MAX_PAGES = 50;

export interface FetchAllPagesOptions {
  /** Absolute path, e.g. `/api/servers`. */
  url: string;
  /** Response array property name, e.g. `servers`, `agents`, `skills`, `records`. */
  itemsKey: string;
  /** Page size; must be within the endpoint's allowed range. */
  pageSize?: number;
  /** Extra query params (must not include limit/offset/skip). */
  params?: Record<string, string | number | boolean | undefined | null>;
  /** Offset query parameter name (`offset` for most routes, `skip` for custom entities). */
  offsetParam?: 'offset' | 'skip';
  /** Abort after this many pages. */
  maxPages?: number;
  /** Optional axios config (headers, signal, etc.). */
  axiosConfig?: AxiosRequestConfig;
}

/**
 * Fetch every page of a paginated list endpoint and return the concatenated items.
 */
export async function fetchAllPages<T = unknown>(
  options: FetchAllPagesOptions,
): Promise<T[]> {
  const {
    url,
    itemsKey,
    pageSize = REGISTRY_LIST_PAGE_SIZE,
    params = {},
    offsetParam = 'offset',
    maxPages = DEFAULT_MAX_PAGES,
    axiosConfig,
  } = options;

  if (pageSize < 1) {
    throw new Error('pageSize must be >= 1');
  }

  const all: T[] = [];
  let offset = 0;

  for (let page = 0; page < maxPages; page += 1) {
    const query: Record<string, string | number | boolean> = {
      limit: pageSize,
      [offsetParam]: offset,
    };
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null) {
        query[key] = value;
      }
    }

    const response = await axios.get(url, {
      ...axiosConfig,
      params: {
        ...(axiosConfig?.params as Record<string, unknown> | undefined),
        ...query,
      },
    });

    const data = response.data || {};
    const items = (Array.isArray(data[itemsKey]) ? data[itemsKey] : []) as T[];
    const totalCount =
      typeof data.total_count === 'number' && Number.isFinite(data.total_count)
        ? data.total_count
        : null;

    all.push(...items);

    // Empty page or short page => no more data
    if (items.length === 0 || items.length < pageSize) {
      break;
    }
    // total_count says we have everything
    if (totalCount !== null && all.length >= totalCount) {
      break;
    }

    offset += pageSize;
  }

  return all;
}

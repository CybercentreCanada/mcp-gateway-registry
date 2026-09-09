import axios from 'axios';
import {
  CUSTOM_ENTITY_PAGE_SIZE,
  DEFAULT_MAX_PAGES,
  fetchAllPages,
  REGISTRY_LIST_PAGE_SIZE,
} from '../fetchAllPages';

jest.mock('axios');

const mockedAxios = axios as jest.Mocked<typeof axios>;

describe('fetchAllPages', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  test('returns a single page when total_count fits in one page', async () => {
    mockedAxios.get.mockResolvedValueOnce({
      data: {
        servers: [{ path: '/a' }, { path: '/b' }],
        total_count: 2,
        limit: 2000,
        offset: 0,
      },
    });

    const items = await fetchAllPages<{ path: string }>({
      url: '/api/servers',
      itemsKey: 'servers',
      params: { include_tools: false },
    });

    expect(items).toEqual([{ path: '/a' }, { path: '/b' }]);
    expect(mockedAxios.get).toHaveBeenCalledTimes(1);
    expect(mockedAxios.get).toHaveBeenCalledWith('/api/servers', {
      params: {
        limit: REGISTRY_LIST_PAGE_SIZE,
        offset: 0,
        include_tools: false,
      },
    });
  });

  test('fetches subsequent pages until all items are collected (issue #880)', async () => {
    const pageSize = 2;
    mockedAxios.get
      .mockResolvedValueOnce({
        data: {
          servers: [{ path: '/1' }, { path: '/2' }],
          total_count: 5,
          limit: pageSize,
          offset: 0,
        },
      })
      .mockResolvedValueOnce({
        data: {
          servers: [{ path: '/3' }, { path: '/4' }],
          total_count: 5,
          limit: pageSize,
          offset: 2,
        },
      })
      .mockResolvedValueOnce({
        data: {
          servers: [{ path: '/5' }],
          total_count: 5,
          limit: pageSize,
          offset: 4,
        },
      });

    const items = await fetchAllPages<{ path: string }>({
      url: '/api/servers',
      itemsKey: 'servers',
      pageSize,
    });

    expect(items.map((i) => i.path)).toEqual(['/1', '/2', '/3', '/4', '/5']);
    expect(mockedAxios.get).toHaveBeenCalledTimes(3);
    expect(mockedAxios.get).toHaveBeenNthCalledWith(2, '/api/servers', {
      params: { limit: pageSize, offset: 2 },
    });
    expect(mockedAxios.get).toHaveBeenNthCalledWith(3, '/api/servers', {
      params: { limit: pageSize, offset: 4 },
    });
  });

  test('stops when a full page returns with no total_count but short final page is not needed', async () => {
    const pageSize = 2;
    mockedAxios.get
      .mockResolvedValueOnce({
        data: {
          agents: [{ path: '/a1' }, { path: '/a2' }],
        },
      })
      .mockResolvedValueOnce({
        data: {
          agents: [{ path: '/a3' }],
        },
      });

    const items = await fetchAllPages<{ path: string }>({
      url: '/api/agents',
      itemsKey: 'agents',
      pageSize,
    });

    expect(items).toHaveLength(3);
    expect(mockedAxios.get).toHaveBeenCalledTimes(2);
  });

  test('uses skip param for custom entity endpoints', async () => {
    mockedAxios.get.mockResolvedValueOnce({
      data: {
        records: [{ path: '/prompt/1' }],
        total_count: 1,
        skip: 0,
        limit: CUSTOM_ENTITY_PAGE_SIZE,
      },
    });

    const items = await fetchAllPages({
      url: '/api/custom/prompt_template',
      itemsKey: 'records',
      pageSize: CUSTOM_ENTITY_PAGE_SIZE,
      offsetParam: 'skip',
    });

    expect(items).toHaveLength(1);
    expect(mockedAxios.get).toHaveBeenCalledWith('/api/custom/prompt_template', {
      params: {
        limit: CUSTOM_ENTITY_PAGE_SIZE,
        skip: 0,
      },
    });
  });

  test('stops after maxPages even if more data is claimed', async () => {
    mockedAxios.get.mockResolvedValue({
      data: {
        skills: [{ path: '/s' }, { path: '/t' }],
        total_count: 100,
      },
    });

    const items = await fetchAllPages({
      url: '/api/skills',
      itemsKey: 'skills',
      pageSize: 2,
      maxPages: 3,
    });

    expect(mockedAxios.get).toHaveBeenCalledTimes(3);
    expect(items).toHaveLength(6);
    expect(DEFAULT_MAX_PAGES).toBeGreaterThan(0);
  });

  test('returns empty array when collection key is missing', async () => {
    mockedAxios.get.mockResolvedValueOnce({ data: { total_count: 0 } });

    const items = await fetchAllPages({
      url: '/api/servers',
      itemsKey: 'servers',
    });

    expect(items).toEqual([]);
  });
});

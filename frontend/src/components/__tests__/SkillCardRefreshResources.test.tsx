import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import axios from 'axios';

jest.mock('react-markdown', () => ({
  __esModule: true,
  default: (props: { children: string }) => <div>{props.children}</div>,
}));
jest.mock('remark-gfm', () => ({ __esModule: true, default: () => {} }));
jest.mock('axios');
jest.mock('../SkillResources', () => () => null);
jest.mock('../SecurityScanModal', () => () => null);
jest.mock('../StarRatingWidget', () => () => null);
jest.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({ user: { username: 'dev1', is_admin: false } }),
}));

import SkillCard from '../SkillCard';

const mockedAxios = axios as jest.Mocked<typeof axios>;

const skill = {
  name: 'Doc Coauthor',
  path: '/skills/doc-coauthor',
  skill_md_url: 'https://github.com/example/skills/blob/main/doc-coauthor/SKILL.md',
  visibility: 'public',
  is_enabled: true,
  num_stars: 0,
  registry_name: 'local',
};

function renderCard(props: Partial<React.ComponentProps<typeof SkillCard>> = {}) {
  const onShowToast = jest.fn();
  render(
    <SkillCard skill={skill as never} onToggle={jest.fn()} onShowToast={onShowToast} {...props} />
  );
  return { onShowToast };
}

describe('SkillCard refresh resources', () => {
  beforeEach(() => {
    jest.resetAllMocks();
    mockedAxios.get.mockResolvedValue({ data: { csrf_token: 'csrf-1' } });
  });

  it('is hidden unless the caller may modify the skill', () => {
    renderCard({ canModify: false });
    expect(screen.queryByLabelText(/Refresh resources for/)).not.toBeInTheDocument();
  });

  it('posts to the refresh endpoint with the CSRF token and reports the count', async () => {
    mockedAxios.post.mockResolvedValue({
      data: { resource_manifest: null, resources_discovered: 3 },
    });
    const { onShowToast } = renderCard({ canModify: true });

    fireEvent.click(screen.getByLabelText('Refresh resources for Doc Coauthor'));

    await waitFor(() => expect(onShowToast).toHaveBeenCalledWith('Discovered 3 resource file(s)', 'success'));
    expect(mockedAxios.post).toHaveBeenCalledWith(
      '/api/skills/doc-coauthor/refresh-resources',
      undefined,
      { headers: { 'X-CSRF-Token': 'csrf-1' } }
    );
  });

  it('shows the server error when the refresh is refused', async () => {
    mockedAxios.post.mockRejectedValue({ response: { data: { detail: 'Access denied' } } });
    const { onShowToast } = renderCard({ canModify: true });

    fireEvent.click(screen.getByLabelText('Refresh resources for Doc Coauthor'));

    await waitFor(() => expect(onShowToast).toHaveBeenCalledWith('Access denied', 'error'));
  });
});

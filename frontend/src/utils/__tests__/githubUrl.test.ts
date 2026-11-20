import { DEFAULT_GITHUB_URL, resolveGithubUrl } from '../githubUrl';

describe('resolveGithubUrl', () => {
  it('defaults to the CCCS fork, not the upstream repository', () => {
    expect(DEFAULT_GITHUB_URL).toBe('https://github.com/CybercentreCanada/mcp-gateway-registry');
    expect(resolveGithubUrl(undefined)).toBe(DEFAULT_GITHUB_URL);
  });

  it('falls back to the default for empty values', () => {
    expect(resolveGithubUrl('')).toBe(DEFAULT_GITHUB_URL);
    expect(resolveGithubUrl(null)).toBe(DEFAULT_GITHUB_URL);
  });

  it('uses a branch URL supplied at build time', () => {
    const branchUrl =
      'https://github.com/CybercentreCanada/mcp-gateway-registry/tree/cccs-main_1.37.0_aks-fixes';
    expect(resolveGithubUrl(branchUrl)).toBe(branchUrl);
  });

  it.each([
    'javascript:alert(1)',
    'data:text/html,<script>alert(1)</script>',
    '//evil.example/repo',
    'not a url',
  ])('rejects the unsafe value %s', (value) => {
    expect(resolveGithubUrl(value)).toBe(DEFAULT_GITHUB_URL);
  });
});

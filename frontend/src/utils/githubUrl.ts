import { isSafeUrl } from './safeUrl';

// Repository the header GitHub icon links to. Override at build time with
// VITE_GITHUB_URL (for example a /tree/<branch> URL) to point at the deployed branch.
export const DEFAULT_GITHUB_URL = 'https://github.com/CybercentreCanada/mcp-gateway-registry';

export const resolveGithubUrl = (injected: string | null | undefined): string =>
  injected && isSafeUrl(injected) ? injected : DEFAULT_GITHUB_URL;

# cccs-main_1.37.0 port audit

This is the working plan and tracker for moving the CCCS customizations onto upstream `1.37.0`.
It supersedes the "explicitly NOT ported" list in [cccs-main-1.29.0-port-audit.md](cccs-main-1.29.0-port-audit.md): the goal this time is that **nothing is left unported**.
Every item ends up as one of: covered upstream, obsolete (upstream changed the mechanism), ported, or deliberately kept in the deployment repo.

Status values used below: `COVERED` (upstream has it), `OBSOLETE` (mechanism no longer exists upstream), `PORT` (still needed, not upstream), `PARTIAL` (upstream has part of it, behavior must be re-tested), `REVIEW` (needs a design decision before porting).
Classifications come from reading the `1.37.0` tag source and from trial-applying each of our commits to a pristine `1.37.0` tree; nothing here has been run or deployed yet, so treat `COVERED`/`OBSOLETE` as "verify when the port lands".

## Branches and deployment policy

| Branch | Cut from | Purpose |
|---|---|---|
| `cccs-main_1.37.0` | upstream tag `1.37.0` | Pristine baseline, never committed to except by merging reviewed work. |
| `cccs-main_1.37.0_aks-fixes` | `cccs-main_1.37.0` | Working branch. Ports are applied here one commit per item, like `cccs-main_1.29.0_aks-fixes`. |

- The `deployment/` submodule tracks `main` of `trinity-registry-deployment` (`.gitmodules` `branch = main`) and is no longer pointed at feature branches. If a feature branch must be deployed, deploy it from a checkout of `trinity-registry-deployment` itself and leave the submodule alone.
- `trinity-registry-deployment` `main` has been replaced by squash merges more than once (`db4f87b` no longer exists; current tip is `a0faeb1`). The `1.29.0` line's pointer was repaired to `a0faeb1`.
- Source lineages being consolidated: the `1.27.1` line (`cccs-main` through `skills-parse-hardening_and_mcp-proxy-timeout`, 46 CCCS-only commits, deployed on staging and prod) and the `1.29.0` line (`cccs-main_1.29.0_aks-fixes`, deployed on dev).

## Pre-start fixes (done)

- `uv.lock`: both our lines carry a lockfile rewritten to the internal PyPI mirror (`1.27.1` line: 2252 mirror references, `1.29.0` line: 1619, upstream: 0). It was restored from the upstream tag on the `1.29.0` line; the `1.37.0` branch starts clean. Never commit a lockfile after running `uv sync` on the host with `UV_INDEX_URL` set; check `git diff uv.lock` first. Dependencies did not change (`pyproject.toml` is identical), so the restore is safe.
- Overlay build files from `cccs-is` (`docker/Dockerfile.registry.custom`, `build_registry.sh`, `docker/Dockerfile.auth.custom`, `build_auth.sh`) apply cleanly to `1.37.0` and will be ported as files. The registry overlay copies only `registry/core/nginx_service.py`; the auth overlay copies `auth_server/server.py` and `registry/core/config.py`. Both expect a `./ca-certificates.crt` in the repo root and default to the `1.29.0` base image. Extend the copy lists and base tags as each code port lands, because an overlay built from files of a different version than its base image is not safe.
- Image builds must pass `--build-arg BUILD_VERSION=<tag>` (see `deploy/CUSTOM_IMAGE_BUILD.md` in the deployment repo). Overlay builds inherit `BUILD_VERSION` from the base image, which is a point in their favor.

## Upstream changes that affect our deployment values

From the `1.37.0` release notes (`docs/release-notes/1.37.0.md`), the items that touch what we run:

- `tools: ["*"]` in a scope no longer grants every MCP method, only `tools/call`. Our `trinity-*` scopes must be re-checked (and `resources/read` / `prompts/get` listed explicitly where needed).
- Helm: `SSRF_ALLOWED_CIDRS` moves to `global.ssrfAllowedCidrs`, which wins over `registry.app.ssrfAllowedCidrs` and is now also passed to the auth-server and mcpgw. Our env values set only `registry.app.ssrfAllowedCidrs` (`10.0.0.0/8,172.0.0.0/8`); move it to the global key. Extra env can no longer set `SSRF_ALLOWED_CIDRS`.
- Helm: session cookie overrides move to stack globals (`global.sessionMaxAgeSeconds`, `global.sessionCookieSecure`, `global.sessionCookieDomain`). Any per-subchart override now fails the render.
- `/branding` is a reserved path; check existing server/agent registrations for a path starting with `/branding`.
- Per-user egress: users must sign in again after the upgrade and reconnect their accounts once; consents must finish on the host the UI runs on. This interacts with our ingress-relay feature (see C6).
- The auth server refuses to start with an edited `session` block in `oauth2_providers.yml`, and an OIDC login with no subject now fails with 401 (relevant to Entra).
- MCP SDK 2.x, a 1 MB body limit for MCP requests, JSON-RPC `-32003` on refused requests, and an `Origin` check for browser-session MCP requests.
- `mongodb-configure` now runs the registry image's own `scripts/init-mongodb-ce.py` (no pip install, no ConfigMap copy). The `mongodb-kubernetes` operator is the only MongoDB dependency of the stack chart, which matches our decision to stay off Bitnami.

### OAuth token caching (1.36.0)

Confirmed present in the `1.37.0` tag: `auth_server/obo_token_cache.py`, settings in `registry/core/config.py`, wiring in `charts/registry/templates/configmap-egress.yaml`. It is an opt-in OBO token cache (`EGRESS_OBO_CACHE_ENABLED`, default `false`, plus `EGRESS_OBO_CACHE_MAX_TTL_SECONDS=300` and `EGRESS_OBO_CACHE_EXPIRY_SKEW_SECONDS=30`).
Enabling it requires `auth-server.egressAuth.credentialEncryptionKey` set to the same value as the registry's `egressAuth.credentialEncryptionKey`.
The 1.37.0 notes add a per-provider JWKS refetch (rate limited to once a minute); they do not remove the cache.

## Port matrix

"Trial" is the result of cherry-picking the commit onto a pristine `1.37.0` tree: `clean` applies without conflicts, `conflict` needs a manual port, `n/a` is a docs/data change.

### A. Charts and infrastructure

| # | Item | Source commits | Status | Notes |
|---|---|---|---|---|
| A1 | Registry ingress: nginx ingress class / TLS support | `f990b578`, `1eed68a9` (1.29.0 line port) | PORT | `1.37.0` `charts/registry/templates/ingress.yaml` is still ALB-annotation only. Trial: conflict. |
| A2 | `registrySubdomain` in secrets, ingress, egress configmaps | `037e35fa`, `d87137e4`, `1dd8d684` | COVERED | Upstream now reads `global.ingress.registrySubdomain` in registry ingress and secret, auth-server secret, keycloak-configure, mcpgw, oauth-provider-secret and the egress configmaps. This also closes the keycloak-configure/mcpgw gap noted in the 1.29.0 audit. Verify by rendering with the staging value `mcpregistry-stg`. |
| A3 | mongodb-configure `pythonImage`, `PIP_INDEX_URL`, setup-mongodb fixes, image-registry override | `81821da8`, `48493c7c`, `38bfc6a3`, `a9210bcc`, `a9b08a77` | OBSOLETE | The job now runs the registry image and installs nothing at runtime; image registry comes from `global.image.registry` / `image.registry`. |
| A4 | openbao-init: image override, `openbaoInitEnabled` escape hatch, pure-Python rewrite (no runtime apt-get), HTTP 204 handling, test update | `607380f2`, `7205d71f`, `d87137e4`, `02fd175e`, `9dd29761`, `dcc057b8` | DONE | The pure-Python rewrite is needed and needs no new image: it uses only the standard library, so the Job runs from the `python:3.14-slim` image that `deploy/sync-images.sh --python` already mirrors (set `global.openbaoInitImage` to the ACR path). Rewritten against the `1.37.0` flow (configurable auth mount, OBO-cache policy and role, stable reviewer token). `global.openbaoInitImage` and `global.openbaoInitEnabled` kept. Verified by helm unittest (129 pass) and `tests/unit/test_chart_openbao_init_script.py`, which runs the real script against a fake OpenBao and Kubernetes API (9 tests) |
| A5 | `charts/registry` `app.githubExtraHosts` -> `GITHUB_EXTRA_HOSTS` | `6d0c6844` | DONE | Only `charts/mcpgw` has it in `1.37.0`. Trial: conflict (reserved-env list, deployment, test). Upstream candidate. |
| A6 | configmap namespace syntax fix | `037e35fa` (part) | OBSOLETE | The mongodb-configure ConfigMap no longer exists. |
| A7 | Overlay build and custom Dockerfiles | `3d0136cf`, `58cc633d` (files) | PORT | Trial: clean. Extend to overlay every patched module. |
| A8 | Move `ssrfAllowedCidrs` to `global.ssrfAllowedCidrs` in env values | (deployment repo) | PORT | Values change, no chart change. |

### B. RBAC, scopes and UI permissions

| # | Item | Source commits | Status | Notes |
|---|---|---|---|---|
| B1 | Trinity scope JSONs (`trinity-all`, `trinity-developers`, `trinity-developers-execute`, `trinity-power-users`) | `97f34deb`, `ae97f87f`, `21469e05`, `3c7850e7`, `9f8e38de`, `1a403635` | PORT | Absent upstream. The seed script loads scope files from its own directory inside the image, and the deployment repo also keeps `scopes/`. REVIEW where the source of truth lives (bake into the image vs import from the deployment repo) before porting. Re-validate against the `tools: ["*"]` change. |
| B2 | Entra Admin/Developer/Power-User group mapping via the mongodb-configure ConfigMap | `3c7850e7`, `9f8e38de`, `51cbeb90`, `1a403635`, `ff740681` | OBSOLETE (mechanism) / PORT (capability) | The ConfigMap is gone and the script only accepts `ENTRA_GROUP_ADMIN_ID`. Developer and Power-User group IDs need either new env support in `scripts/init-mongodb-ce.py` or an import via the deployment repo. |
| B3 | `publishSkillEnabled` gating | `57413466` | OBSOLETE | The seed admin scopes (`scripts/registry-admins.json`, `mcp-registry-admin.json`) grant `publish_skill: ["all"]` unconditionally, as on `1.29.0`. |
| B4 | Owner-based toggle for servers/agents/skills, non-admin toggle in the UI | `7015d9ad`, `cc81bc11`, `4d72cbdd`, `0be9045e` | PARTIAL | Upstream `toggle_agent` already lets an owner pass the per-resource check, but still requires the `toggle_service` scope permission first. Our `_has_toggle_agent_permission` and `hasScopedUiPermission` do not exist upstream. Re-test with the trinity scopes, then port only the delta. |
| B5 | Egress auth 403 and proxy URL overwrite for power users | `9aa19b2b` | REVIEW | `_require_admin_or_registrant` does not exist upstream, and `1.36.0`/`1.37.0` reworked per-user egress (vault keyed on the IdP account id). Reproduce the original problem on `1.37.0` before porting. |
| B6 | Agent card refresh must not overwrite the gateway URL | `69006163`, `cf869627` | PORT | Absent upstream. Trial: conflict (small). |

### C. Application features and fixes

| # | Item | Source commits | Status | Notes |
|---|---|---|---|---|
| C1 | SKILL.md security scan with GHES credentials (`_build_skill_fetch_auth`) | `21013bdf` | PORT | Function absent upstream; the surrounding code changed. Trial: conflict (`skill_routes.py`, `skill_service.py`). |
| C2 | SKILL.md fetch error hardening | `abd522be` | DONE | Absent upstream; ported with its tests (`test_skill_service_fetch_errors.py`) |
| C3 | Skill ownership gating, frontmatter tag parsing, resource refresh action | `3d13a70e` | PARTIAL | Backend done: frontmatter is parsed with `yaml.safe_load`, so block-style `tags:` lists work (the bug reproduced on `1.37.0`). The resource-refresh route already requires CSRF upstream. Still to do: the frontend ownership gating and the refresh-resources button (`Dashboard.tsx`, `SkillCard.tsx`, `SkillsSection.tsx`), with the UI tests |
| C4 | `url_guard` literal-IP allowlist fix | `26f1cd3e` | DONE | Ported with a narrower design than on `1.29.0`: only the skill allowlist (`GITHUB_EXTRA_HOSTS`) trusts operator-listed literal IPs, because `1.37.0` has a test that a literal private IP from a configured base URL must not be trusted. See `cccs-upstream-contributions.md` item 1. |
| C5 | Forwarded scheme: nginx MCP blocks use `$real_scheme`/`$forwarded_proto`, not `$scheme` | `fe59c5f8` | DONE | Still `$scheme` upstream in the MCP transport blocks. Ported as `_forwarded_scheme_var` (`$real_scheme` / `$forwarded_proto` per template). The 308 symptom itself may already be fixed on `1.37.0` because the auth server now strips `x-forwarded-*` before calling the upstream (see C6); the nginx value still feeds `X-Original-URL`. Verify against an https-redirecting upstream behind the ingress before relying on it. Upstream candidate, lower priority |
| C6 | Forwarded scheme in the auth server | `4cbc52e4` | COVERED | `1.37.0` `_PROXY_CONTEXT_HEADERS` already strips `forwarded`, `x-forwarded-host`, `x-forwarded-port` and `x-forwarded-proto` in `_forward_headers`. The cherry-pick applied cleanly only because the lines differ; behavior is already present. Not ported. |
| C7 | Agent proxying must not downgrade to http | `10b20da2` | DONE | Agent blocks still sent `$scheme` straight to the agent backend and had no slashless `/agent/<path>` redirect. Ported using the same `_forwarded_scheme_var` instead of adding a `$real_scheme` map to the http-only template, and the redirect keeps the query string (`$uri/$is_args$args`) where the original used `$request_uri/` |
| C8 | A2A shared gateway/agent token (`A2A_SHARED_GATEWAY_AGENT_TOKEN`) | `05cbdd60` (1.27.1 line), `58cc633d` (1.29.0 port, use this one) | PORT | Absent upstream. Conflicts in `charts/auth-server`. Must be re-checked against the new egress/OBO code and the per-user vault change. |
| C9 | Ingress relay of the access token to listed MCP servers (`ingressRelayAllowedServers`) | `dbbb0154` | REVIEW | Absent upstream and it overlaps the reworked egress credentials and the OBO cache. Staging and prod use it (`/rag`, `/rag-stg`, `/mcp-a2a-bridge`). Decide whether to port as-is or re-express it on the new egress model; either way it must exist on dev. |
| C10 | MCP proxy timeout | deployment values (`mcpProxyTimeout`, `MCP_PROXY_TIMEOUT`) | COVERED | Auth-server chart has `app.mcpProxyTimeout` and nginx derives `proxy_read_timeout` from `MCP_PROXY_TIMEOUT`. The registry chart does not reserve the name, so the existing registry `extraEnv` entry keeps working. |
| C11 | UI GitHub link points at the fork (and the deployed branch) | new | PORT | `frontend/src/components/Layout.tsx` hardcodes `GITHUB_URL = 'https://github.com/agentic-community/mcp-gateway-registry'`. Plan: read a build-time `VITE_GITHUB_URL`, defaulting to `https://github.com/CybercentreCanada/mcp-gateway-registry`, and pass the branch URL when building the image. The FAQ links in `Dashboard.tsx` and `SkillsSection.tsx` also point upstream. |

## Apply trial summary

48 commits evaluated: 7 apply cleanly, 40 conflict, 1 is empty on `1.37.0`.
The clean ones are `607380f2`, `7205d71f`, `7526db9f` (docs), `4cbc52e4`, `97f34deb`, `abd522be` and `3d0136cf`.
A clean apply does not prove the behavior is missing upstream: `4cbc52e4` applied cleanly but `1.37.0` already strips the same headers under a different name. Check behavior, not just the diff, before porting each item.
The 40 conflicts are mostly the chart items that are obsolete or already reimplemented upstream (A2, A3, A6, B2) plus code that moved (C1, C3, C4, B4, B5).
A verbatim-line comparison of each commit against `1.37.0` found essentially no commit already present upstream (all but a few at 0 to 11 percent), which is consistent with the matrix above.

## Suggested order

1. A5, A7, C4, C5, C7 (small, well understood, upstream candidates), each with its tests.
2. A4, A1 (chart work with `helm unittest` coverage).
3. C2, C1, B6, C11.
4. B1, B2 (decide source of truth first), B4, B5, C3.
5. C8, C9 (largest design surface, depend on the egress rework).

Each item is one commit on `cccs-main_1.37.0_aks-fixes`, with the full test suite and `helm unittest` run before pushing. Dev is redeployed once after steps 1 to 3 to validate the base, then again after the rest.

# Upstream contribution candidates

Changes we carry on the CCCS fork that are worth proposing to `agentic-community/mcp-gateway-registry`.
Each section is written so it can become an issue or PR description, and is kept only while it still applies to the release we deploy.
Current reference release: `1.37.0`.
We curate and run each change ourselves first (see [cccs-main-1.37.0-port-audit.md](cccs-main-1.37.0-port-audit.md)), then propose it.

| # | Title | Relevant to 1.37.0 | Our commit(s) |
|---|---|---|---|
| 1 | Allowlisted literal-IP hosts are never relaxed by the SSRF guard | Yes, read from the tag, not yet reproduced on `1.37.0` | `26f1cd3e` |
| 2 | `charts/registry` has no `githubExtraHosts` value | Yes | `6d0c6844` |
| 3 | Generated nginx MCP blocks forward `$scheme` instead of the terminated-TLS scheme | Yes | `fe59c5f8` |
| 4 | Auth server drops the forwarded scheme | Yes (apply trial clean) | `4cbc52e4` |
| 5 | `openbao-init` needs internet access at runtime and has no image override or opt-out | Yes | `607380f2`, `7205d71f`, `02fd175e`, `9dd29761` |
| 6 | SKILL.md fetch errors are not actionable | Yes (apply trial clean) | `abd522be` |
| 7 | Registry ingress template supports ALB only | Yes | `1eed68a9` |

## 1. Allowlisted literal-IP hosts are never relaxed by the SSRF guard

Problem: with `GITHUB_EXTRA_HOSTS` set for a GitHub Enterprise Server, registering a SKILL.md fails with `Redirect to unsafe URL blocked: https://<internal-IP>/...` when the GHES raw-content server redirects to a raw internal backend IP, even after that IP is added to `GITHUB_EXTRA_HOSTS`.

Cause: the DNS-resolved path computes `trusted_hostname = allowlist.allows_host(hostname)` and passes it to `_is_blocked_ip`, which relaxes the private-range block for allowlisted hosts. The literal-IP branches do not: in `registry/utils/url_guard.py` at `1.37.0`, `validate_url` and the three `GuardedAsyncTransport` / pinned-resolver sites call `_is_blocked_ip(hostname, allowlist, allow_private=...)` without `trusted_hostname`, so an allowlisted literal IP is blocked.

Proposed change: pass `trusted_hostname=allowlist.allows_host(hostname.lower())` in each literal-IP branch. Hard-denied categories (cloud metadata `169.254.169.254`, link-local, loopback, multicast, reserved, unspecified) are enforced in `_ip_denial_reason` before the relaxation, so they stay blocked even when allowlisted.

Tests (already written on the `1.29.0` line): allowlisted literal IP allowed; non-allowlisted private IP still rejected; metadata IP rejected even if allowlisted; same for the sync and async pinned transports.

Discussion point for maintainers: upstream documents `trusted_hostname` as applying "after DNS resolution". Operators who want the redirect target admitted may be expected to use `SSRF_ALLOWED_CIDRS` instead, but the skill profile deliberately has no CIDR support. The PR should state which direction upstream prefers (literal IPs in `GITHUB_EXTRA_HOSTS`, or CIDR support for the skill profile).

## 2. `charts/registry` has no `githubExtraHosts` value

Problem: `charts/mcpgw` exposes `app.githubExtraHosts` and reserves `GITHUB_EXTRA_HOSTS`, but `charts/registry` does not, although the registry is the service that fetches SKILL.md. Operators have to use `extraEnv`, and the name is not reserved, so it silently collides with future chart keys.

Proposed change: add `app.githubExtraHosts` to `charts/registry/values.yaml`, render `GITHUB_EXTRA_HOSTS` in `templates/deployment.yaml` when set, add the name to `reserved-env-names.txt`, and add two `extra_env_test.yaml` cases (omitted by default, rendered when set). Document that it is a separate allowlist from `ssrfAllowedHosts`/`ssrfAllowedCidrs`, which feed the proxy profile and are never consulted for skill fetches.

## 3. Generated nginx MCP blocks forward `$scheme` instead of the terminated-TLS scheme

Problem: when an ingress or load balancer terminates TLS, nginx inside the registry pod sees `$scheme = http`. The generated per-server location blocks send `X-Forwarded-Proto $scheme` and `X-Original-URL $scheme://...`, so upstreams behind their own force-SSL redirect answer 308 to the gateway.

Proposed change: forward the template's forwarded-proto map variable (`$real_scheme` in the http+https template, `$forwarded_proto` in the http-only template) through a `_forwarded_scheme_var` attribute set in `NginxConfigService.__init__`. Includes a unit test asserting the generated block never contains `$scheme` in those two headers.

## 4. Auth server drops the forwarded scheme

Companion to item 3 for the auth server's own proxy hop (`auth_server/server.py`, 25 added lines). Fold into the item 3 PR if maintainers prefer one change.

## 5. `openbao-init` needs internet access at runtime and has no image override or opt-out

Problem: the `openbao-init` Job template runs `apt-get install curl jq kubectl` inside `public.ecr.aws/docker/library/python:3.14-slim`. In air-gapped or egress-restricted clusters it cannot run, and there is no value to change the image or to skip the Job.

Proposed change: `global.openbaoInitImage` and `global.openbaoInitEnabled` values (small, applies cleanly), and a stdlib-only Python implementation of the init flow (no apt, no kubectl, no jq) using the Job service account's projected token. Includes HTTP 204 handling and an updated unit test.

## 6. SKILL.md fetch errors are not actionable

Problem: a failed SKILL.md fetch surfaces a generic error. Proposed change (`registry/services/skill_service.py`): normalize the configured credential, and describe request errors and HTTP status failures with the URL host and a remediation hint. Four files, with a new `test_skill_service_fetch_errors.py`.

## 7. Registry ingress template supports ALB only

Problem: `charts/registry/templates/ingress.yaml` always emits `alb.ingress.kubernetes.io/*` annotations. On AKS with an nginx ingress controller those are ignored, and TLS is not configurable.

Proposed change: render ALB annotations only when the class is ALB, and add `ingress.tls` support with a secret name, as in `1eed68a9`.

## Process notes

- Open an issue first for items 1, 3 and 7 (behavior or design choices), and a direct PR for items 2, 5 and 6.
- Reproduce items 1 and 3 on a stock `1.37.0` image before filing, so the report does not depend on our patches.
- Remove a section here when upstream ships the fix, or note the release that contains it.

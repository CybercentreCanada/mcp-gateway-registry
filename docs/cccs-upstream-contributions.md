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
| 4 | `openbao-init` needs internet access at runtime and has no image override or opt-out | Yes | `607380f2`, `7205d71f`, `02fd175e`, `9dd29761` |
| 5 | SKILL.md fetch errors are not actionable | Yes (apply trial clean) | `abd522be` |
| 6 | Registry ingress template supports ALB only | Yes | `1eed68a9` |

## 1. Allowlisted literal-IP hosts are never relaxed by the SSRF guard

Problem: with `GITHUB_EXTRA_HOSTS` set for a GitHub Enterprise Server, registering a SKILL.md fails with `Redirect to unsafe URL blocked: https://<internal-IP>/...` when the GHES raw-content server redirects to a raw internal backend IP, even after that IP is added to `GITHUB_EXTRA_HOSTS`.

Cause: the DNS-resolved path computes `trusted_hostname = allowlist.allows_host(hostname)` and passes it to `_is_blocked_ip`, which relaxes the private-range block for allowlisted hosts. The literal-IP branches do not: in `registry/utils/url_guard.py` at `1.37.0`, `validate_url` and the three `GuardedAsyncTransport` / pinned-resolver sites call `_is_blocked_ip(hostname, allowlist, allow_private=...)` without `trusted_hostname`, so an allowlisted literal IP is blocked.

Proposed change: add an opt-in `trust_literal_hosts` flag on `_Allowlist`, set only by `_skill_allowlist()` (the operator types `GITHUB_EXTRA_HOSTS` by hand), and pass `trusted_hostname=allowlist.trusts_literal_ip(hostname.lower())` at the four literal-IP call sites (`validate_url`, `_pin_request`, `_pin_request_async`, `_validated_addresses` and its async twin). Allowlists derived from a configured base URL (for example the Entra Graph profile, covered by `test_configured_private_literal_is_not_trusted_proxy_hostname`) keep rejecting literal private IPs. Hard-denied categories (cloud metadata `169.254.169.254`, link-local, multicast, unspecified) are enforced in `_ip_denial_reason` before the relaxation, so they stay blocked even when listed.

Tests (on `cccs-main_1.37.0_aks-fixes`): a listed literal IP is trusted by `validate_url`, the sync and async pinned transports and both `_validated_addresses` variants; an unlisted literal IP is still rejected; a listed metadata IP is still rejected; a literal IP in `SSRF_ALLOWED_HOSTS` is not trusted on the proxy profile (admitted by `SSRF_ALLOWED_CIDRS` instead). The five positive tests fail without the change.

Discussion point for maintainers: upstream documents `trusted_hostname` as applying "after DNS resolution", so a hostname allowlist deliberately does not admit literal IPs on most profiles. The alternative to this change is CIDR support on the skill profile, which `_skill_allowlist` deliberately lacks. The PR should state which direction upstream prefers.

## 2. `charts/registry` has no `githubExtraHosts` value

Problem: `charts/mcpgw` exposes `app.githubExtraHosts` and reserves `GITHUB_EXTRA_HOSTS`, but `charts/registry` does not, although the registry is the service that fetches SKILL.md. Operators have to use `extraEnv`, and the name is not reserved, so it silently collides with future chart keys.

Proposed change: add `app.githubExtraHosts` to `charts/registry/values.yaml`, render `GITHUB_EXTRA_HOSTS` in `templates/deployment.yaml` when set, add the name to `reserved-env-names.txt`, and add two `extra_env_test.yaml` cases (omitted by default, rendered when set). Document that it is a separate allowlist from `ssrfAllowedHosts`/`ssrfAllowedCidrs`, which feed the proxy profile and are never consulted for skill fetches.

## 3. Generated nginx MCP blocks forward `$scheme` instead of the terminated-TLS scheme

Problem: when an ingress or load balancer terminates TLS, nginx inside the registry pod sees `$scheme = http`. The generated per-server location blocks send `X-Forwarded-Proto $scheme` and `X-Original-URL $scheme://...`, so upstreams behind their own force-SSL redirect answer 308 to the gateway.

Proposed change: forward the template's forwarded-proto map variable (`$real_scheme` in the http+https template, `$forwarded_proto` in the http-only template) through a `_forwarded_scheme_var` attribute set in `NginxConfigService.__init__`. Includes a unit test asserting the generated block never contains `$scheme` in those two headers.

## 4. `openbao-init` needs internet access at runtime and has no image override or opt-out

Problem: the `openbao-init` Job template runs `apt-get install curl jq kubectl` inside `public.ecr.aws/docker/library/python:3.14-slim`. In air-gapped or egress-restricted clusters it cannot run, and there is no value to change the image or to skip the Job.

Proposed change: `global.openbaoInitImage` and `global.openbaoInitEnabled` values (small, applies cleanly), and a stdlib-only Python implementation of the init flow (no apt, no kubectl, no jq) using the Job service account's projected token. Includes HTTP 204 handling and an updated unit test.

## 5. SKILL.md fetch errors are not actionable

Problem: a failed SKILL.md fetch gives an empty or unhelpful reason (many httpx network errors stringify to an empty string), and a token pasted with a `Bearer ` prefix or stray whitespace is sent as-is.

Proposed change (`registry/services/skill_service.py`): `_normalize_credential` strips whitespace and a pasted `Bearer ` prefix; `_describe_request_error` builds a non-empty, credential-free reason (timeout, connect error, invalid URL, illegal header characters) and is never derived from `str(error)`, because httpx can echo header values; `_describe_http_status` explains 401/403/404 for bearer and API-key auth (GitHub Enterprise hides private repositories as 404). The timeout message names the case where public github.com is not reachable from the cluster. Includes `test_skill_service_fetch_errors.py`.

## 6. Registry ingress template supports ALB only

Problem: `charts/registry/templates/ingress.yaml` always emits `alb.ingress.kubernetes.io/*` annotations and takes only `ingress.className`. On AKS with an nginx ingress controller the ALB annotations are ignored and there is no TLS block.

Proposed change (as in `1eed68a9`): resolve the class from `ingress.className`, then `global.ingress.className`, defaulting to `alb`; emit the ALB annotations only for `alb`; for `nginx` emit `ssl-redirect`, `force-ssl-redirect`, `proxy-body-size`, and read/send timeouts; add a `spec.tls` block when `ingress.tls` (or the global equivalent) is true, with an optional `tlsSecretName`; allow extra `ingress.annotations`. Needs rebasing on the `1.37.0` template, which already handles `registrySubdomain` and path routing mode itself.

## Process notes

- Open an issue first for items 1, 3 and 6 (behavior or design choices), and a direct PR for items 2, 4 and 5.
- Reproduce items 1 and 3 on a stock `1.37.0` image before filing, so the report does not depend on our patches.
- Remove a section here when upstream ships the fix, or note the release that contains it.

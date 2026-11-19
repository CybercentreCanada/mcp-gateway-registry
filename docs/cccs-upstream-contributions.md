# Upstream contribution candidates

Changes we carry on the CCCS fork that are worth proposing to `agentic-community/mcp-gateway-registry`.
Each section is written so it can become an issue or PR description, and is kept only while it still applies to the release we deploy.
Current reference release: `1.37.0`.
We curate and run each change ourselves first (see [cccs-main-1.37.0-port-audit.md](cccs-main-1.37.0-port-audit.md)), then propose it.

| # | Title | Relevant to 1.37.0 | Our commit(s) |
|---|---|---|---|
| 1 | Allowlisted literal-IP hosts are never relaxed by the SSRF guard | Yes, read from the tag, not yet reproduced on `1.37.0` | `26f1cd3e` |
| 2 | `charts/registry` has no `githubExtraHosts` value | Yes | `6d0c6844` |
| 3 | Generated nginx MCP and agent blocks forward `$scheme` instead of the terminated-TLS scheme | Yes for agent blocks and `X-Original-URL`; the 308 symptom on MCP servers may already be fixed by the auth server stripping `x-forwarded-*` | `fe59c5f8` |
| 4 | `openbao-init` needs internet access at runtime and has no image override or opt-out | Yes | `607380f2`, `7205d71f`, `02fd175e`, `9dd29761` |
| 5 | SKILL.md fetch errors are not actionable | Yes (apply trial clean) | `abd522be` |
| 6 | Registry ingress template supports ALB only | Yes | `1eed68a9` |
| 7 | The `registry-egress-internal` NetworkPolicy isolates the registry from every other client | Yes, reproduced on `1.37.0` (dev outage) | `d834ed0c` |
| 8 | The egress credential encryption key can only be passed as a Helm value | Yes, hit during the dev deploy | deployment repo (`--set-file` workaround) |
| 10 | The auth-server image has no `hvac`, so the OBO token cache cannot start on the Helm/OpenBao path | Yes, reproduced with the stock `1.37.0` image | `build_auth.sh` and `Dockerfile.auth.custom` (workaround) |
| 9 | The stack chart's OpenBao uses the `file` backend, which OpenBao 2.7 removed, and pins the image in two places | Yes, migrated on dev (2.5.5 to 2.7.2) | deployment repo (values override, migration manifest) |

## 1. Allowlisted literal-IP hosts are never relaxed by the SSRF guard

Problem: with `GITHUB_EXTRA_HOSTS` set for a GitHub Enterprise Server, registering a SKILL.md fails with `Redirect to unsafe URL blocked: https://<internal-IP>/...` when the GHES raw-content server redirects to a raw internal backend IP, even after that IP is added to `GITHUB_EXTRA_HOSTS`.

Cause: the DNS-resolved path computes `trusted_hostname = allowlist.allows_host(hostname)` and passes it to `_is_blocked_ip`, which relaxes the private-range block for allowlisted hosts. The literal-IP branches do not: in `registry/utils/url_guard.py` at `1.37.0`, `validate_url` and the three `GuardedAsyncTransport` / pinned-resolver sites call `_is_blocked_ip(hostname, allowlist, allow_private=...)` without `trusted_hostname`, so an allowlisted literal IP is blocked.

Proposed change: add an opt-in `trust_literal_hosts` flag on `_Allowlist`, set only by `_skill_allowlist()` (the operator types `GITHUB_EXTRA_HOSTS` by hand), and pass `trusted_hostname=allowlist.trusts_literal_ip(hostname.lower())` at the four literal-IP call sites (`validate_url`, `_pin_request`, `_pin_request_async`, `_validated_addresses` and its async twin). Allowlists derived from a configured base URL (for example the Entra Graph profile, covered by `test_configured_private_literal_is_not_trusted_proxy_hostname`) keep rejecting literal private IPs. Hard-denied categories (cloud metadata `169.254.169.254`, link-local, multicast, unspecified) are enforced in `_ip_denial_reason` before the relaxation, so they stay blocked even when listed.

Tests (on `cccs-main_1.37.0_aks-fixes`): a listed literal IP is trusted by `validate_url`, the sync and async pinned transports and both `_validated_addresses` variants; an unlisted literal IP is still rejected; a listed metadata IP is still rejected; a literal IP in `SSRF_ALLOWED_HOSTS` is not trusted on the proxy profile (admitted by `SSRF_ALLOWED_CIDRS` instead). The five positive tests fail without the change.

Discussion point for maintainers: upstream documents `trusted_hostname` as applying "after DNS resolution", so a hostname allowlist deliberately does not admit literal IPs on most profiles. The alternative to this change is CIDR support on the skill profile, which `_skill_allowlist` deliberately lacks. The PR should state which direction upstream prefers.

## 2. `charts/registry` has no `githubExtraHosts` value

Problem: `charts/mcpgw` exposes `app.githubExtraHosts` and reserves `GITHUB_EXTRA_HOSTS`, but `charts/registry` does not, although the registry is the service that fetches SKILL.md. Operators have to use `extraEnv`, and the name is not reserved, so it silently collides with future chart keys.

Proposed change: add `app.githubExtraHosts` to `charts/registry/values.yaml`, render `GITHUB_EXTRA_HOSTS` in `templates/deployment.yaml` when set, add the name to `reserved-env-names.txt`, and add two `extra_env_test.yaml` cases (omitted by default, rendered when set). Document that it is a separate allowlist from `ssrfAllowedHosts`/`ssrfAllowedCidrs`, which feed the proxy profile and are never consulted for skill fetches.

## 3. Generated nginx blocks forward `$scheme` instead of the terminated-TLS scheme

Problem: when an ingress or load balancer terminates TLS, nginx inside the registry pod sees `$scheme = http`. The generated per-server location blocks send `X-Forwarded-Proto $scheme` and `X-Original-URL $scheme://...`, so upstreams behind their own force-SSL redirect answer 308 to the gateway.

Proposed change: forward the template's forwarded-proto map variable (`$real_scheme` in the http+https template, `$forwarded_proto` in the http-only template) through a `_forwarded_scheme_var` attribute set in `NginxConfigService.__init__`. Includes a unit test asserting the generated block never contains `$scheme` in those two headers.

The same applies to the generated A2A agent blocks, which proxy directly to the agent backend (`proxy_pass {backend_url}/`), and a slashless `/agent/<path>` needs a 308 to the proxied route built from the same variable. On `1.37.0` the MCP blocks go through the auth server, which strips `x-forwarded-*` before calling the upstream, so reproduce the original 308 on a stock image before filing; the agent blocks have no such intermediary.

## 4. `openbao-init` needs internet access at runtime and has no image override or opt-out

Problem: the `openbao-init` Job template runs `apt-get install curl jq kubectl` inside `public.ecr.aws/docker/library/python:3.14-slim`. In air-gapped or egress-restricted clusters it cannot run, and there is no value to change the image or to skip the Job.

Proposed change: `global.openbaoInitImage` and `global.openbaoInitEnabled` values (small, applies cleanly), and a stdlib-only Python implementation of the init flow (no apt, no kubectl, no jq) using the Job service account's projected token. Includes HTTP 204 handling and an updated unit test.

## 5. SKILL.md fetch errors are not actionable

Problem: a failed SKILL.md fetch gives an empty or unhelpful reason (many httpx network errors stringify to an empty string), and a token pasted with a `Bearer ` prefix or stray whitespace is sent as-is.

Proposed change (`registry/services/skill_service.py`): `_normalize_credential` strips whitespace and a pasted `Bearer ` prefix; `_describe_request_error` builds a non-empty, credential-free reason (timeout, connect error, invalid URL, illegal header characters) and is never derived from `str(error)`, because httpx can echo header values; `_describe_http_status` explains 401/403/404 for bearer and API-key auth (GitHub Enterprise hides private repositories as 404). The timeout message names the case where public github.com is not reachable from the cluster. Includes `test_skill_service_fetch_errors.py`.

## 6. Registry ingress template supports ALB only

Problem: `charts/registry/templates/ingress.yaml` always emits `alb.ingress.kubernetes.io/*` annotations and takes only `ingress.className`. On AKS with an nginx ingress controller the ALB annotations are ignored and there is no TLS block.

Proposed change (as in `1eed68a9`): resolve the class from `ingress.className`, then `global.ingress.className`, defaulting to `alb`; emit the ALB annotations only for `alb`; for `nginx` emit `ssl-redirect`, `force-ssl-redirect`, `proxy-body-size`, and read/send timeouts; add a `spec.tls` block when `ingress.tls` (or the global equivalent) is true, with an optional `tlsSecretName`; allow extra `ingress.annotations`. Needs rebasing on the `1.37.0` template, which already handles `registrySubdomain` and path routing mode itself.

## 7. The `registry-egress-internal` NetworkPolicy isolates the registry from every other client

Problem: on a cluster that enforces NetworkPolicy (AKS with Azure CNI network policy), a stock `1.37.0` install makes the registry unreachable. The public Ingress returns 504, the login page cannot load its providers, and `mcpgw` and the agents time out calling the registry Service. The auth server can still reach port 8091, so the pods look healthy and the policy is not an obvious suspect.

Cause: `charts/registry/templates/networkpolicy-egress-internal.yaml` selects every registry pod (`podSelector: app.kubernetes.io/name: registry`, `policyTypes: [Ingress]`) but its only ingress rule allows the auth-server on the vend port (8091). Selecting a pod isolates it, so every other port and source is dropped, including the nginx listeners on 8080 and 8443. The template comment says it only gates the vend listener, which is not what the resource does. It renders by default whenever `egressAuth.enabled` or `app.gatewayGenericProxyEnabled` is true.

Proposed change: add a second ingress rule with no `from` for the public listeners (8080, 8443), and for the scrape port (`metricsScrape.port`) only when `metricsScrape.networkPolicy.enabled` is false, because its own policy covers it otherwise. The vend port stays restricted to the auth-server. Update the template comment to say that selecting the pod isolates it.

Tests: `charts/registry/tests/networkpolicy_egress_internal_test.yaml` (five cases): vend port is auth-server only, public ports have no `from`, scrape port is open only when its policy is disabled, nothing renders when `egressAuth.internalNetworkPolicy.enabled` is false.

Evidence: on dev, a pod-to-Service check timed out from `mcpgw` and from the auth-server (port 8000) before the change and returned 200 after, and `/api/auth/providers` returned 200 through the Ingress. The same chart also gates the metrics port with its own policy, so any sibling policy that selects the registry pods should be re-checked for the same isolation effect.

## 8. The egress credential encryption key can only be passed as a Helm value

Problem: `registry.egressAuth.credentialEncryptionKey` (and the auth-server's equivalent) is the root key for the per-user egress credential vault, and the docs say to keep it outside the secret store. The chart renders it from a plain value in `charts/registry/templates/secret.yaml`, and the name `EGRESS_CREDENTIAL_ENCRYPTION_KEY` is in `reserved-env-names.txt`, so `extraEnv` cannot carry it either. The only existing-secret route, `app.existingSecret`, replaces the whole generated env Secret, so an operator would have to supply every key. Teams that keep secrets in a vault end up passing the key on the Helm command line or in a values file.

Proposed change: add `egressAuth.credentialEncryptionKeyExistingSecret` (name and key) on the registry, and the same on the auth-server for the OBO cache, rendered as a `secretKeyRef` env var in the Deployment, with the same pattern mcpgw already uses for `githubPatExistingSecret`. Keep the plain value for compatibility and fail the render if both are set. Add the new fields to the reserved-name and unit-test coverage.

Workaround we run meanwhile (deployment repo): the key is synced from Azure Key Vault into a Secret, and the deploy target reads it and passes it with `--set-file` from a private temp file, refusing to run Helm when it is missing. This works but needs the deploy tooling to see the Secret, which an existing-secret field would make unnecessary.

## 9. The stack chart's OpenBao uses the `file` backend, which OpenBao 2.7 removed, and pins the image in two places

Problem: `charts/mcp-gateway-registry-stack/values.yaml` configures the standalone OpenBao with `storage "file"` and pins `quay.io/openbao/openbao:2.5.5`. OpenBao 2.7.0 removed the `file` backend (deprecated since 2.6), so bumping the image alone leaves a pod that cannot read its data. The image is also pinned separately for the `unseal` sidecar (`extraContainers`), and nothing keeps the two equal.

Proposed change: document the move in the chart README (stop OpenBao, run `bao operator migrate` from `file` to `pebbledb` or `raft`, change the storage stanza), and ship a supported default for new installs on a 2.7+ image. Derive the sidecar image from `server.image` (or add a test that fails when they differ) so a bump cannot leave them apart.

Evidence: migrated on dev from 2.5.5 to 2.7.2 on 2026-11-19. `bao operator migrate` copied all 32 keys with the source untouched, the server started on `pebbledb`, the sidecar unsealed it by itself, and the `openbao-init` job reported "already initialized" and re-created the Kubernetes auth role. Our one-off migration pod and the steps are in the deployment repo (`deploy/openbao-migrate-file-to-pebbledb.yaml`, `deploy/DEPLOYING.md`).

## 10. The auth-server image has no `hvac`, so the OBO token cache cannot start on the Helm/OpenBao path

Problem: with `auth-server.egressAuth.oboCacheEnabled: "true"` and `secretStoreBackend: "openbao"`, the auth server logs `OBO token cache init failed (No module named 'hvac'); OBO exchange runs stateless`. Nothing fails visibly: exchanges silently run uncached, so the cache looks enabled in the config but is not active. The `openbao-init` job still creates the cache's OpenBao policy and role, which makes the setup look complete.

Cause: `hvac` is declared only in the root `pyproject.toml` (installed into the registry image) and not in `auth_server/pyproject.toml`, which is what the auth-server image installs. `registry/secrets/factory.py` imports `hvac` for the OpenBao backend, and the auth server uses that factory for the cache. Reproduced by importing `hvac` in the unmodified upstream `auth-server:1.37.0` image (`ModuleNotFoundError`).

Proposed change: add `hvac>=2.0.0` to `auth_server/pyproject.toml` (and its lock file) so the published image includes it. Optionally log the failure at startup as a warning that names the config flag, and add a test or CI check that imports every optional secret-store backend inside the built auth-server image.

Workaround we run (fork): `build_auth.sh` downloads `hvac==2.4.0` as a pinned, sha256-checked pure-Python wheel and the overlay Dockerfile unpacks it into the app's venv (its only requirement, `requests`, is already present). Verified by importing it in the built image; the cache then starts.

## Process notes

- Open an issue first for items 1, 3 and 6 (behavior or design choices), and a direct PR for items 2, 4 and 5.
- Reproduce items 1 and 3 on a stock `1.37.0` image before filing, so the report does not depend on our patches.
- Remove a section here when upstream ships the fix, or note the release that contains it.

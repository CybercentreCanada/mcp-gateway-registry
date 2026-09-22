# cccs-main_1.29.0 port audit

This note records the results of auditing every `charts/`-touching commit authored by
our team (`CyberKianZ`, `cccs-is`) between `cccs-main` and the tip of our customization
chain, against the `cccs-main_1.29.0` branch (pure upstream `1.29.0` tag plus the
deployment fixes we've re-applied). Scope: deployment-relevant chart changes only —
scope/RBAC content and application features are explicitly out of scope here and
called out separately below.

Fixes from this audit landed in commit `1dd8d684` ("fix(charts): port registrySubdomain
support missed in earlier port").

## Gap found and fixed

The registry ingress hostname computation (`charts/registry/templates/ingress.yaml`)
already respected `global.ingress.registrySubdomain`, but three other templates that
independently compute the registry's public URL still hardcoded `mcpregistry`:

- `charts/auth-server/templates/secret.yaml` (`AUTH_SERVER_EXTERNAL_URL`)
- `charts/registry/templates/secret.yaml` (`REGISTRY_EXTERNAL_URL`,
  `GATEWAY_ADDITIONAL_SERVER_NAMES`)
- `charts/mcp-gateway-registry-stack/templates/oauth-provider-secret.yaml`
  (`KEYCLOAK_EXTERNAL_URL`)

This only matters for environments where `registrySubdomain` is overridden away from
the default (e.g. staging's `mcpregistry-stg`) — for `pb-dev`, where the subdomain
happens to equal the hardcoded default, this gap was invisible. It would have caused
a hostname mismatch (ingress serving `mcpregistry-stg.<domain>`, but the app computing
OAuth/redirect URLs against `mcpregistry.<domain>`) had we deployed this branch to
staging as-is.

Also cleaned up: `charts/mcp-gateway-registry-stack/values.yaml`'s `registry.ingress`
block set `ingressClassName: alb`, a key the registry subchart template never reads
(it reads `.Values.ingress.className`) — silently ignored. Replaced with the correct
`className: ""` default.

Added a handful of documented chart-level defaults (`tlsSecretName`,
`registrySubdomain`, `pythonImage`, `pipIndexUrl`) purely for discoverability — no
functional change, since our environment values files already set these correctly.

## Explicitly reviewed, intentionally NOT ported

- **`mongodb.operator.enabled` gate** on the `MongoDBCommunity` CR
  (`charts/mcp-gateway-registry-stack/templates/mongodb-cluster.yaml`) and its
  `values.yaml` default (`true`). In our current fork, this key toggles between the
  Bitnami `mongodb` subchart and the Community Operator subchart, both of which are
  (or were) available as Chart.yaml dependencies. `cccs-main_1.29.0` uses upstream's
  real `mongodb-kubernetes` operator dependency directly, and we deliberately keep
  `mongodb.operator.enabled: false` in our own values as an inert rollback marker.
  Porting this gate verbatim would make the CR never render on this branch. Skipped.

- **RBAC / scope-seed content** — `charts/mongodb-configure/templates/configmap.yaml`
  and the `publishSkillEnabled` value that gates it. This is scope-content, not
  deployment infrastructure, and out of scope for this audit.

- **`ingressRelayAllowedServers`** (`charts/registry/values.yaml`, from the
  `TRI-408__accessing_RAG_MCP_using_ai_registry` feature). An application feature
  (relays the caller's bearer token to specific upstream MCP servers) tied to
  registry application code that isn't present in the vanilla `1.29.0` image we're
  pinned to. Setting the chart value alone would be a no-op. Flagged for awareness,
  not ported.

- New upstream-only additions with no CCCS equivalent to port: embeddings IdP auth,
  MCP token TTL config, quarantine fail-closed, Entra `scopeFormat`/
  `applicationIdUri`, caller-supplied asset id. These postdate our fork point and
  require no action.

## Bonus finding (outside this audit's mandate — needs a team decision)

While verifying the fix above, we found that `charts/keycloak-configure/templates/secret.yaml`
and `charts/mcpgw/templates/secret.yaml` **also** hardcode `mcpregistry.<domain>` —
in **our own fork**, not just in vanilla. This is a pre-existing gap that was never
fixed anywhere in our customization chain, so it's not something this port missed;
it's a latent bug in both branches. Same trigger condition: only matters when
`registrySubdomain` differs from the default (staging).

Not fixed as part of this audit since it's outside "port what we already fixed" —
raise separately, and consider fixing in both branches (this one and our actual
fork) rather than just here.

## Follow-up: SKILL.md registration blocked by SSRF guard (found during DEV UI testing)

Admins hit `Failed to parse SKILL.md: Redirect to unsafe URL blocked: https://<internal-IP>/...`
when registering a skill backed by our internal GHES. Root cause, and what it is
NOT:

- **Not** the `ssrfAllowedHosts` / `ssrfAllowedCidrs` values (`10.0.0.0/8,172.0.0.0/8`
  in our env files) — those feed `registry.utils.url_guard`'s *proxy* allowlist
  (`_proxy_allowlist`), used for server/agent proxy targets. They have no effect on
  skill fetching at all.
- **Is** `registry.utils.url_guard._skill_allowlist()`, which `skill_service.py`
  uses for the SKILL.md SSRF check. It only consults `GITHUB_EXTRA_HOSTS`
  (exact hostnames, deliberately no CIDR support) and is fully separate from the
  proxy allowlist above.
- `GITHUB_EXTRA_HOSTS` for the registry pod is already set correctly (via a manual
  `registry.extraEnv` entry in our values files, since `charts/registry` had no
  native key for it — fixed below) to our GHES hostnames. That's enough for the
  *initial* request to succeed.
- The failure is on the **redirect**: our GHES's raw-content server responds to the
  initial (trusted-hostname) request with an HTTP redirect straight to an internal
  backend IP. `skill_service.py` re-validates the final URL after redirects
  (`follow_redirects=True`, then a second `_is_safe_url` check on `response.url`).
  That backend IP is stable, and we confirmed it's fine to allowlist (see below) —
  but after adding it to `GITHUB_EXTRA_HOSTS`, the redirect was **still** blocked.
  That pointed at a second, deeper bug (see "Actual root cause" below).

Fixed layer 1 (chart hygiene, no security-posture change): added a native
`app.githubExtraHosts` value + `GITHUB_EXTRA_HOSTS` env wiring to
`charts/registry` (values.yaml, deployment.yaml, reserved-env-names.txt, plus
two new `extra_env_test.yaml` cases) — `charts/mcpgw` already had this, registry
did not. See commit `6d0c6844`.

### Actual root cause: `url_guard.py` never relaxed literal-IP hosts against the allowlist

`registry/utils/url_guard.py` has two code paths that decide whether a target IP is
blocked: the DNS-resolved-hostname path (`_validate_resolved_ips`), and the
literal-IP-in-URL path (used directly when a URL's host is already an IP, which is
exactly what happens after GHES's redirect). The DNS path correctly computes
`trusted_hostname = allowlist.allows_host(hostname)` and passes it to
`_is_blocked_ip()`, which relaxes the private-IP block for allowlisted hosts. The
literal-IP path — in `validate_url()`, and in `GuardedAsyncTransport`'s
`_pin_request()` / `_pin_request_async()` — called `_is_blocked_ip()` **without**
`trusted_hostname`, so an allowlisted literal IP was never relaxed. Adding
`172.20.73.8` to `GITHUB_EXTRA_HOSTS` therefore had no effect on the redirect
check specifically. Hard-denied categories (cloud metadata `169.254.169.254`,
loopback, link-local, multicast, reserved, unspecified) are enforced independently
of `trusted_hostname` and remain blocked even for allowlisted hosts — this fix
does not weaken that.

Fixed (application code, all 3 call sites): pass `trusted_hostname =
allowlist.allows_host(hostname.lower())` into `_is_blocked_ip()` in all three
literal-IP branches. See commit `26f1cd3e`. Added 5 regression tests to
`tests/unit/utils/test_url_guard.py` covering the allowed and still-blocked cases
(allowlisted literal IP allowed, non-allowlisted private IP still blocked, hard-denied
metadata IP still blocked even if allowlisted). Full suite: 258 `url_guard` tests +
47 skill-service tests pass.

`172.20.73.8` is included in `GITHUB_EXTRA_HOSTS` for `pb-dev` only (staging/prod
values files intentionally omit it for now — they're still on older, unrelated
custom image tags and haven't hit this issue). **Still open**: we have not yet
confirmed with whoever manages our GHES instance whether this backend IP is
stable long-term; a bare IP is fragile if that backend ever moves. Follow up on
this before relying on it outside of `pb-dev` testing.

**Deployed and verified in `pb-dev`**: built `registry:1.29.0-cccs-ssrf-fix1`
(chart fix + `url_guard.py` fix only) and, after picking up `fe59c5f8` — a
teammate's unrelated nginx `X-Forwarded-Proto`/`X-Original-URL` redirect fix that
landed on the same branch — rebuilt as `registry:1.29.0-cccs-combined1` (both
fixes together, using Docker layer caching so the rebuild only reran the
`uv sync`/frontend layers, not the ~1GB nginx-extras install). Retested by an
admin in the DEV UI: skill registration via a SKILL.md URL now succeeds when
using "global credentials" (the registry's PAT) as the source authentication
mode. Issue resolved.

Separately investigated: `publishSkillEnabled` (recalled from our actual fork, not
found here) does not exist as a toggle in this vanilla-1.29.0-based branch at all —
`charts/mongodb-configure/templates/configmap.yaml` unconditionally grants
`"publish_skill": ["all"]` in the `registry-admins`/unrestricted scope seed here,
no gate. Admins already have the permission needed to register skills, so this
does not need to be ported for the SSRF fix above to take effect; this remains the
RBAC/scope-content item already flagged as out of scope in this audit.

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

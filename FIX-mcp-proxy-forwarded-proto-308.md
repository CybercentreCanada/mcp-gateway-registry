# Fix: MCP proxy forwarded a `http` scheme, causing upstream 308 redirects

## Summary

An MCP server registered with an On-Behalf-Of (OBO) egress mode returned an
opaque `HTTP/2 308` to the MCP client when accessed through the registry URL.
The root cause was that the dynamically generated nginx location block for MCP
servers forwarded nginx's own `$scheme` (which is `http` when TLS is terminated
at an upstream ingress/ALB) instead of the template's forwarded-proto map
variable. A downstream MCP upstream that runs behind its own
`force-ssl-redirect` ingress therefore saw `X-Forwarded-Proto: http` and answered
with a `308 Permanent Redirect`, which the gateway relayed with the `Location`
header stripped.

## Symptom

- Connecting to an OBO server through the gateway (`https://<gateway>/<server>/mcp`)
  returned a bare `HTTP/2 308` with no usable `Location` header.
- The upstream MCP server was healthy and served `/mcp` directly (a direct,
  unauthenticated call returned an auth error, not a redirect).
- Only servers whose traffic actually reaches the upstream surfaced it (OBO, after
  the token exchange). Token-less 3LO servers answer `initialize`/`tools/list`
  locally and never forward upstream, so they did not show the 308.

## Root cause

In a Kubernetes deployment the ingress terminates TLS and forwards to the gateway
pod over plaintext HTTP, so inside the gateway's nginx `$scheme` is `http`.

The nginx templates define a scheme map that recovers the real client scheme from
the ingress `X-Forwarded-Proto`:

- `docker/nginx_rev_proxy_http_only.conf` → `$forwarded_proto`
- `docker/nginx_rev_proxy_http_and_https.conf` → `$real_scheme`

Every hand-written location block uses that map variable. However, the
dynamically generated MCP location block in
`registry/core/nginx_service.py` hardcoded `$scheme`:

```nginx
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Original-URL $scheme://$host$request_uri;
```

Because `$scheme` was `http`, the gateway stamped `X-Forwarded-Proto: http` on the
request. The auth-server proxy hop (`_forward_headers` in `auth_server/server.py`)
relays that header to the upstream. An upstream fronted by its own ingress with
`force-ssl-redirect` then answered with a `308` to HTTPS. The gateway's
`mcp_proxy` relays the upstream status verbatim but only forwards an allowlisted
set of response headers (`mcp-session-id`, `x-mcp-session-id`, `www-authenticate`,
`retry-after`) — `Location` is intentionally dropped — so the client received a
`308` with no `Location`, a dead-end redirect.

## Fix

Make the generated MCP location block use the template's forwarded-proto map
variable instead of `$scheme`, selected by the deployment mode that
`NginxConfigService` already determines when it picks the template.

`registry/core/nginx_service.py`:

- In `__init__`, set `self._forwarded_scheme_var` alongside the template choice:
  - http+https template → `$real_scheme`
  - http-only template → `$forwarded_proto`
- In `_create_location_block`, emit that variable for both headers:

```nginx
proxy_set_header X-Forwarded-Proto {self._forwarded_scheme_var};
proxy_set_header X-Original-URL {self._forwarded_scheme_var}://$host$request_uri;
```

With the fix the gateway forwards `X-Forwarded-Proto: https` (honoring the
terminated TLS), the upstream no longer force-ssl-redirects, and no 308 is
generated to relay.

## Tests

`tests/unit/core/test_nginx_service.py`:

- `test_nginx_service_init_http_only` / `test_nginx_service_init_http_and_https`
  assert `_forwarded_scheme_var` is `$forwarded_proto` / `$real_scheme`.
- `test_location_block_forwards_proto_from_map_not_scheme` asserts the generated
  MCP block forwards the map variable for `X-Forwarded-Proto` and `X-Original-URL`
  and no longer contains the `$scheme` literal in either header.

## Deploying the fix

The nginx config is regenerated on startup and on server add/edit/toggle. After
upgrading, either restart the registry pod or re-save/toggle the affected server
so the running nginx config picks up the corrected `X-Forwarded-Proto` directive.

## Related latent issue (not changed here)

`mcp_proxy` relays an upstream 3xx status while stripping the `Location` header
(the response-header allowlist), producing a Location-less redirect. The scheme
fix removes the trigger (no redirect is generated), but making the internal proxy
hop resolve trailing-slash/normalization redirects itself — using the
SSRF-guarded client — would make the gateway resilient to any upstream that
normalizes with a redirect.

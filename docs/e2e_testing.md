# Rate Limiting: End-to-End Test Guide (issue #295)

A hands-on sequence to verify application-level rate limiting on a running gateway. It creates two dedicated Keycloak test principals (a human user `rl-test-user` and an M2M client `rl-test-m2m`), then walks from backwards-compatibility (nothing configured) through group (caller) limits, response headers, OTel metrics + logs, the floor safeguards, a per-agent (M2M `client_id`) limit, and finally target limits (per MCP server / per tool).

For failure-mode and correctness-invariant tests (concurrency/atomicity, window reset + `Retry-After`, deny-does-not-consume, caller-type classification, membership-vs-authz, admin bypass, data-plane-only scope, enable/disable, and fail-open/fail-closed), see the companion [e2e_testing_advanced.md](e2e_testing_advanced.md). Run it after Step 0 here.

> **Before you start, you MUST set and export these environment variables** (no defaults; the commands fail loudly if any is unset, so no URL or password is ever hardcoded in this repo):
>
> ```bash
> export KC_URL=...                 # Keycloak base URL reachable from the host
> export REG=...                    # gateway base URL (e.g. http://localhost)
> export RL_TEST_USER_PASSWORD=...  # password for the rl-test-user test account
> ```
>
> Plus the Keycloak admin credentials from your environment: `KEYCLOAK_ADMIN`, `KEYCLOAK_ADMIN_PASSWORD`, `KEYCLOAK_REALM` (e.g. `set -a; source .env; set +a`). See [Conventions](#conventions) for the full guard block.

## Conventions

Run from the repo root. All examples assume an admin token file at `./.token`.

**Required environment variables (the guide fails loudly if any is unset).** URLs and the test password are deliberately NOT hardcoded and have NO defaults, so a sensitive URL or password can never be accidentally committed from these docs. Set them in your shell for the session:

```bash
# --- Required inputs: no defaults, never commit these values ---
export KC_URL=...                 # Keycloak base URL reachable FROM THE HOST
                                  #   (e.g. http://localhost:8080 locally; a real
                                  #   hostname for a remote Keycloak). NOTE: the
                                  #   in-container name (http://keycloak:8080) is
                                  #   NOT reachable from the host.
export REG=...                    # gateway base URL (e.g. http://localhost)
export RL_TEST_USER_PASSWORD=...  # password for the rl-test-user test account

# Fail loudly if any required input is missing.
: "${KC_URL:?Set KC_URL (Keycloak base URL reachable from the host)}"
: "${REG:?Set REG (gateway base URL)}"
: "${RL_TEST_USER_PASSWORD:?Set RL_TEST_USER_PASSWORD (test account password)}"

# --- Derived / fixed ---
export TOK=.token                          # admin token file
export SRV="$REG/airegistry-tools/mcp"     # a confirmed data-plane MCP endpoint
```

Two important reminders before you start:

- **`RATE_LIMITING_ENABLED=true`** must be set in `.env` (and the containers rebuilt/restarted) or everything is a no-op.
- **`registry_management.py` global flags go BEFORE the subcommand**: `... --token-file .token --registry-url http://localhost <subcommand> ...`. The test script `call_mcp_tool.py` is flat (flags anywhere).
- **Admin is bypassed** on caller limits, and caller limits only apply to **data-plane** (MCP/A2A) calls, never `/api/*`. So to see a caller limit bite, test with a **non-admin** user or an M2M client, calling an **MCP server**.

### CLI vs UI

Every step below uses the CLI/API, but the same operations are available in the UI (admin only):

- **Settings → IAM → Rate Limits** — create / edit / enable-disable / delete rate-limit definitions (group and target).
- **Settings → IAM → Users** and **Settings → IAM → M2M Accounts** — a "Rate-limit Groups" column shows each user's / client's membership and lets you edit it via a multi-select of the defined groups.

You can drive the whole sequence from the UI instead of the CLI; the CLI is used here because it is scriptable and copy-pasteable.

---

## Step 0 — Create the test principals (`rl-test-user` and `rl-test-m2m`)

The whole suite runs against two dedicated Keycloak principals so you never test as admin (admins bypass caller limits). Create both with direct Keycloak admin-API `curl` calls.

This uses the required env vars you exported in Conventions (`KC_URL`, `REG`, `RL_TEST_USER_PASSWORD`) plus the Keycloak admin credentials (`KEYCLOAK_ADMIN`, `KEYCLOAK_ADMIN_PASSWORD`, `KEYCLOAK_REALM`), which come from the environment (e.g. `set -a; source .env; set +a`). Every value fails loudly if unset.

```bash
# Keycloak admin credentials + realm come from the environment (e.g. `.env`):
#   set -a; source .env; set +a   # if not already exported
export REALM="${KEYCLOAK_REALM:-mcp-gateway}"

# Fail loudly if any required value is missing (URLs/password have no defaults).
: "${KC_URL:?Set KC_URL (Keycloak base URL reachable from the host)}"
: "${RL_TEST_USER_PASSWORD:?Set RL_TEST_USER_PASSWORD (test account password)}"
: "${KEYCLOAK_ADMIN:?KEYCLOAK_ADMIN not set (expected from .env)}"
: "${KEYCLOAK_ADMIN_PASSWORD:?KEYCLOAK_ADMIN_PASSWORD not set (expected from .env)}"

# Admin API token (master realm, admin-cli public client)
export ADMIN_TOKEN=$(curl -s -X POST "$KC_URL/realms/master/protocol/openid-connect/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=password" -d "client_id=admin-cli" \
  -d "username=$KEYCLOAK_ADMIN" --data-urlencode "password=$KEYCLOAK_ADMIN_PASSWORD" | jq -r '.access_token')
echo "admin token length: ${#ADMIN_TOKEN}"    # non-zero => good
```

### 0a. Human user `rl-test-user` (password grant)

```bash
# Guard again in case this block is run on its own (set in the setup step above).
: "${RL_TEST_USER_PASSWORD:?Set RL_TEST_USER_PASSWORD before running (e.g. export RL_TEST_USER_PASSWORD='<a-strong-password>')}"

# Delete any pre-existing rl-test-user first for a clean slate. If it does not
# exist the lookup returns null and the DELETE is skipped/404s -- that is fine.
EXISTING_USER_ID=$(curl -s -H "Authorization: Bearer $ADMIN_TOKEN" \
  "$KC_URL/admin/realms/$REALM/users?username=rl-test-user" | jq -r '.[0].id // empty')
[ -n "$EXISTING_USER_ID" ] && curl -s -o /dev/null -w "delete_existing_user=%{http_code}\n" \
  -X DELETE "$KC_URL/admin/realms/$REALM/users/$EXISTING_USER_ID" \
  -H "Authorization: Bearer $ADMIN_TOKEN"       # -> 204 (or skipped if none existed)

# Create the user with the password from $RL_TEST_USER_PASSWORD (jq injects it so
# special characters are JSON-escaped safely).
curl -s -o /dev/null -w "create_user=%{http_code}\n" -X POST "$KC_URL/admin/realms/$REALM/users" \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d "$(jq -n --arg pw "$RL_TEST_USER_PASSWORD" '{
    username:"rl-test-user",
    email:"rl-test-user@example.com",
    firstName:"RL", lastName:"TestUser",
    enabled:true, emailVerified:true,
    credentials:[{type:"password", value:$pw, temporary:false}]
  }')"                                          # -> 201 (or 409 if it already exists)

# Resolve its id
export RL_USER_ID=$(curl -s -H "Authorization: Bearer $ADMIN_TOKEN" \
  "$KC_URL/admin/realms/$REALM/users?username=rl-test-user" | jq -r '.[0].id')
echo "rl-test-user id: $RL_USER_ID"

# (Re)set the password if the user already existed.
curl -s -o /dev/null -w "reset_password=%{http_code}\n" -X PUT \
  "$KC_URL/admin/realms/$REALM/users/$RL_USER_ID/reset-password" \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d "$(jq -n --arg pw "$RL_TEST_USER_PASSWORD" '{type:"password", value:$pw, temporary:false}')"  # -> 204

# Put the user in a group that grants MCP-server access so it can reach the data
# plane (otherwise calls are denied with a genuine 403, not a throttle). The group
# must be mapped to a scope in mcp_scopes_default that grants the target server.
# read-all-register-new grants server:"*" (all servers) in this deployment; pick
# whichever group your deployment maps to the servers you are testing.
export ACCESS_GROUP="${ACCESS_GROUP:-read-all-register-new}"
export GID=$(curl -s -H "Authorization: Bearer $ADMIN_TOKEN" \
  "$KC_URL/admin/realms/$REALM/groups" | jq -r ".[] | select(.name==\"$ACCESS_GROUP\") | .id")
curl -s -o /dev/null -w "join_group=%{http_code}\n" -X PUT \
  "$KC_URL/admin/realms/$REALM/users/$RL_USER_ID/groups/$GID" \
  -H "Authorization: Bearer $ADMIN_TOKEN"       # -> 204
```

Get a token for `rl-test-user` via the password (direct access) grant on the `mcp-gateway-web` client, and write it to a token file the test script reads:

```bash
# The web client is confidential, so its secret is needed for the password grant.
export WEB_UUID=$(curl -s -H "Authorization: Bearer $ADMIN_TOKEN" \
  "$KC_URL/admin/realms/$REALM/clients?clientId=mcp-gateway-web" | jq -r '.[0].id')
export WEB_SECRET=$(curl -s -H "Authorization: Bearer $ADMIN_TOKEN" \
  "$KC_URL/admin/realms/$REALM/clients/$WEB_UUID/client-secret" | jq -r '.value')

# Uses $RL_TEST_USER_PASSWORD (guarded above). --data-urlencode keeps special
# characters intact and keeps the secret off the visible command line.
curl -s -X POST "$KC_URL/realms/$REALM/protocol/openid-connect/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=password" -d "client_id=mcp-gateway-web" -d "client_secret=$WEB_SECRET" \
  -d "username=rl-test-user" --data-urlencode "password=$RL_TEST_USER_PASSWORD" \
  -d "scope=openid email profile" \
  | jq -r '.access_token' > .token-rl-test-user
echo "user token bytes: $(wc -c < .token-rl-test-user)"    # non-trivial => good
```

`call_mcp_tool.py` accepts a bare JWT in the token file, so `.token-rl-test-user` is ready to use with `--token-file .token-rl-test-user`.

### 0b. M2M client `rl-test-m2m` (client_credentials grant)

```bash
# Delete any pre-existing rl-test-m2m client first for a clean slate (deleting the
# client also removes its service-account user). If none exists the lookup returns
# null and the DELETE is skipped -- that is fine.
EXISTING_M2M_UUID=$(curl -s -H "Authorization: Bearer $ADMIN_TOKEN" \
  "$KC_URL/admin/realms/$REALM/clients?clientId=rl-test-m2m" | jq -r '.[0].id // empty')
[ -n "$EXISTING_M2M_UUID" ] && curl -s -o /dev/null -w "delete_existing_m2m=%{http_code}\n" \
  -X DELETE "$KC_URL/admin/realms/$REALM/clients/$EXISTING_M2M_UUID" \
  -H "Authorization: Bearer $ADMIN_TOKEN"       # -> 204 (or skipped if none existed)

# Create a confidential client with a service account (client_credentials only)
curl -s -o /dev/null -w "create_m2m=%{http_code}\n" -X POST "$KC_URL/admin/realms/$REALM/clients" \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{
    "clientId":"rl-test-m2m",
    "enabled":true,
    "publicClient":false,
    "serviceAccountsEnabled":true,
    "standardFlowEnabled":false,
    "directAccessGrantsEnabled":false,
    "protocol":"openid-connect"
  }'                                            # -> 201 (or 409 if it already exists)

# Resolve the client uuid + secret; capture the client_id string for memberships
export M2M_UUID=$(curl -s -H "Authorization: Bearer $ADMIN_TOKEN" \
  "$KC_URL/admin/realms/$REALM/clients?clientId=rl-test-m2m" | jq -r '.[0].id')
export M2M_SECRET=$(curl -s -H "Authorization: Bearer $ADMIN_TOKEN" \
  "$KC_URL/admin/realms/$REALM/clients/$M2M_UUID/client-secret" | jq -r '.value')
export M2M_CLIENT_ID=rl-test-m2m
echo "m2m secret bytes: ${#M2M_SECRET}"

# The service account needs MCP-server access too. Its user is named
# service-account-rl-test-m2m; add it to the same access group ($GID from 0a,
# e.g. read-all-register-new).
export M2M_SA_ID=$(curl -s -H "Authorization: Bearer $ADMIN_TOKEN" \
  "$KC_URL/admin/realms/$REALM/users?username=service-account-rl-test-m2m" | jq -r '.[0].id')
curl -s -o /dev/null -w "m2m_join_group=%{http_code}\n" -X PUT \
  "$KC_URL/admin/realms/$REALM/users/$M2M_SA_ID/groups/$GID" \
  -H "Authorization: Bearer $ADMIN_TOKEN"       # -> 204
```

Get a token for the M2M client and write it to its token file:

```bash
curl -s -X POST "$KC_URL/realms/$REALM/protocol/openid-connect/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials" -d "client_id=rl-test-m2m" -d "client_secret=$M2M_SECRET" \
  -d "scope=openid email profile" \
  | jq -r '.access_token' > .token-rl-test-m2m
echo "m2m token bytes: $(wc -c < .token-rl-test-m2m)"
```

> Tokens expire (default ~5 min). Re-run the relevant token `curl` to refresh `.token-rl-test-user` / `.token-rl-test-m2m` whenever you get a `401 Authentication required`.

From here on the guide uses:

- `--token-file .token-rl-test-user` for user (caller_type=user) tests,
- `--token-file .token-rl-test-m2m` and `--subject <M2M_CLIENT_ID>` (i.e. `rl-test-m2m`) for M2M (caller_type=agent) tests.

---

## Step 1 — Backwards compatibility (no config, no groups)

With no definitions and no memberships, every call must pass. Confirm the feature is inert until used.

```bash
# A burst of 100 calls should all be 200 (0 throttled).
uv run python tests/scripts/call_mcp_tool.py \
  --server-url "$SRV" --tool healthcheck --tool-args '{}' \
  --token-file "$TOK" --registry-url "$REG" --count 100
```

Expected: `Done: 100 call(s) ..., 0 throttled (429), 100 succeeded/other`. If you see 429s here, something is already configured — list and clear it:

```bash
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" rate-limit-list
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" rate-limit-member-list
```

---

## Step 2 — Create two groups and verify a per-minute limit

Create a group with a **25 req/min** user limit (above the 20/min user floor), plus a second group with a wider daily volume cap. A definition below the floor on a short window is rejected (see Step 6).

```bash
# Group A: 25/min burst for users
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" \
  rate-limit-set --axis caller --entity-type group --name rl-test \
  --user-max-requests 25 --window-seconds 60

# Group A also gets a daily volume cap (long window, floor does not apply)
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" \
  rate-limit-set --axis caller --entity-type group --name rl-test \
  --user-max-requests 1000 --window-seconds 86400

# Group B: a second, independent group (e.g. a tighter power-user tier)
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" \
  rate-limit-set --axis caller --entity-type group --name rl-test-strict \
  --user-max-requests 30 --window-seconds 60

uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" rate-limit-list
```

Map the **`rl-test-user`** principal (from Step 0) into `rl-test` (do NOT use admin — admins bypass caller limits), then drive calls **as that user** with its token file:

```bash
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" \
  rate-limit-member-set --subject-type user --subject rl-test-user --groups rl-test

# As rl-test-user (their own token from Step 0a), a 30-call burst against the 25/min limit:
uv run python tests/scripts/call_mcp_tool.py \
  --server-url "$SRV" --tool healthcheck --tool-args '{}' \
  --token-file .token-rl-test-user --registry-url "$REG" --count 30
```

Expected: 25× `200`, then `429 (rate limited)`. The per-minute (burst) gate is the tightest, so it trips first.

### Multiple limits at once (burst + volume)

Because `rl-test` now has both a 25/min and a 1000/day limit, both are enforced as independent gates. Within a minute the 25/min gate governs; the daily counter only advances on **allowed** requests (a burst-denied request does not consume the daily budget). To watch the daily gate, lower it temporarily (e.g. `--user-max-requests 20 --window-seconds 86400` sits below neither floor since 86400 > 60s) and drive > 20 allowed calls across minutes.

---

## Step 3 — Verify the 429 response headers

The script prints the rate-limit headers on each line; to see them raw, use curl with `X-Authorization` (the header nginx's `auth_request` reads):

```bash
# .token-rl-test-user holds a bare JWT (from Step 0a), so read it directly:
TOKEN=$(cat .token-rl-test-user)

# Fire enough to trip, then inspect the throttled response headers:
for i in $(seq 1 30); do
  curl -s -D - -o /dev/null -X POST "$SRV" \
    -H "X-Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"healthcheck","arguments":{}}}' \
  | grep -iE "^HTTP|^x-ratelimit|^retry-after"
done | sort | uniq -c
```

On a throttled request you should see:

```
HTTP/1.1 429 Too Many Requests
X-RateLimit-Limit: 25
X-RateLimit-Remaining: 0
X-RateLimit-Reset: <epoch>
Retry-After: <seconds>
```

> Note: internally the auth-server `/validate` subrequest returns a **403** with an `X-RateLimit-Throttled: 1` marker, because nginx `auth_request` forwards only 401/403 (a 429 there would become a 500). nginx's `@forbidden_error` location detects the marker and rewrites the response into the 429 shown above. If you ever see a **500** on a throttled call, the nginx config was rendered from a stale template that lacks the `$rl_*` captures / `@forbidden_error` branch: regenerate it (restart the registry) and confirm `grep rl_throttled /etc/nginx/conf.d/nginx_rev_proxy.conf` returns matches.

---

## Step 4 — Verify OTel metrics (Prometheus)

Enforcement runs in the **auth-server**, which exports Prometheus metrics on `:9464`; the in-cluster Prometheus (job `mcp-auth-server`) scrapes it and is exposed at `http://localhost:9090`.

**Scrape the auth-server exporter directly:**

```bash
docker exec mcp-gateway-registry-auth-server-1 \
  sh -c 'curl -s localhost:9464/metrics' | grep -E "mcpgw_rate_limit"
```

**Or query Prometheus** (browser: `http://localhost:9090/graph`, or curl):

```bash
# how many throttles, by axis/entity_type/window
curl -s 'http://localhost:9090/api/v1/query?query=mcpgw_rate_limit_throttled_total' | jq '.data.result'

# total gate checks (allow vs deny)
curl -s 'http://localhost:9090/api/v1/query?query=mcpgw_rate_limit_checks_total' | jq '.data.result'

# backend op latency (histogram) — the per-op counter-store round trip
curl -s 'http://localhost:9090/api/v1/query?query=mcpgw_rate_limit_backend_duration_milliseconds_count' | jq '.data.result'

# fail-open events (should be 0 in a healthy run)
curl -s 'http://localhost:9090/api/v1/query?query=mcpgw_rate_limit_errors_total' | jq '.data.result'
```

You should see `mcpgw_rate_limit_throttled_total{axis="clr",entity_type="group",window_seconds="60"}` increment by the number of 429s from Steps 2-3, and `..._checks_total` increment for every gate evaluation.

---

## Step 5 — Verify logs / trace messages

The limiter logs a WARNING on each throttle (with the bounded, non-PII fields) and the limiter initializes with an INFO line on first use:

```bash
# Throttle events
docker logs mcp-gateway-registry-auth-server-1 --since 10m 2>&1 | grep "rate-limit throttled"
# -> ... rate-limit throttled: axis=clr entity_type=group name=<user> limit=25/60s

# Limiter initialization (backend, fail_open, cache TTL, timeout)
docker logs mcp-gateway-registry-auth-server-1 2>&1 | grep -i "Rate limiter initialized"

# Fail-open events (backend errors), if any
docker logs mcp-gateway-registry-auth-server-1 --since 10m 2>&1 | grep "rate-limit backend error"
```

For distributed traces, the auth-server is OTel-instrumented; if an OTLP endpoint is configured (`OTEL_EXPORTER_OTLP_ENDPOINT`), the `/validate` spans carry the request attributes. The rate-limit decision is visible in the WARNING logs above regardless.

---

## Step 6 — Verify the floor safeguards

Confirm a too-tight caller limit is rejected at config time (this is what prevents the earlier admin lockout):

```bash
# User number below the 20/min floor on a 60s window -> 400 rejected
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" \
  rate-limit-set --axis caller --entity-type group --name rl-floor-test \
  --user-max-requests 3 --window-seconds 60
# -> "user_max_requests 3 is below the user floor of 20/min ..."

# Agent number below the 10/min floor -> 400 rejected
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" \
  rate-limit-set --axis caller --entity-type group --name rl-floor-test \
  --agent-max-requests 2 --window-seconds 60
# -> "agent_max_requests 2 is below the agent floor of 10/min ..."
```

Also confirm the **admin is never throttled** (caller-axis) and the **dashboard/API is never throttled**: while a caller limit is active on your user, the registry UI and `/api/*` calls keep working — caller limits apply to data-plane MCP/A2A calls only.

---

## Step 7 — Per-agent (M2M `client_id`) limit

Use the **`rl-test-m2m`** client from Step 0b: map its `client_id` into a group with an agent limit, then drive calls with its token and confirm the **agent** number is enforced.

**1. Map the `rl-test-m2m` client_id into a group with an agent limit** (>= the 10/min agent floor):

```bash
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" \
  rate-limit-set --axis caller --entity-type group --name rl-agents \
  --agent-max-requests 10 --window-seconds 60

# subject-type=client, subject is the client_id string (rl-test-m2m)
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" \
  rate-limit-member-set --subject-type client --subject rl-test-m2m --groups rl-agents
```

**2. Drive calls as that agent and confirm the agent limit trips:**

```bash
# Refresh the M2M token first if it may have expired (see Step 0b), then:
uv run python tests/scripts/call_mcp_tool.py \
  --server-url "$SRV" --tool healthcheck --tool-args '{}' \
  --token-file .token-rl-test-m2m --registry-url "$REG" --count 15
```

Expected: 10× `200`, then `429`. The limiter keys the counter on the agent's `client_id`, picks the group's **agent** number (10). Confirm attribution in the log — a throttle line shows `caller_type=agent` with the client_id:

```bash
docker logs mcp-gateway-registry-auth-server-1 --since 2m 2>&1 | grep "rate-limit throttled" | tail -2
# -> ... axis=clr entity_type=group name=rl-test-m2m limit=10/60s caller_type=agent caller_username= caller_client_id=rl-test-m2m
```

---

## Step 8 — Target limits: per MCP server and per tool

The **target axis** caps aggregate load against an entity regardless of caller (it applies even to admin, so it protects a weak backend). A target limit is defined on `--entity-type mcp_server` (or `a2a_agent`) with a `--max-requests` number. Because target gates are not caller-scoped, `rl-test-user`, `rl-test-m2m`, and admin all draw down the same counter.

**1. Per-MCP-server limit** — cap total calls to the `airegistry-tools` server at 5/min:

```bash
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" \
  rate-limit-set --axis target --entity-type mcp_server \
  --name airegistry-tools --max-requests 5 --window-seconds 60
```

Wait ~30s for the definitions cache (`RATE_LIMIT_DEFINITIONS_CACHE_TTL_SECONDS`) to pick it up, then a burst as **any** principal trips it after 5 calls:

```bash
uv run python tests/scripts/call_mcp_tool.py \
  --server-url "$SRV" --tool healthcheck --tool-args '{}' \
  --token-file .token-rl-test-user --registry-url "$REG" --count 10
# -> 5x 200, then 429; throttle log: axis=tgt entity_type=mcp_server name=airegistry-tools
```

**2. Tool-level scope (current capability + limitation).** The `name` you set for a target limit is matched against the target the auth-server classifies from the request path. In v1 the classifier resolves the target at **MCP-server granularity** (the server segment of the path), not per tool: every `tools/call` to `airegistry-tools` shares the one `mcp_server:airegistry-tools` counter regardless of which tool (`healthcheck`, `intelligent_tool_finder`, ...) is invoked. So:

- A limit on `--entity-type mcp_server --name airegistry-tools` caps the whole server (all tools together). This is the enforceable tool-adjacent control today.
- Fine-grained `mcp_tool` / `a2a_skill` targets (a limit on a single `server:tool`) are modeled in the definitions API but are **rejected as not-yet-enforceable** at config time (they need the JSON-RPC payload, a later phase). Confirm the guard:

```bash
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" \
  rate-limit-set --axis target --entity-type mcp_tool \
  --name airegistry-tools/healthcheck --max-requests 5 --window-seconds 60
# -> 400 rejected: "entity_type 'mcp_tool' is not enforced in this version
#    (tool/skill rate limiting is a later phase)"
```

**3. Workarounds to bound a specific tool today.** Until fine-grained `mcp_tool` targets land, there are two ways to constrain tool usage. Both are runnable now.

**3a. Isolate the tool on its own MCP server path (target limit per server).** A target `mcp_server` limit is keyed on the server path, and each path is an independent counter. So if a tool is exposed as its own registered MCP server, an `mcp_server` limit on that path bounds exactly that tool without touching other servers/tools. The path-isolation property is what makes this work: a limit set on `--name airegistry-tools` only ever decrements the `tgt:mcp_server:airegistry-tools` counter.

```bash
# Limit ONLY the airegistry-tools server path to 5/min.
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" \
  rate-limit-set --axis target --entity-type mcp_server \
  --name airegistry-tools --max-requests 5 --window-seconds 60
# Wait ~30s for the definitions cache, then trip it (this path has its own counter):
uv run python tests/scripts/call_mcp_tool.py \
  --server-url "$SRV" --tool healthcheck --tool-args '{}' \
  --token-file .token-rl-test-user --registry-url "$REG" --count 10
# -> 5x 200 then 429; throttle log: axis=tgt entity_type=mcp_server name=airegistry-tools
```

That a *different* server path is unaffected follows from the counter key including the
server name (`tgt:mcp_server:<name>:<window>`) — no limit named for another path is ever
consulted. To confirm against a second server, point `--server-url` at another registered
server's endpoint (use its exact path from `grep -E "location .*/mcp" /etc/nginx/conf.d/nginx_rev_proxy.conf`
inside the registry container, e.g. `http://localhost/ai-registry/mcpgw/mcp`) and a
same-size burst returns all `200` because no target limit names that path.

So, to cap a single tool: register it as its own MCP server (it then has a dedicated path) and put an `mcp_server` target limit on that path. Every call to that tool hits its own counter, isolated from other tools/servers.

**3b. Bound the callers instead (caller-axis group limit).** If the goal is "these callers may not hammer this tool", a caller group limit (Step 2 / Step 7) already does it — it caps each caller's total data-plane rate. This does not single out one tool, but it prevents any one user/agent from over-calling. Reuse the `rl-test` group + membership from Step 2 (user 25/60s) or `rl-agents` from Step 7 (agent 10/60s); the burst there is the test.

```bash
# Example: the rl-test-user caller is already capped at 25/min by the rl-test group
# (Step 2). That cap governs its calls to airegistry-tools' healthcheck tool too.
uv run python tests/scripts/call_mcp_tool.py \
  --server-url "$SRV" --tool healthcheck --tool-args '{}' \
  --token-file .token-rl-test-user --registry-url "$REG" --count 30
# -> 25x 200 then 429 (caller-axis; see throttle log caller_type=user)
```

---

## Cleanup

```bash
# Remove memberships
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" \
  rate-limit-member-delete --id user:rl-test-user
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" \
  rate-limit-member-delete --id client:rl-test-m2m

# Remove definitions (list first to get exact ids)
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" rate-limit-list
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" \
  rate-limit-delete --id caller:group:rl-test:60
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" \
  rate-limit-delete --id caller:group:rl-test:86400
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" \
  rate-limit-delete --id target:mcp_server:airegistry-tools:60
# ...repeat for rl-test-strict / rl-agents

# (Optional) delete the Keycloak test principals and local token files.
# Reuses $KC_URL / $REALM / $ADMIN_TOKEN from Step 0 (refresh ADMIN_TOKEN if expired).
RL_USER_ID=$(curl -s -H "Authorization: Bearer $ADMIN_TOKEN" \
  "$KC_URL/admin/realms/$REALM/users?username=rl-test-user" | jq -r '.[0].id')
curl -s -o /dev/null -w "del_user=%{http_code}\n" -X DELETE \
  "$KC_URL/admin/realms/$REALM/users/$RL_USER_ID" -H "Authorization: Bearer $ADMIN_TOKEN"
M2M_UUID=$(curl -s -H "Authorization: Bearer $ADMIN_TOKEN" \
  "$KC_URL/admin/realms/$REALM/clients?clientId=rl-test-m2m" | jq -r '.[0].id')
curl -s -o /dev/null -w "del_m2m=%{http_code}\n" -X DELETE \
  "$KC_URL/admin/realms/$REALM/clients/$M2M_UUID" -H "Authorization: Bearer $ADMIN_TOKEN"
rm -f .token-rl-test-user .token-rl-test-m2m
```

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| All 200s even with a limit + membership | `RATE_LIMITING_ENABLED` not `true` on the containers; or you tested as **admin** (bypassed); or you hit `/api/*` (control-plane, exempt) instead of an MCP server. |
| `403 Access forbidden` on every call as `rl-test-user`/`rl-test-m2m` (no `X-RateLimit-*` header, `0 throttled`) | Genuine **authorization** denial, not a throttle: the principal's group does not map to a scope granting the target server (auth-server logs `Final mapped scopes: []` then `Access denied ... no scopes configured`). Rate limiting runs only **after** authorization passes, so the limiter is never reached. Fix: put the principal in a group whose name appears in some `mcp_scopes_default` doc's `group_mappings` array **and** whose scope grants the target server. `read-all-register-new` maps to a scope with `server:"*"` in this deployment (used in Step 0). Check with: `docker exec mcp-mongodb mongosh -u admin -p "$DOCUMENTDB_PASSWORD" --authenticationDatabase admin --quiet --eval 'db.getSiblingDB("mcp_registry").mcp_scopes_default.find({group_mappings:"<group>"},{_id:1}).forEach(d=>print(d._id))'`. |
| `member-set` returns 404 | Containers predate the memberships build; rebuild. |
| Login/dashboard breaks | Should not happen now (data-plane-only scope + admin bypass). If it does, a caller limit is somehow applying to `/api/*` — capture the auth-server logs. |
| Metrics missing in Prometheus | Scrape `auth-server:9464/metrics` directly; if present there but not in Prometheus, check the `mcp-auth-server` job. Enforcement is in auth-server, not registry. |
| 429 on the very first call | The limit is below what the UI's parallel calls need, or a stale ~30s cache — wait 30s or restart auth-server. |

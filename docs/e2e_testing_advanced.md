# Rate Limiting: Advanced / Edge-Case Test Guide (issue #295)

Companion to [e2e_testing.md](e2e_testing.md). That guide covers the happy paths (group limits, headers, metrics, floors, per-agent, target). This one covers the **failure modes and correctness invariants** — the behaviors that matter most in production and are easy to regress.

Run these **after** the base guide's Step 0, so the `rl-test-user` / `rl-test-m2m` principals and their token files already exist.

> **Before you start, you MUST set and export these environment variables** (no defaults; the commands fail loudly if any is unset, so no URL or password is ever hardcoded in this repo):
>
> ```bash
> export KC_URL=...                 # Keycloak base URL reachable from the host
> export REG=...                    # gateway base URL (e.g. http://localhost)
> export RL_TEST_USER_PASSWORD=...  # password for the rl-test-user test account
> ```
>
> Plus the Keycloak admin credentials from your environment: `KEYCLOAK_ADMIN`, `KEYCLOAK_ADMIN_PASSWORD`, `KEYCLOAK_REALM` (e.g. `set -a; source .env; set +a`).

## Conventions

Same as the base guide (which you must have run through Step 0). From the repo root, with the required env vars from above already exported:

```bash
# Fail loudly if a required input is missing (no defaults for URLs/password).
: "${REG:?Set REG (gateway base URL)}"
: "${KC_URL:?Set KC_URL (Keycloak base URL reachable from the host)}"
: "${RL_TEST_USER_PASSWORD:?Set RL_TEST_USER_PASSWORD (test account password)}"

export TOK=.token                          # admin token file
export SRV="$REG/airegistry-tools/mcp"     # a confirmed data-plane MCP endpoint
```

Reminders:
- New/changed definitions take up to `RATE_LIMIT_DEFINITIONS_CACHE_TTL_SECONDS` (default **30s**) to go live. Wait ~30s after any `rate-limit-set`.
- Principal tokens expire (~5 min). Re-run the token `curl` from base-guide Step 0 to refresh `.token-rl-test-user` / `.token-rl-test-m2m` on a `401`.
- A `curl` helper used throughout (bare-JWT token files):

```bash
# Usage: RLTOKEN=$(cat .token-rl-test-user); rlcall   -> prints the HTTP status
rlcall() {
  curl -s -o /dev/null -w "%{http_code}" -X POST "$SRV" \
    -H "X-Authorization: Bearer $RLTOKEN" -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"healthcheck","arguments":{}}}'
}
```

---

## Test 1 — Concurrency / atomicity (no over-admit under a parallel burst)

The counter increment must be atomic: firing many requests **in parallel** at a limit of `K` must admit **exactly** `K`, never more. This validates the conditional atomic `$inc` (a naive read-then-write would over-admit under load).

```bash
# Target limit of 5/min on the airegistry-tools server.
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" \
  rate-limit-set --axis target --entity-type mcp_server \
  --name airegistry-tools --max-requests 5 --window-seconds 60
sleep 32   # cache

# Fire 20 requests concurrently and tally the statuses.
RLTOKEN=$(cat .token-rl-test-user)
for i in $(seq 1 20); do
  curl -s -o /dev/null -w "%{http_code}\n" -X POST "$SRV" \
    -H "X-Authorization: Bearer $RLTOKEN" -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"healthcheck","arguments":{}}}' &
done | sort | uniq -c
wait
```

**Expected:** exactly `5  200` and `15  429`. If you see more than 5 `200`, the increment is not atomic (a race is over-admitting).

---

## Test 2 — Window reset + `Retry-After` honesty

A fixed window must recover once it rolls over, and the `Retry-After` header must roughly match the time to reset.

```bash
RLTOKEN=$(cat .token-rl-test-user)

# Burn through the 5/60s limit (from Test 1), then inspect a throttled response:
for i in $(seq 1 6); do rlcall >/dev/null; done
curl -s -D - -o /dev/null -X POST "$SRV" \
  -H "X-Authorization: Bearer $RLTOKEN" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"healthcheck","arguments":{}}}' \
  | grep -iE "^HTTP|x-ratelimit|retry-after"
```

**Expected** (all four headers present):

```
HTTP/1.1 429 Too Many Requests
X-RateLimit-Limit: 5
X-RateLimit-Remaining: 0
X-RateLimit-Reset: <epoch>
Retry-After: <seconds, 1..60>
```

Then confirm recovery after the window rolls over:

```bash
sleep "$(( $(curl -s -D - -o /dev/null -X POST "$SRV" -H "X-Authorization: Bearer $RLTOKEN" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"healthcheck","arguments":{}}}' \
  | awk 'tolower($1)=="retry-after:"{print $2+2}' | tr -d '\r') ))"
echo "after reset: $(rlcall)"   # -> 200
```

**Expected:** `200` after the window resets. (`Retry-After` was missing before the fix that captures `$upstream_http_retry_after`; if you see no `Retry-After` line, the nginx config is stale — regenerate it.)

---

## Test 3 — Deny-does-not-consume across windows (Blocker-1 invariant)

A request rejected by a tight window must **not** consume a wider window's quota, or a burst self-inflicts a long lockout. Give one group both a per-minute and a per-hour cap, burst past the minute cap, and confirm the hour counter only advanced by the *allowed* count.

```bash
# Group with 5/min AND 20/hour, mapped to rl-test-user.
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" \
  rate-limit-set --axis caller --entity-type group --name rl-dnc \
  --user-max-requests 5 --window-seconds 60
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" \
  rate-limit-set --axis caller --entity-type group --name rl-dnc \
  --user-max-requests 20 --window-seconds 3600
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" \
  rate-limit-member-set --subject-type user --subject rl-test-user --groups rl-dnc
sleep 32

# Also clear the Test 1 target limit so only the caller gates apply here:
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" \
  rate-limit-delete --id target:mcp_server:airegistry-tools:60 || true

# Burst 40 within one minute: the 5/min gate denies the rest.
RLTOKEN=$(cat .token-rl-test-user)
for i in $(seq 1 40); do rlcall; echo; done | sort | uniq -c
```

**Expected:** about `5  200` and `35  429` (the minute gate governs). Now inspect the **hour** counter directly — it must be ~5 (the allowed calls), NOT 40:

```bash
docker exec mcp-mongodb mongosh -u admin -p "$DOCUMENTDB_PASSWORD" --authenticationDatabase admin --quiet --eval '
const db = db.getSiblingDB("mcp_registry");
db.rate_limit_counters_default.find({_id:/clr:group:rl-test-user:3600/}).forEach(d=>printjson({_id:d._id,count:d.count}));'
```

**Expected:** the `...:3600` counter's `count` is ~5, not ~40. If it is ~40, denied requests are wrongly consuming the daily/hourly budget (the Blocker-1 regression).

> Set `DOCUMENTDB_PASSWORD` first: `export DOCUMENTDB_PASSWORD=$(docker exec mcp-gateway-registry-registry-1 printenv DOCUMENTDB_PASSWORD)`.

---

## Test 4 — Caller-type classification (human = user, machine = agent)

A human token must be classified as `caller_type=user` (and pick the group's user number), and an M2M token as `caller_type=agent` (agent number). This guards the fix for the azp-vs-client_id bug where humans were misclassified as agents.

```bash
# One group carrying BOTH numbers: user 25/min, agent 10/min.
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" \
  rate-limit-set --axis caller --entity-type group --name rl-both \
  --user-max-requests 25 --agent-max-requests 10 --window-seconds 60
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" \
  rate-limit-member-set --subject-type user   --subject rl-test-user --groups rl-both
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" \
  rate-limit-member-set --subject-type client --subject rl-test-m2m  --groups rl-both
sleep 32

echo "=== human (expect ~25x200 then 429) ==="
RLTOKEN=$(cat .token-rl-test-user); for i in $(seq 1 30); do rlcall; echo; done | sort | uniq -c

echo "=== agent (expect ~10x200 then 429) ==="
RLTOKEN=$(cat .token-rl-test-m2m);  for i in $(seq 1 15); do rlcall; echo; done | sort | uniq -c
```

**Expected:** the human trips near **25**, the agent near **10** — the SAME group, different number by caller type. Confirm the log attribution:

```bash
docker logs mcp-gateway-registry-auth-server-1 --since 2m 2>&1 | grep "rate-limit throttled" | tail -4
# human -> caller_type=user   caller_username=rl-test-user            caller_client_id=
# agent -> caller_type=agent  caller_username=service-account-rl-test-m2m  caller_client_id=rl-test-m2m
```

---

## Test 5 — Membership does not change authorization (security invariant)

Adding a caller to a rate-limit group must NOT alter what servers/scopes they can reach (rate-limit groups are decoupled from authz groups). Confirm access is identical before and after a membership change.

```bash
# Baseline: rl-test-user reaches the server (200).
RLTOKEN=$(cat .token-rl-test-user); echo "before membership: $(rlcall)"   # 200

# Add to a rate-limit group (already done in Test 4). A NEW token must still
# resolve the same authz scopes -- rate-limit groups never enter the scope path.
# (Refresh the token from base Step 0 to prove the fresh claims are unchanged.)
echo "after membership + token refresh: $(rlcall)"                        # still 200 (or 429 if over limit, never 403-authz)
```

**Expected:** access is unchanged. A `403` that appears ONLY after a membership change (with no `X-RateLimit-Throttled`) would indicate the membership leaked into authorization — it must not.

---

## Test 6 — Admin bypass on caller limits; target limits still apply to admin

An operator must not be able to lock themselves out on a **caller** limit, but a **target** limit must still bound everyone (including admin) to protect a weak backend.

```bash
# Caller limit on a group containing admin would NOT throttle admin data-plane
# calls (admins skip caller gates). Verify admin is not throttled by rl-both:
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" \
  rate-limit-member-set --subject-type user --subject admin --groups rl-both
sleep 32
RLTOKEN=$(python3 -c "import json;r=open('.token').read().strip().strip(chr(2)).strip();i=r.rfind('}');print(json.loads(r[:i+1])['tokens']['access_token'])")
for i in $(seq 1 30); do rlcall; echo; done | sort | uniq -c   # expect all 200 (caller gate skipped)

# But a TARGET limit applies to admin too:
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" \
  rate-limit-set --axis target --entity-type mcp_server \
  --name airegistry-tools --max-requests 5 --window-seconds 60
sleep 32
for i in $(seq 1 10); do rlcall; echo; done | sort | uniq -c   # expect ~5x200 then 429
```

**Expected:** admin is NOT throttled by the caller group (all `200`), but IS throttled by the target limit (`5x200` then `429`).

> Clean up the admin membership immediately after: `uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" rate-limit-member-delete --id user:admin`. Leaving a caller limit on admin is fine (admins bypass it), but keep the test DB tidy.

---

## Test 7 — Data-plane-only scope (control plane is never throttled)

Caller limits apply to data-plane MCP/A2A calls only. With a caller limit active on your user, the dashboard and `/api/*` must keep working — no `429`.

```bash
# With rl-both active on rl-test-user (Test 4), hammer a control-plane API as that
# user's session and confirm zero 429s. Example: repeatedly GET /api/auth/me is not
# applicable for a bearer; instead confirm /api/* over the admin session UI is
# unaffected. As a simple proxy, confirm many admin API calls never 429:
for i in $(seq 1 40); do
  curl -s -o /dev/null -w "%{http_code}\n" "$REG/api/rate-limits" \
    -H "X-Authorization: Bearer $(python3 -c "import json;r=open('.token').read().strip().strip(chr(2)).strip();i=r.rfind('}');print(json.loads(r[:i+1])['tokens']['access_token'])")"
done | sort | uniq -c
```

**Expected:** all `200` (or `401` if the token expired), **never** `429`. `/api/*` has no classified rate-limit target, so caller limits never apply there.

---

## Test 8 — Enable / disable / edit a definition

Disabling a definition (not deleting) stops enforcement; editing the number takes effect after the cache TTL.

```bash
# With a target limit on airegistry-tools tripping at 5, disable it:
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" \
  rate-limit-disable --id target:mcp_server:airegistry-tools:60
sleep 32
RLTOKEN=$(cat .token-rl-test-user); for i in $(seq 1 10); do rlcall; echo; done | sort | uniq -c
# -> all 200 (disabled definition is not enforced)

# Re-enable and confirm it bites again:
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" \
  rate-limit-enable --id target:mcp_server:airegistry-tools:60
sleep 32
for i in $(seq 1 10); do rlcall; echo; done | sort | uniq -c   # -> ~5x200 then 429
```

**Expected:** disabled -> all `200`; re-enabled -> throttles again.

---

## Test 9 — Fail-open vs fail-closed (backend unavailable)

> **DISRUPTIVE.** This stops the shared MongoDB/DocumentDB container, which also backs sessions and other services. Only run it in a disposable environment, and restart mongo immediately afterward. It is the single most important behavior to verify before trusting the limiter in production, but it is not safe on a shared box.

**Fail-open (default, availability first).** With `RATE_LIMIT_FAIL_OPEN=true` and a definition whose `fail_closed` is false, a backend outage must let calls **through** (never a self-inflicted outage) while the error metric climbs.

```bash
export DOCUMENTDB_PASSWORD=$(docker exec mcp-gateway-registry-registry-1 printenv DOCUMENTDB_PASSWORD)

# Ensure a fail-open target limit exists (default fail_closed=false).
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" \
  rate-limit-set --axis target --entity-type mcp_server \
  --name airegistry-tools --max-requests 5 --window-seconds 60
sleep 32

# Baseline error metric (likely absent):
docker exec mcp-gateway-registry-auth-server-1 sh -c 'curl -s localhost:9464/metrics' | grep "mcpgw_rate_limit_errors_total" | grep -v '^#'

# Stop the backend and drive calls -- they should still succeed (fail open):
docker stop mcp-mongodb
RLTOKEN=$(cat .token-rl-test-user); for i in $(seq 1 10); do rlcall; echo; done | sort | uniq -c
# -> expect 200s (fail open); the limiter could not read the counter but allowed the call.

# Error metric should now be climbing:
docker exec mcp-gateway-registry-auth-server-1 sh -c 'curl -s localhost:9464/metrics' | grep "mcpgw_rate_limit_errors_total" | grep -v '^#'

# RESTORE the backend:
docker start mcp-mongodb
sleep 10
```

**Expected:** calls return `200` while mongo is down (fail open), and `mcpgw_rate_limit_errors_total` increments.

**Fail-closed (security-critical limits).** A definition created with `--fail-closed` must **deny** when the backend is unavailable.

```bash
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" \
  rate-limit-set --axis target --entity-type mcp_server \
  --name airegistry-tools --max-requests 5 --window-seconds 60 --fail-closed
sleep 32

docker stop mcp-mongodb
RLTOKEN=$(cat .token-rl-test-user); for i in $(seq 1 5); do rlcall; echo; done | sort | uniq -c
# -> expect 429 (fail closed: backend error denies)
docker start mcp-mongodb
sleep 10
```

**Expected:** `429` while mongo is down (fail closed denies). Restore mongo immediately.

> Note on `RATE_LIMIT_FAIL_OPEN`: the global env flag is the default policy; a per-definition `fail_closed=true` overrides it toward denial for that specific limit. A definition is fail-closed if `fail_closed=true` OR the global `RATE_LIMIT_FAIL_OPEN=false`.

---

## Cleanup

```bash
for id in \
  "target:mcp_server:airegistry-tools:60" \
  "caller:group:rl-dnc:60" "caller:group:rl-dnc:3600" \
  "caller:group:rl-both:60"; do
  uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" rate-limit-delete --id "$id" || true
done

for id in user:rl-test-user client:rl-test-m2m user:admin; do
  uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" rate-limit-member-delete --id "$id" || true
done

# Confirm clean:
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" rate-limit-list
uv run python api/registry_management.py --token-file "$TOK" --registry-url "$REG" rate-limit-member-list
```

---

## Coverage not scripted here

These are better as automated tests than manual runs, or need infra this guide does not assume:

- **Cross-replica correctness** — two auth-server replicas sharing one DocumentDB must enforce one combined counter (the counter store is shared, not per-process). Verify by scaling the auth-server to 2 replicas and confirming the limit is global, not per-replica.
- **Persistence across restart** — definitions/memberships live in DocumentDB; restarting the auth-server must resume enforcement without re-seeding (the ~30s cache repopulates on first use).
- **A2A agent target** — Step 8 covers `mcp_server`; the `a2a_agent` target axis needs the A2A reverse proxy enabled (`A2A_REVERSE_PROXY_ENABLED=true`) and an `/agent/<path>` route.
- **UI parity** — create/edit/toggle a definition and edit membership via **Settings -> IAM -> Rate Limits** and the Users / M2M membership editors; behavior must match the CLI.
- **Most-restrictive-wins across groups** — a caller in two groups with different limits at the same window is governed by the tighter one (unit-tested in `tests/unit/rate_limiting/test_limiter.py`; add an e2e pass if you want belt-and-braces).

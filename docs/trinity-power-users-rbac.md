# Trinity Power Users — RBAC Scope Reference

This document records the scope configuration for the `trinity-power-users` role in the AIGR
(AI Gateway & Registry) and all commands used to verify and manage it. It covers the dev and
staging Kubernetes environments operated by the Trinity team.

---

## Overview

The `trinity-power-users` scope grants a "developer / analyst" role to members of a dedicated
Entra security group. Power users can discover and use all registered assets, register their own
agents, MCP servers, and skills, and modify the resources they own — without admin elevation.

| Aspect | Detail |
|---|---|
| Scope name (MongoDB `_id`) | `trinity-power-users` |
| Entra group Object ID | `57ea5441-f8ba-4b74-bc04-cd0500bb12b3` |
| MCP gateway access | Provided by `mcp-servers-unrestricted/execute` (group_mappings updated separately in MongoDB) |
| Admin status | **No** — uses `"*"` wildcard, not `"all"`, for mutating permissions |

### Why two scopes instead of one

The `import-group` API blocks any scope definition containing `"server": "*"` (a security guard
that prevents non-init-time injection of cross-server wildcards). Wildcard MCP gateway access is
therefore supplied by the pre-seeded `mcp-servers-unrestricted/execute` scope, which already
carries `"server": "*"` and was updated to include the power user Entra group Object ID directly
in MongoDB. `trinity-power-users` itself carries only `ui_permissions` and the registry API
server entry.

### Permission table

| ui_permission | Value | Effect |
|---|---|---|
| `list_service` | `["all"]` | See all registered MCP servers |
| `health_check_service` | `["all"]` | Run health checks on all servers |
| `register_service` | `["*"]` | Register new MCP servers (ownership check prevents overwriting others') |
| `modify_service` | `["*"]` | Edit own MCP servers and agents (route-level `registered_by` guard applies) |
| `modify_agent` | `["*"]` | Semantic parity with admin scope; future-proofs against code evolution |
| `list_virtual_server` | `["all"]` | Discover all virtual servers |
| `list_agents` | `["all"]` | List all public / group-accessible agents |
| `get_agent` | `["all"]` | Fetch agent card details |
| `publish_agent` | `["*"]` | Register new agents |
| `publish_skill` | `["*"]` | Register new skills |
| `toggle_service` | — | **Not granted** — toggle has no ownership check; would affect all users |
| `delete_service` / `delete_agent` | — | **Not granted** |

### Why `"*"` does not grant admin

`_user_is_admin()` (in `registry/auth/dependencies.py`) fires only when a mutating action has
`"all"` in its resource list. A scope with e.g. `modify_service: ["*"]` passes the route-level
permission gate (`user_has_ui_permission_for_service` — fixed to recognize `"*"` as a wildcard)
but never triggers the admin flag. Route-level `registered_by` ownership guards still apply, so
power users can only modify resources they own.

---

## Environments

| Environment | Registry URL | k8s context | Namespace | MongoDB collection |
|---|---|---|---|---|
| Dev | `https://mcpregistry.dev.pb.analysis.cyber.gc.ca` | `pb-dev` | `trinity` | `mcp_scopes_trinity` |
| Staging | `https://mcpregistry-stg.hogwarts.pb.azure.chimera.cyber.gc.ca` | `pb-stg` | `trinity-stg` | `mcp_scopes_trinity-stg` |

---

## Verification Commands

### Prerequisites

```bash
# Save an admin JWT from the registry UI (Settings → Token Generation)
# or use the self-signed token from the user profile sidebar
echo "<your-jwt-here>" > ~/development/awa-poc/ai-gateway-registry/api/.token

# Note: a token is environment-specific (dev token works on dev, staging token on staging).
# Swap the file contents when targeting a different environment.
```

---

### 1. List all scopes and group_mappings (CLI)

```bash
cd ~/development/awa-poc/ai-gateway-registry

# Dev
uv run python api/registry_management.py \
  --registry-url https://mcpregistry.dev.pb.analysis.cyber.gc.ca \
  --token-file api/.token \
  list-groups --json 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
scopes = data.get('scopes_groups', data)
for name, meta in scopes.items():
    print(f'[{name}]')
    for k in ('group_mappings', 'ui_scopes'):
        v = meta.get(k)
        if v is not None:
            print(f'  {k}: {v}')
    print()
"

# Staging (swap token first)
uv run python api/registry_management.py \
  --registry-url https://mcpregistry-stg.hogwarts.pb.azure.chimera.cyber.gc.ca \
  --token-file api/.token \
  list-groups --json 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
scopes = data.get('scopes_groups', data)
for name, meta in scopes.items():
    print(f'[{name}]')
    for k in ('group_mappings', 'ui_scopes'):
        v = meta.get(k)
        if v is not None:
            print(f'  {k}: {v}')
    print()
"
```

---

### 2. Inspect the trinity-power-users scope in detail (CLI)

```bash
# Dev
uv run python api/registry_management.py \
  --registry-url https://mcpregistry.dev.pb.analysis.cyber.gc.ca \
  --token-file api/.token \
  describe-group --name trinity-power-users

# Staging (swap token first)
uv run python api/registry_management.py \
  --registry-url https://mcpregistry-stg.hogwarts.pb.azure.chimera.cyber.gc.ca \
  --token-file api/.token \
  describe-group --name trinity-power-users
```

---

### 3. Inspect scopes via raw API (curl)

```bash
TOKEN=$(cat ~/development/awa-poc/ai-gateway-registry/api/.token | tr -d '\n')

# All scopes (dev)
curl -sk "https://mcpregistry.dev.pb.analysis.cyber.gc.ca/api/servers/groups" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# Specific scope (dev)
curl -sk "https://mcpregistry.dev.pb.analysis.cyber.gc.ca/api/servers/groups/trinity-power-users" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

---

### 4. Verify group_mappings directly in MongoDB

```bash
# ── Dev ──────────────────────────────────────────────────────────────────────
MONGO_POD_DEV=$(kubectl --context pb-dev -n trinity get pod \
  -l app.kubernetes.io/name=mongodb -o jsonpath='{.items[0].metadata.name}')
MONGO_PASS_DEV=$(kubectl --context pb-dev -n trinity get secret mongo-credentials \
  -o jsonpath='{.data.DOCUMENTDB_PASSWORD}' | base64 -d)

kubectl --context pb-dev -n trinity exec "$MONGO_POD_DEV" -- \
  mongosh --username root --password "$MONGO_PASS_DEV" \
  --authenticationDatabase admin --quiet mcp_registry \
  --eval 'JSON.stringify(
    db.mcp_scopes_trinity.find({},{_id:1,group_mappings:1}).toArray(), null, 2
  )' 2>/dev/null

# ── Staging ───────────────────────────────────────────────────────────────────
MONGO_POD_STG=$(kubectl --context pb-stg -n trinity-stg get pod \
  -l app.kubernetes.io/name=mongodb -o jsonpath='{.items[0].metadata.name}')
MONGO_PASS_STG=$(kubectl --context pb-stg -n trinity-stg get secret mongo-credentials \
  -o jsonpath='{.data.DOCUMENTDB_PASSWORD}' | base64 -d)

kubectl --context pb-stg -n trinity-stg exec "$MONGO_POD_STG" -- \
  mongosh --username root --password "$MONGO_PASS_STG" \
  --authenticationDatabase admin --quiet mcp_registry \
  --eval 'JSON.stringify(
    db["mcp_scopes_trinity-stg"].find({},{_id:1,group_mappings:1}).toArray(), null, 2
  )' 2>/dev/null
```

---

### 5. Verify a specific scope document in full (MongoDB)

```bash
# Replace SCOPE_NAME and CONTEXT/NS/COLLECTION for your target environment

# Dev — trinity-power-users full document
kubectl --context pb-dev -n trinity exec "$MONGO_POD_DEV" -- \
  mongosh --username root --password "$MONGO_PASS_DEV" \
  --authenticationDatabase admin --quiet mcp_registry \
  --eval '
    var d = db.mcp_scopes_trinity.findOne({_id:"trinity-power-users"});
    print("group_mappings:", JSON.stringify(d.group_mappings));
    print("ui_permissions:", JSON.stringify(d.ui_permissions, null, 2));
  ' 2>/dev/null

# Staging
kubectl --context pb-stg -n trinity-stg exec "$MONGO_POD_STG" -- \
  mongosh --username root --password "$MONGO_PASS_STG" \
  --authenticationDatabase admin --quiet mcp_registry \
  --eval '
    var d = db["mcp_scopes_trinity-stg"].findOne({_id:"trinity-power-users"});
    print("group_mappings:", JSON.stringify(d.group_mappings));
    print("ui_permissions:", JSON.stringify(d.ui_permissions, null, 2));
  ' 2>/dev/null
```

---

### 6. Import / update the scope via API

Use this after any change to `cli/examples/trinity-power-users.json`. The endpoint upserts the
scope document and triggers an auth-server scope-cache reload.

```bash
cd ~/development/awa-poc/ai-gateway-registry

# Dev (requires dev admin token in api/.token)
uv run python api/registry_management.py \
  --registry-url https://mcpregistry.dev.pb.analysis.cyber.gc.ca \
  --token-file api/.token \
  import-group --file cli/examples/trinity-power-users.json

# Staging (swap token first)
uv run python api/registry_management.py \
  --registry-url https://mcpregistry-stg.hogwarts.pb.azure.chimera.cyber.gc.ca \
  --token-file api/.token \
  import-group --file cli/examples/trinity-power-users.json
```

---

### 7. Side-by-side comparison of both environments

```bash
for ENV in "pb-dev trinity mcp_scopes_trinity" "pb-stg trinity-stg mcp_scopes_trinity-stg"; do
  CTX=$(echo $ENV | cut -d' ' -f1)
  NS=$(echo $ENV  | cut -d' ' -f2)
  COLL=$(echo $ENV | cut -d' ' -f3)
  POD=$(kubectl --context $CTX -n $NS get pod \
    -l app.kubernetes.io/name=mongodb -o jsonpath='{.items[0].metadata.name}')
  PASS=$(kubectl --context $CTX -n $NS get secret mongo-credentials \
    -o jsonpath='{.data.DOCUMENTDB_PASSWORD}' | base64 -d)
  echo "════ $CTX — all scopes ════"
  kubectl --context $CTX -n $NS exec "$POD" -- \
    mongosh --username root --password "$PASS" \
    --authenticationDatabase admin --quiet mcp_registry \
    --eval "JSON.stringify(db['$COLL'].find({},{_id:1,group_mappings:1}).toArray(),null,2)" 2>/dev/null
  echo ""
done
```

---

### 8. Check what scopes/permissions a logged-in user receives

A power user can call the registry debug endpoint after logging in via the browser. With their
session cookie:

```bash
# From a curl-accessible session (replace cookie value):
curl -sk "https://mcpregistry.dev.pb.analysis.cyber.gc.ca/api/servers/user-context" \
  -H "Cookie: mcp_gateway_session=<session-cookie>" | python3 -m json.tool
```

Look for:
- `"is_admin": false` — confirmed non-admin
- `"accessible_servers": ["*"]` — wildcard MCP gateway access from `mcp-servers-unrestricted/execute`
- `"ui_permissions"` containing `modify_service`, `modify_agent`, `publish_skill`, etc.

---

### 9. RBAC behaviour checklist (manual test)

Log in as a user in the `57ea5441-f8ba-4b74-bc04-cd0500bb12b3` Entra group who is **not** in the
admin group.

| Test | Expected result |
|---|---|
| View MCP Servers tab | All servers visible, no toggle switches, no delete buttons on others' servers |
| View Agents tab | All public agents visible |
| Register a new MCP server | Succeeds |
| Edit an MCP server you just registered | Succeeds (bug fixed: previously 403) |
| Edit an MCP server registered by another user | 403 Forbidden |
| Register a new agent | Succeeds |
| Edit an agent you just registered | Succeeds |
| Edit an agent registered by another user | 403 Forbidden |
| Register a new skill | Succeeds |
| Edit a skill you just registered | Succeeds |
| Delete a server/agent registered by another user | 403 Forbidden |
| Settings → IAM panel visible | Not accessible (no admin escalation) |
| Visibility: register agent with `group-restricted` + your Entra group in `allowed_groups` | Agent visible to your group, invisible to others |

---

## Helm chart reference

The scope is seeded by the `mongodb-configure` init job. Key locations in the chart:

| File | What it controls |
|---|---|
| `charts/mongodb-configure/values.yaml` | `global.authProvider.entra.developerGroupId` — Entra Object ID wired into both scopes |
| `charts/mongodb-configure/templates/configmap.yaml` | `trinity-power-users.json` block + `mcp-servers-unrestricted/execute.group_mappings` |
| `cli/examples/trinity-power-users.json` | Canonical scope definition used by `import-group` CLI |
| `deployment_trinity/aigr/values/values.pb.ap-aks-pb-dev.trinity.yaml` | `developerGroupId: 57ea5441-...` (dev) |
| `deployment_trinity/aigr/values/values.pb.hogwarts-aks-pb.trinity-stg.yaml` | `developerGroupId: 57ea5441-...` (staging) |

To make a scope change permanent across redeployments:
1. Edit `cli/examples/trinity-power-users.json`
2. Update the matching block in `charts/mongodb-configure/templates/configmap.yaml`
3. Import via the API (`import-group` command above) to activate immediately
4. On next `make aigr-deploy env=<env>`, the init job re-seeds from the configmap

---

## Staging: pending re-import

After the `modify_agent` permission was added, the staging MongoDB was updated directly
(`$set: { 'ui_permissions.modify_agent': ['*'] }`). A re-import via the API is needed to
trigger an auth-server scope-cache reload in staging. Run when a staging admin token is available:

```bash
uv run python api/registry_management.py \
  --registry-url https://mcpregistry-stg.hogwarts.pb.azure.chimera.cyber.gc.ca \
  --token-file api/.token \
  import-group --file cli/examples/trinity-power-users.json
```

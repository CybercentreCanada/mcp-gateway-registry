# A2A Shared Gateway/Agent Token Mode (fork port guide)

This document describes the code changes required to add an **opt-in** mode in
which a single `Authorization` bearer token is used **both** to authenticate the
caller to the gateway **and** to authenticate to the downstream A2A agent. When
enabled, the gateway stops requiring the separate `X-Authorization` gateway
credential on agent paths and stops rejecting a request whose `Authorization`
equals its `X-Authorization`.

It is written so it can be applied to a **fork** of this project. Each section
gives the file, the current behavior, and the exact edit.

---

## 1. Background: the default (strict) behavior

On an A2A agent path (`{root}/agent/{agent_path}/...`) the gateway today enforces
strict credential separation:

- **Gateway credential** travels in `X-Authorization`. It is validated at
  `/validate` and stripped at egress — it must never reach the agent backend.
- **Agent credential** travels in `Authorization`. It is forwarded end-to-end to
  the agent backend; the gateway never inspects it as a gateway token on agent
  paths.

Two rules implement this in the auth server's `/validate` handler
([auth_server/server.py](../../auth_server/server.py), function
`validate_request`):

1. **No `Authorization` fallback on agent paths.** If `X-Authorization` is absent
   on an agent path, the request fails closed (unauthenticated) rather than
   authenticating on the agent's `Authorization` token.
2. **Duplicate-token rejection.** If `Authorization` == `X-Authorization` on an
   agent path, the request is refused with `401`, so a caller cannot leak its
   gateway credential to the registrant-controlled agent backend.

nginx already forwards `Authorization` end-to-end to both the `/validate`
subrequest and the agent backend, and strips `X-Authorization` + `Cookie` on the
agent hop. See the agent location blocks in
[registry/core/nginx_service.py](../../registry/core/nginx_service.py).

### Security caveat (read before enabling)

This mode **intentionally defeats** the credential-separation protection. When
enabled, the token used to authenticate to the gateway is also handed to the
registrant-controlled agent backend, which can replay it against the registry.
It is only safe when the agent backend is in the **same trust domain** as the
gateway (i.e. you own the agent). It must therefore be:

- **default-off**,
- **explicitly opt-in via an env var**, and
- **fail-closed** (strict behavior remains the default whenever the flag is unset
  or false).

---

## 2. Change map (what to touch)

| # | Area | File |
|---|------|------|
| 1 | Setting definition | `registry/core/config.py` |
| 2 | Core `/validate` logic | `auth_server/server.py` |
| 3 | Helm values | `charts/auth-server/values.yaml` |
| 4 | Helm deployment env | `charts/auth-server/templates/deployment.yaml` |
| 5 | Helm reserved names | `charts/auth-server/reserved-env-names.txt` |
| 6 | Helm reserved-name helper | `charts/auth-server/templates/_helpers.tpl` |
| 7 | Helm unit tests | `charts/auth-server/tests/` |
| 8 | Auth-server unit tests | `tests/auth_server/unit/test_server.py` |
| 9 | (Optional) Terraform/ECS + CDK parity | `terraform/aws-ecs/`, `infra/` |
| 10 | Docs | `docs/design/egress-auth-design.md`, `docs/a2a.md` |

Env var name used throughout this guide:

```
A2A_SHARED_GATEWAY_AGENT_TOKEN_ENABLED   (bool, default false)
```

nginx needs **no change** — it already forwards `Authorization` to the agent
backend and to `/validate`.

---

## 3. Setting definition — `registry/core/config.py`

The auth server imports `settings` from `registry.core.config`, so add the flag
as a Pydantic `Settings` field near the other A2A / egress fields.

```python
a2a_shared_gateway_agent_token_enabled: bool = Field(
    default=False,
    description=(
        "OPT-IN, default-off. When true, on an A2A agent path the gateway "
        "authenticates the caller on the standard Authorization header (falling "
        "back when X-Authorization is absent) and no longer rejects a request "
        "whose Authorization equals its X-Authorization. This lets a single "
        "bearer token both authenticate to the gateway AND be forwarded to the "
        "agent backend. It defeats gateway/agent credential separation, so only "
        "enable it when the agent backend is in the same trust domain as the "
        "gateway. Env: A2A_SHARED_GATEWAY_AGENT_TOKEN_ENABLED."
    ),
)
```

Pydantic `BaseSettings` maps this field to the `A2A_SHARED_GATEWAY_AGENT_TOKEN_ENABLED`
env var automatically (case-insensitive).

---

## 4. Core logic — `auth_server/server.py` (`validate_request`)

Locate the agent-path credential-selection block. In the current code it reads:

```python
        a2a_agent_path = _get_a2a_agent_path(original_url)
        is_a2a_request = a2a_agent_path is not None
        if x_authorization:
            authorization = x_authorization
        elif is_a2a_request:
            # No gateway credential on an agent path: fail closed as
            # unauthenticated rather than trusting the target-agent Authorization.
            authorization = None
        else:
            authorization = raw_authorization
```

### 4a. Allow the `Authorization` fallback when the flag is on

```python
        a2a_agent_path = _get_a2a_agent_path(original_url)
        is_a2a_request = a2a_agent_path is not None
        shared_token_mode = getattr(
            settings, "a2a_shared_gateway_agent_token_enabled", False
        )
        if x_authorization:
            authorization = x_authorization
        elif is_a2a_request and not shared_token_mode:
            # Strict default: no gateway credential on an agent path -> fail closed
            # rather than trusting the target-agent Authorization.
            authorization = None
        else:
            # Non-agent path, OR agent path with shared-token mode enabled:
            # authenticate on the standard Authorization header.
            authorization = raw_authorization
```

### 4b. Skip the duplicate-token rejection when the flag is on

The current duplicate check reads:

```python
        if (
            is_a2a_request
            and x_authorization
            and _bearer_token_value(raw_authorization) == _bearer_token_value(x_authorization)
        ):
            logger.warning(...)
            return JSONResponse(
                content={"detail": "Authorization must not duplicate the gateway credential"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer", "Connection": "close"},
            )
```

Add `and not shared_token_mode` to the guard so the rejection is bypassed only
when the operator has explicitly opted in:

```python
        if (
            is_a2a_request
            and not shared_token_mode
            and x_authorization
            and _bearer_token_value(raw_authorization) == _bearer_token_value(x_authorization)
        ):
            logger.warning(...)
            return JSONResponse(...)
```

Everything else (scope/`invoke_agent` enforcement, token validation) is unchanged
and still runs.

> Note: in shared-token mode the same `Authorization` header is forwarded to the
> agent backend by nginx (it is not stripped on the agent hop). This is the
> intended behavior of this mode — no nginx edit is required.

---

## 5. Helm — `charts/auth-server`

### 5a. `values.yaml`

Add a first-class toggle (mirrors the `egressAuth`/`rateLimiting` style):

```yaml
# A2A shared gateway/agent token mode (OPT-IN, default off).
# When true, a single Authorization bearer token both authenticates to the
# gateway AND is forwarded to the downstream agent on /agent/... paths. This
# DEFEATS gateway/agent credential separation: the gateway credential becomes
# readable (and replayable) by the registrant-controlled agent backend. Only
# enable when the agent backend is in the same trust domain as the gateway.
a2a:
  sharedGatewayAgentToken: false
```

### 5b. `templates/deployment.yaml`

Render the env var in the `env:` list, before the `extraEnv` append (mirrors the
`RATE_LIMITING_ENABLED` pattern):

```yaml
            - name: A2A_SHARED_GATEWAY_AGENT_TOKEN_ENABLED
              value: {{ .Values.a2a.sharedGatewayAgentToken | default false | toString | quote }}
            {{- with .Values.extraEnv }}
            {{- toYaml . | nindent 12 }}
            {{- end }}
```

### 5c. `reserved-env-names.txt`

Add the name so it cannot be shadowed by user-supplied `extraEnv`:

```
A2A_SHARED_GATEWAY_AGENT_TOKEN_ENABLED
```

### 5d. `templates/_helpers.tpl`

Add the same name to the `auth-server.reservedEnvNames` list and update that
helper's source comment to mention the new deployment env var. (Per the repo
convention, the reserved list is the union of every name the deployment can
render into `env:` plus every `envFrom` key.)

### 5e. Chart unit tests — `charts/auth-server/tests/`

- Add a case asserting the env var renders `"false"` by default and `"true"`
  when `a2a.sharedGatewayAgentToken=true`.
- If the `env[N]` index assertions in `tests/extra_env_test.yaml` hardcode the
  position where chart-managed env ends and `extraEnv` begins, bump those indices
  (you inserted one entry before the `extraEnv` append) and update the `NOTE:`
  comment. Add an `extraEnv`-collision rejection case for the new reserved name.

Run:

```bash
helm unittest charts/auth-server
# If the stack chart surfaces the toggle, refresh packaged subcharts first:
helm dep update charts/mcp-gateway-registry-stack
helm unittest charts/mcp-gateway-registry-stack
```

---

## 6. Auth-server unit tests — `tests/auth_server/unit/test_server.py`

Existing tests assert the strict behavior and must keep passing with the flag
**off** (the default), so they need no functional change — just confirm they run
with `a2a_shared_gateway_agent_token_enabled` unset/false. Add new cases with the
flag **on**:

1. **Fallback:** agent path, only `Authorization` (no `X-Authorization`) →
   authenticates successfully (no `401`).
2. **No duplicate rejection:** agent path, `Authorization` == `X-Authorization` →
   succeeds instead of returning `401`.
3. **Still fail-closed when off:** the same two requests with the flag off still
   return `401` (guards against regressions).

Patch the setting in tests the same way other `settings` fields are patched
(e.g. `monkeypatch.setattr(settings, "a2a_shared_gateway_agent_token_enabled", True)`).

---

## 7. Optional: deployment-surface parity

Per the project's IaC-parity rule, if you deploy via Terraform/ECS or CDK:

- **Docker Compose:** already works once the reserved name exists — set it in
  `extra_env/auth-server.env`:
  ```
  A2A_SHARED_GATEWAY_AGENT_TOKEN_ENABLED=true
  ```
- **Terraform/ECS:** flows through `auth_server_extra_env`. For a first-class
  variable, add it to the auth-server task definition env in
  `terraform/aws-ecs/` and mirror in the CDK stack under `infra/`.

---

## 8. Verification checklist

- [ ] `uv run python -m py_compile auth_server/server.py registry/core/config.py`
- [ ] Flag **off** (default): agent request with only `Authorization` → `401`;
      duplicate `Authorization`==`X-Authorization` → `401` (strict behavior intact).
- [ ] Flag **on**: agent request with only `Authorization` → authenticated and
      proxied; duplicate token → allowed.
- [ ] `invoke_agent` scope enforcement still applies in both modes.
- [ ] `helm unittest charts/auth-server` passes.
- [ ] `uv run pytest tests/auth_server/unit/test_server.py`.
- [ ] Docs updated: `docs/design/egress-auth-design.md`, `docs/a2a.md`.

---

## 9. Files that describe the strict model (update the prose)

The strict separation is described in:

- [docs/design/egress-auth-design.md](egress-auth-design.md)
- [docs/a2a.md](../a2a.md)

Add a short subsection to each noting that
`A2A_SHARED_GATEWAY_AGENT_TOKEN_ENABLED=true` relaxes the separation on agent
paths, that it is default-off and fail-closed, and the trust-domain caveat from
section 1 above.

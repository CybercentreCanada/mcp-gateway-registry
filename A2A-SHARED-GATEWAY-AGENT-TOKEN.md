# A2A shared gateway/agent token mode

This document describes the changes applied to the `cccs-main_1.29.0_aks-fixes`
branch that port commit `05cbdd6` ("added a new flag to allow sharing of the
authorization with the agents").

## What it does

Adds an **opt-in, default-off** flag, `A2A_SHARED_GATEWAY_AGENT_TOKEN_ENABLED`,
that changes how the auth server authenticates requests on **A2A agent paths**
(`/agent/...`).

- **Default (flag off, unchanged behavior):** the gateway fails closed on an
  agent path that has no gateway credential, and it rejects a request whose
  `Authorization` equals its `X-Authorization` (defense against a caller
  duplicating its gateway token into both headers). This preserves strict
  gateway/agent credential separation.
- **Flag on:** on an agent path the gateway authenticates the caller on the
  standard `Authorization` header (falling back when `X-Authorization` is
  absent) and no longer rejects `Authorization == X-Authorization`. This lets a
  **single bearer token** both authenticate to the gateway **and** be forwarded
  to the downstream agent backend.

### Security trade-off

Enabling this **defeats gateway/agent credential separation**: the gateway
credential becomes readable (and replayable) by the registrant-controlled agent
backend. Only enable it when the agent backend is in the **same trust domain**
as the gateway. See the design doc for the full threat model.

## Configuration surface

| Surface | Key | Default |
| --- | --- | --- |
| App setting / env | `A2A_SHARED_GATEWAY_AGENT_TOKEN_ENABLED` (`settings.a2a_shared_gateway_agent_token_enabled`) | `false` |
| Helm (auth-server) | `app.a2aSharedGatewayAgentTokenEnabled` | `false` |
| Terraform (aws-ecs) | `a2a_shared_gateway_agent_token_enabled` | `false` |

The env name is reserved in the auth-server chart, so it cannot be overridden
through `extraEnv`.

## Files changed

Applied from the source commit:

- Core logic
  - [auth_server/server.py](auth_server/server.py) — the `validate_request`
    decision: reads the flag and relaxes the two A2A checks when it is on.
  - [registry/core/config.py](registry/core/config.py) — new
    `a2a_shared_gateway_agent_token_enabled` setting (already present locally).
- Helm (auth-server)
  - [charts/auth-server/values.yaml](charts/auth-server/values.yaml) — new
    `app.a2aSharedGatewayAgentTokenEnabled: false`.
  - [charts/auth-server/templates/deployment.yaml](charts/auth-server/templates/deployment.yaml) —
    renders the `A2A_SHARED_GATEWAY_AGENT_TOKEN_ENABLED` env var.
  - [charts/auth-server/reserved-env-names.txt](charts/auth-server/reserved-env-names.txt) —
    reserves the env name.
  - [charts/auth-server/tests/a2a_shared_token_test.yaml](charts/auth-server/tests/a2a_shared_token_test.yaml) —
    new helm-unittest suite (added verbatim).
  - [charts/auth-server/tests/extra_env_test.yaml](charts/auth-server/tests/extra_env_test.yaml) —
    updated index/reserved-name assertions.
- Terraform (aws-ecs)
  - [terraform/aws-ecs/main.tf](terraform/aws-ecs/main.tf),
    [terraform/aws-ecs/modules/mcp-gateway/ecs-services.tf](terraform/aws-ecs/modules/mcp-gateway/ecs-services.tf),
    [terraform/aws-ecs/modules/mcp-gateway/variables.tf](terraform/aws-ecs/modules/mcp-gateway/variables.tf),
    [terraform/aws-ecs/variables.tf](terraform/aws-ecs/variables.tf),
    [terraform/aws-ecs/terraform.tfvars.example](terraform/aws-ecs/terraform.tfvars.example) —
    plumb the env var through the ECS task definition and variables.
- Tests
  - [tests/auth_server/unit/test_server.py](tests/auth_server/unit/test_server.py) —
    unit tests covering both flag states.

## Referenced / updated documentation

- [docs/design/a2a-shared-gateway-agent-token.md](docs/design/a2a-shared-gateway-agent-token.md) —
  the feature design doc and threat model (added verbatim from the commit).
- [docs/a2a.md](docs/a2a.md) — A2A user docs updated with the flag.
- [docs/design/egress-auth-design.md](docs/design/egress-auth-design.md) —
  related note on the egress auth design.

## Application notes

- `registry/core/config.py` already contained this flag in the working tree, so
  it was left as-is.
- `auth_server/uv.lock` from the source commit (~1485 lines of incidental lock
  churn) was **not** applied — it is unrelated to the feature and would conflict
  with this branch's pinned dependencies.
- The four auth-server chart files had diverged on this branch (additional env
  vars since the source commit), so their changes were merged manually rather
  than patched. The rendered `A2A_SHARED_GATEWAY_AGENT_TOKEN_ENABLED` env sits
  at index 12 on this branch (before the conditional `AWS_EC2_METADATA_DISABLED`).

## Validation

- `helm unittest charts/auth-server` — 9 suites / 49 tests pass.
- `tests/auth_server/unit/test_server.py` A2A tests — 33 passed.
- `py_compile` of the changed Python modules — clean.

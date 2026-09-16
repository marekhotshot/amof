# Capability Authority (v0)

Status: public Runtime Authority sibling of Write-Scope  
Audience: OSS operators using the public AMOF CLI

## Concept

Write-Scope Authority already governs **Git filesystem mutation**.

This slice adds the smallest sibling needed for a second execution surface:

```text
Authority
 ├── git.write            existing Write-Scope Authority
 └── kubernetes.read      this slice
     kubernetes.mutate    this slice
```

The lifecycle is the same:

```text
proposal → approval → binding → execution → verification → receipt
```

A worker proposal is never authority. An execution without a valid Approval and
Binding fails closed.

This is **not** a generic RBAC platform, policy language, or Kubernetes-native
controller. It is one governed capability slice.

## Kubernetes v0 supported surface

| Field | v0 |
|---|---|
| Capabilities | `kubernetes.read`, `kubernetes.mutate` |
| Verbs | `get`, `list`, `patch` |
| Resources | constrained by grant; advertised demo surface is `deployments` |
| Cluster | logical target id (not a kubeconfig path) |
| Namespace | required; deny wins over allow |
| TTL | mandatory on Approval |
| Executor | in-process fixture (`--executor fixture`) or one governed live adapter (`--executor live`) |

The live adapter is one AMOF-owned Kubernetes path: `Deployment` `get` and
annotation `patch` against a named logical cluster target. It is not a
Kubernetes platform, generic `kubectl` surface, or worker command runner.

Logical cluster ids resolve from
`capabilities/kubernetes/targets.json` (or `AMOF_K8S_TARGETS_FILE`) to an
existing kubeconfig path + context. Receipts store only the logical id.

## Enforcement guarantees

Runtime enforces:

1. No Kubernetes mutation without an active Approval + Binding.
2. Approval is pinned to the frozen capability body (`body_hash`).
3. Namespace / resource / verb outside the grant are rejected.
4. Expired and revoked Approvals are rejected.
5. `kubernetes.read` cannot silently become `kubernetes.mutate`.
6. Deny wins where allow and deny overlap.
7. Successful mutation consumes the Approval (single-use).
8. Successful reads do not consume the Approval (TTL still applies).
9. A successful receipt is minted only after verified in-scope execution.
10. Interrupted or unverified execution yields `UNVERIFIED`, never `PASS`.

## Receipt shape

`kubernetes_capability_receipt` (`kcr-...`) answers:

| Question | Field |
|---|---|
| What mission caused this? | `mission_id` |
| Which worker requested it? | `requested_by` |
| What authority was granted? | `capability`, `granted`, `approval_id` |
| What target was touched? | `target` |
| What operation was executed? | `operation.verb`, `operation.digest` |
| What changed? | `change.digest` / generation counters (no raw payload) |
| Was execution inside scope? | `within_scope` |
| How was the result verified? | `verification.method` |
| Final acceptance? | `acceptance_state` (`PASS` \| `FAIL` \| `UNVERIFIED`) |

Secrets and raw patch payloads are omitted (`secrets_omitted: true`).

## Example lifecycle

```bash
export AMOF_HOME=/tmp/amof-k8s-capability-demo

amof scope propose \
  --capability kubernetes.mutate \
  --cluster local-fixture \
  --namespace demo \
  --verb get --verb list --verb patch \
  --resource deployments \
  --from-run AMOF-CAPABILITY-AUTHORITY-V0-K8S-001 \
  --requested-by worker:demo \
  --reason "patch demo/web replicas"

amof scope approve kcp-... --ttl 30m --approved-by operator:you

# Out of scope: fails closed, no mutation
amof scope execute --approval kca-... \
  --cluster local-fixture --namespace other \
  --verb patch --resource deployments --name web \
  --patch-replicas 3 --run-id exec-oos

# In scope: fixture mutation + receipt
amof scope execute --approval kca-... \
  --cluster local-fixture --namespace demo \
  --verb patch --resource deployments --name web \
  --patch-replicas 3 --run-id exec-ok \
  --mission-id AMOF-CAPABILITY-AUTHORITY-V0-K8S-001 \
  --requested-by worker:demo --json
```

`amof scope list|show|revoke|audit` accept `kcp-` / `kca-` / `kcb-` / `kcr-`
ids on the existing Write-Scope CLI surface. No second orchestration path.

## Explicit non-claims

- Not perfect OS or container isolation.
- Not a Kubernetes RBAC replacement or admission controller.
- Not a Kubernetes platform. The live adapter is one Deployment get/patch lane
  after proposal → approval → binding. It does not expose raw `kubectl` or
  unrestricted worker cluster mutation.
- Not autonomous approval or automatic privilege renewal.
- Not network, database, secrets, or Jira capability authority.
- Not Predator / Workforce / runner-architecture replacement.
- Hermes still denies `kubernetes_mutation` as an execution capability.
- Does not claim that raw `kubectl` via unmanaged shells is intercepted
  everywhere. Public Runtime Authority v0 intercepts this slice's
  `amof scope execute` path.

App-data layout:

```text
capabilities/kubernetes/proposals/{proposal_id}.json
capabilities/kubernetes/approvals/{approval_id}.json
capabilities/kubernetes/bindings/{binding_id}.json
capabilities/kubernetes/receipts/{receipt_id}.json
capabilities/kubernetes/revocations/{revocation_id}.json
capabilities/kubernetes/events/kubernetes-capability-events.jsonl
```

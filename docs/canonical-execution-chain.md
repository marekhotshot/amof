# Canonical Execution Chain

Status: public architecture note (documents what already exists in code)  
Audience: maintainers and operators who might otherwise invent a parallel
“Intent → ExecutionRequest → ExecutionResult” layer  
Baseline: public `main` at the AMOF v3.3.0 Write-Scope Authority line
(`docs/write-scope-authority.md`, Hermes + Claude Code backends, handoff
dispatch). Private operator projection of delivery verdicts is described only
at the ownership boundary — not as a second public schema.

## Purpose

The governed execution path is **already sufficiently represented**. This note
names the concrete chain so future work extends adapters and evidence — it does
**not** introduce a new abstraction bus, ABC hierarchy, or parallel result type.

Audit verdict that triggered this note: **ALREADY SUFFICIENTLY REPRESENTED**
(document-over-abstraction).

## Chain (concrete names)

```text
intent / operative mission
        |
        v
canonical-mission-packet  (+ private operative-mission carrier when used)
        |
        v
PreparedHandoffPacket / handoff dispatch
        |
        v
duck-typed execution backends  (amof_native | hermes_opensandbox | claude_code | cursor_agent | builtin plan-execute)
        |
        v
agent-run-result  (result.json)
        |
        v
run evidence  (events, runtime log, receipts, evidence refs)
        |
        v
public lifecycle status / stop_reason
        +
private outcome_class / delivery gate  (operator projection; not a public field)
```

| Stage | Concrete artifact | Where it lives |
| --- | --- | --- |
| Intent | Operative mission text; public packet `goal` / `objective` (short) | Mission prep / intake; `contracts/canonical-mission-packet.schema.json` |
| Request | `PreparedHandoffPacket` + `amof handoff` prepare → accept → execute-agent | `scripts/amof/commands/handoff.py` |
| Worker / substrate | Duck-typed backends: `build_selection(...)` + `run(...)` | `scripts/amof/execution_backends/` (`amof_native`, `hermes_opensandbox`, `claude_code`, `cursor_agent`); builtin plan-execute when no runner |
| Result | `agent_run_result` / `agent-run-v1` → `result.json` | `contracts/agent-run-result.schema.json` |
| Evidence | Run dir artifacts + handoff evidence refs + optional Studio ledger | Backend `run()`; handoff receipts; `contracts/studio-lifecycle.md` |
| Verdict | Public: lifecycle `status` / `stop_reason` / failure classification. Private operator projection: `outcome_class` for delivery gating | Public result envelope; private console/runtime projection |

### Naming trap

`contracts/execution-handoff-result.schema.json` is **not** the agent execution
result. Its `result_kind` is `workspace_materialization_handoff_result` — the
isolated workspace is ready. Do **not** invent a second “ExecutionResult”
type by misreading that schema. The agent completion contract is
`agent-run-result`.

## Ownership boundaries

| Owner | Owns | Does not own |
| --- | --- | --- |
| **AMOF** | Authority, policy, write scope (propose/approve/bind/enforce), capabilities, evidence paths, result envelope fields, public lifecycle status / stop_reason semantics, private delivery-verdict projection | Model cognition / tool choice inside approved tools |
| **Worker** | Cognition and tool use inside the approved capability and path set | Source truth, mutation authority, evidence root, verdict semantics |
| **Substrate** | Containment and compute transport (Hermes↔Remote IAL vs Claude CLI↔Anthropic API, env stripping) | Governance policy |

**Containment today:** both governed backends declare
`isolation_model = runtime_owner_workspace` — **workspace-level only**.
Stronger session/run execution environments are listed as future isolation
models on the backend modules; they are not the current shipped containment.

Write-scope authority (propose → approve → bind → enforce → receipt) is part of
the AMOF-owned layer; see `docs/write-scope-authority.md`.

## Duck-typed backend contract

There is **no** Python ABC. Adapters are selected by runner `backend` metadata
and must provide:

1. `build_selection(...)` — capabilities, writable roots, timeout, readable
   root (optional write-scope binding id)
2. `run(manifest=, goal=, request_id=, studio_session_id=, selection=, …)` →
   dict matching `agent_run_result`

Shared governance helpers (prompt contract, write-scope proposal extraction,
read-only restore + replan, changed-path accounting) are reused across Hermes
and Claude Code so operator-facing results stay contract-identical. Details:
`scripts/amof/execution_backends/README.md`.

## Known uniformity gaps (do not paper over)

These are real gaps. They are **classification / enforcement** issues, not proof
that a new execution-boundary abstraction is missing.

1. **backend-flavored verdict recovery** — when structured worker findings are
   empty, private refusal / zero-write labeling can still lean on
   Hermes-shaped log heuristics. Same write truth may label differently across
   backends until recovery is backend-neutral.
2. **`max_loops` non-enforcement on handoff dispatch** — handoff backend
   selection enforces **timeout** (and budget fields on the request). Loop
   bounding is enforced by the separate `amof loop` command
   (`stop_reason=max_loops_reached`), not by `amof handoff execute-agent`
   dispatch into Hermes/Claude. Do not document handoff as if `max_loops` were
   already a backend kill-switch.
3. **Builtin structured-proposal discovery routing** — the builtin read-only
   structured-proposal path may still execute through the Hermes adapter while
   labeling `backend=amof_builtin_code` (attribution honesty gap).
4. **Schema plural drift (minor)** — backends may emit `write_scope_proposals`
   (plural) while older docs/schema text emphasize singular
   `write_scope_proposal`. Align emitters and schema; do not invent a new
   result type.

## What not to invent

- Top-level `ExecutionRequest` / `ExecutionResult` types or a “runtime
  authority bus” parallel to handoff + `AgentRunResult`
- Formal `Protocol`/`ABC` without behavioral change
- Promoting private `outcome_class` into the public `AgentRunResult` schema as
  a release gate (public already carries `status` / `stop_reason` /
  `failure_classification`)
- Claiming OS/sandbox or Firecracker-grade containment while
  `runtime_owner_workspace` is the live isolation model
- Treating `execution-handoff-result` as the agent completion contract

## Related public surfaces

- `contracts/canonical-mission-packet.schema.json`
- `contracts/agent-run-result.schema.json`
- `contracts/execution-handoff-result.schema.json` (workspace materialization only)
- `docs/write-scope-authority.md`
- `docs/governed-cognition-runtime.md`
- `docs/architecture/public-private-boundary.md`
- `scripts/amof/execution_backends/README.md`

# Read-Only Chat Planning

Status: public MVP

## Purpose

`amof chat plan` is the first bounded AMOF chat/planning surface that routes
one planning call through the active `remote-ial` provider profile and emits a
proposal-only `PlanBundle` for AMOF Director.

`PlanPacket` remains as a compatibility alias in runtime payloads, but the
canonical planning contract is now `plan_bundle`.

It is intentionally narrower than `amof agent`:

- planning only
- no repo mutation
- no shell execution from chat
- no editor integration
- no Director handoff execution
- no private gateway policy disclosure

## Command

```bash
amof chat plan "Plan AMOF-CHAT-001" --repo . --ticket-id AMOF-CHAT-001 --file README.md --file scripts/amof/cli.py
```

If `--file` is omitted, AMOF inspects a bounded set of top-level text files in
the target repo. For precise planning, pass explicit `--file` values.

## Output

The command prints a structured `PlanBundle` proposal plus transport/evidence
metadata. The canonical bundle is always non-executable and always carries:

- `requires_user_approval: true`
- `execution_allowed: false`
- a Director-facing prompt that says the packet is proposal-only

The packet includes:

- `ticket_id` or `proposed_ticket_id`
- `objective`
- `repo_scope`
- `files_to_inspect`
- `proposed_steps`
- `risks`
- `validation_plan`
- `execution_prompt_for_director`
- `requires_user_approval`
- `execution_allowed`

Optional cognition fields (Evolution rule; absent when unknown — never
fabricated):

- `confidence` — planner confidence in `[0, 1]` when the planner provides it
- `suggested_next_actions[]` — typed intake prefills
  (`{ label, prefill: { intake_text } }`); proposals only, never executable
  commands
- `interpretations[]` — role-bound interpretation entries (often empty until
  a Critic/Interpreter pass writes them)
- `dissent[]` — risk-gated Critic dissent entries (absent when critique did
  not run)

## Risk-gated Critic

After the planner PlanBundle is built, AMOF may run **at most one** Critic
pass over the same canonical planning context. The Critic has no tools, no
mutation, and no dialogue with the planner.

Trigger table (never always-on):

| Trigger | Critic |
|---|---|
| High mutation ceiling, prod-touching, or security-sensitive | ON by default |
| Low planner confidence (`< 0.5`) or contract disagreement | ON |
| Cheap read-only / explore work | OFF |
| Budget exhausted | OFF (degrade to supervised) |
| No positive risk signals supplied | OFF (`insufficient_risk_signals`) |

The gating decision is always evidenced:

- `events.jsonl` event `critic_gate_decision`
- `plan-result` evidence key `critic_gate`
- an `interpretations[]` entry with `role: "critic"` and
  `critique_ran:...` or `critique_skipped:...`

Callers may pass optional `risk_signals` into `plan_read_only_chat`
(`mutation_ceiling`, `prod_touching`, `security_sensitive`,
`planner_confidence`, `contract_disagreement`, `budget_exhausted`,
`explore_readonly`).

CLI (additive):

```bash
amof chat plan "<objective>" --repo . --risk-signals-json '{"mutation_ceiling":"runtime_mutation","prod_touching":true}'
```

When `--risk-signals-json` is omitted, Critic stays off
(`insufficient_risk_signals`) unless other positive signals are inferred.

## Evidence Behavior

Evidence is written to AMOF app-data only, never into the target repo.

All chat-plan evidence lives under the chat-plan run path:

- `~/.local/share/amof/runs/chat-plans/<session-id>/...`
- or the equivalent `AMOF_HOME` app-data root

`messages` evidence mode controls how the stored session and persisted
`plan-result.json` are written:

- `raw_local`
- `redacted_local`
- `hash_only`

`journal` evidence mode controls whether a shell-free chat journal is written
inside the same chat-plan run directory:

- `enabled`
- `redacted`
- `disabled`

To mirror the remote IAL proof posture, configure:

```yaml
evidence:
  messages: hash_only
  journal: disabled
```

## Boundaries

`amof chat plan` reads current bounded filesystem truth only. It does not
execute the proposal, does not mutate the repo, and does not transfer
execution authority to Director without an explicit later approval step.

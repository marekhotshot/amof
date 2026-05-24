# Read-Only Chat Planning

Status: public MVP

## Purpose

`amof chat plan` is the first bounded AMOF chat/planning surface that routes
one planning call through the active `remote-ial` provider profile and emits a
proposal-only `PlanPacket` for AMOF Director.

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

The command prints a structured `PlanPacket` proposal plus transport/evidence
metadata. The `PlanPacket` is always non-executable and always carries:

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

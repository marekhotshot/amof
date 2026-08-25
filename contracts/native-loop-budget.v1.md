# amof.native_loop_budget/v1

Canonical Native agent loop-budget policy for progress-aware bounded execution.

Policy version `native-loop-budget-v1.1` adds read-only evidence progress and a
single synthesis turn at base-budget exhaustion. Schema remains
`amof.native_loop_budget/v1`. Turn limits are unchanged.

## Fields (telemetry)

- `schema`: `amof.native_loop_budget/v1`
- `policy_version`: `native-loop-budget-v1.1`
- `base_turn_limit` (default 12)
- `extension_increment` (default 3)
- `max_extension_count` (default 2)
- `absolute_turn_limit` (default 18)
- `turns_used`
- `effective_turn_limit`
- `extension_count`
- `extensions_granted[]` / `extensions_denied[]`
- `progress_checks[]`
- `progress_fingerprint` (includes `evidence_coverage_digest`, `successful_evidence_count`)
- `synthesis_required` / `synthesis_consumed`
- `stop_reason`

## Progress classes

Machine-observable only. Model prose is never authority.

- `NO_PROGRESS` — repeated reads/listings of already-seen material, tool
  errors/path noise, failure churn, or no new coverage.
- `EVIDENCE_PROGRESS` — newly observed successful `read_file` / `list_dir` /
  `glob` coverage (distinct repository-relative observation keys). Absolute
  paths, `..` escapes, and failed tools do not count.
- `PARTIAL_PROGRESS` — write/grant or shell movement alone.
- `MATERIAL_PROGRESS` — implementation plus validation movement.

## Evidence progress

Authority is the distinct successful observation-key set:

- `read_file:<path>`
- `list_dir:<path>`
- `glob:<pattern>`

`evidence_coverage_digest` is the hash of the sorted key set.
`successful_evidence_count` is the set size. Repeating an already-seen key is
not progress even if `tool_outcome_signature` changes.

## Synthesis boundary (read-only)

At base-budget exhaustion, write-capable missions still require
`MATERIAL_PROGRESS` for a bounded exploration extension.

Read-only missions do **not** receive that extension. Lifetime evidence versus
an empty baseline is the authority (not the late turn-11 checkpoint):

- useful evidence (`successful_evidence_count > 0` and lifetime verdict in
  `EVIDENCE_PROGRESS` / `PARTIAL_PROGRESS` / `MATERIAL_PROGRESS`) →
  `SYNTHESIS_REQUIRED`: exactly one additional turn, tools stripped, model
  instructed to stop exploration and produce the final bounded result
- no useful evidence → `amof_native_base_budget_no_progress`

If the synthesis turn does not produce a tool-less final result:

- `amof_native_synthesis_not_completed`

Do not recycle generic `NO_PROGRESS` for a failed synthesis turn.

## Invariants

1. Execution remains deterministically bounded by `absolute_turn_limit`.
2. Write-mission extension requires `MATERIAL_PROGRESS` from machine-observable evidence.
3. Read-only base exhaustion with useful evidence grants one synthesis turn, not unlimited exploration.
4. Model self-report is never authority.
5. Stale progress cannot be reused across extensions.
6. Write authority is unchanged by budget evaluation.

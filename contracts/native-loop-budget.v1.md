# amof.native_loop_budget/v1

Canonical Native agent loop-budget policy for progress-aware bounded execution.

## Fields (telemetry)

- `schema`: `amof.native_loop_budget/v1`
- `policy_version`: `native-loop-budget-v1`
- `base_turn_limit` (default 12)
- `extension_increment` (default 3)
- `max_extension_count` (default 2)
- `absolute_turn_limit` (default 18)
- `turns_used`
- `effective_turn_limit`
- `extension_count`
- `extensions_granted[]` / `extensions_denied[]`
- `progress_checks[]`
- `progress_fingerprint`
- `stop_reason`

## Invariants

1. Execution remains deterministically bounded by `absolute_turn_limit`.
2. Extension requires `MATERIAL_PROGRESS` from machine-observable evidence.
3. Model self-report is never authority.
4. Stale progress cannot be reused across extensions.
5. Write authority is unchanged by budget evaluation.

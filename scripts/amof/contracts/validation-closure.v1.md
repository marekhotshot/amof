# amof.validation_closure/v1

Typed separation of **execution terminal state**, **validation gate outcomes**,
and **Mission acceptance**.

## Non-claims

- Execution `completed` is not Mission acceptance.
- Heuristic / prose “passed” is not per-gate executable evidence.
- `null` / missing validation is never treated as PASS.

## Fields

| Field | Meaning |
| --- | --- |
| `schema` | `amof.validation_closure/v1` |
| `execution_status` | `completed` \| `failed` \| `blocked` |
| `validation_status` | Aggregate of required gates / heuristic when no gates |
| `acceptance_state` | `PASS` \| `FAIL` \| `UNVERIFIED` \| `BLOCKED` \| `PARTIAL` |
| `required_count` / counters | Required gate tallies |
| `requirements[]` | `{validation_id, required, state, evidence_refs}` |
| `heuristic_status` | From `_infer_validation_status` (`passed`\|`failed`\|`not_run`) |
| `acceptance_evidence_refs` | Evidence supporting acceptance elevation |
| `notes` | Human-readable derivation notes |

## Acceptance rules (critical)

1. Any required gate `FAILED` → acceptance `FAIL`.
2. Else any required `BLOCKED` → `BLOCKED`.
3. Else any required `NOT_RUN` / `PENDING` / `UNKNOWN` → `UNVERIFIED`.
4. Else all required `PASSED` → `PASS`.
5. When `required_count == 0`:
   - heuristic `failed` → `FAIL`
   - heuristic `passed` → `PASS`
   - heuristic `not_run` → **`UNVERIFIED` (never PASS)**

## Agent run result embedding

`validation_summary` remains an open object. Preferred keys:

- `status` — legacy compat: `passed` \| `failed` \| `not_run`
- `reason`
- `acceptance_state`
- `closure` — full `amof.validation_closure/v1` object

Legacy mapping: `FAIL`→`failed`, `PASS`→`passed`, else→`not_run`.
**Never** map `UNVERIFIED` to `passed`.

## Post-verify

`attach_post_verify` returns a **new** closure with provenance
(`verified_by`, `verified_at`, `verification_mode`, evidence refs) without
mutating inputs. Historical execution fields remain caller-owned.

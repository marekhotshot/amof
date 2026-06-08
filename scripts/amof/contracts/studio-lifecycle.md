# Studio Lifecycle

Status: canonical

This document captures the bounded Studio ledger lifecycle and the files that
must remain portable across AMOF runtime surfaces.

Lifecycle:

1. `amof studio create` writes `session.json`, `events.jsonl`, `runs.json`, and
   `checkpoints.jsonl`.
2. `amof agent ... --studio-session <studio_session_id>` validates the Studio
   Session before provider execution, then emits child run artifacts with
   optional `studio_session_id`.
3. The shared run path records one idempotent `run.attached` event in the Studio
   ledger and one run reference entry in `runs.json`.
4. `amof studio checkpoint add` appends checkpoint objects to
   `checkpoints.jsonl` and adds a ledger event.
5. `amof studio end` marks the session ended without rewriting historical child
   runs.

Artifact truth:

- `session.json` is validated by `studio-session.schema.json`.
- `events.jsonl` contains one event object per line validated by
  `studio-event.schema.json`.
- `runs.json` contains an array of run references whose items validate against
  `studio-run-reference.schema.json`.
- `checkpoints.jsonl` contains one checkpoint object per line validated by
  `studio-checkpoint.schema.json`.
- `AgentRunResult` may optionally carry `studio_session_id`; that field remains
  additive and legacy run artifacts without Studio correlation remain valid.

Compatibility rules:

- `run.attached` is the canonical Studio child-run event.
- `studio_run_attached` remains accepted by the event schema as a legacy alias
  for previously written local artifacts.
- `run_id` remains the idempotent attachment identity inside one
  `studio_session_id`.
- No historical backfill or mutation of legacy child run artifacts is allowed.

Safety rules:

- These schemas prohibit secrets, raw provider credentials, auth headers, and
  similar sensitive fields.
- Studio ledger artifacts may point to child run artifacts, but they must not
  duplicate the entire child event stream.

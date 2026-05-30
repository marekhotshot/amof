# Runtime Logs Contract

This document defines the minimal runtime logs contract for AMOF planning runs.

Scope: contract-first runtime log structure for `amof chat plan`, including
minimal-context mode. This is not a dashboard, console, or observability
platform implementation.

## Event Stream

Each run writes append-only JSONL at:

- `<session_dir>/events.jsonl`

The stream is validated as lifecycle events with required metadata.

## Required Event Metadata

Every event line must include:

- `event_id`
- `run_id`
- `session_id`
- `timestamp`
- `event_type`
- `severity`
- `actor`
- `ticket_id` (when provided by command arguments)
- `planning_mode`
- `context` (resolved runtime context, for example `local`, `cloud-dev`, `msg-aws-dev`)

Legacy aliases (`ts`, `type`) may remain for backward compatibility.

## Required Lifecycle Events (Minimal Chat Plan Run)

Required event set:

1. `run_created`
2. `planning_mode_selected`
3. `context_file_loaded` (for each explicit bounded `--file`)
4. `ial_request_started`
5. `ial_request_finished`
6. `planning_context_receipt_written`
7. `run_finished`

Ordering constraints:

- `run_created` must appear before request events.
- `ial_request_started` must appear before `ial_request_finished`.
- `run_finished` must be the final lifecycle marker for the run.

## Cost Truth Contract

- `cost_status` values are `observed` or `unknown`.
- When `cost_status=observed`, `estimated_cost` may be non-null.
- When `cost_status=unknown`, `estimated_cost` must be null/absent in event
  payloads (never fake `0.0` truth).

Related fields:

- `tokens_in` (nullable)
- `tokens_out` (nullable)

## Receipt Reference Contract

When planning context receipt is written, event payload must include a stable
reference:

- `receipt_ref`

`run_finished` should also carry a receipt reference when available.

## Public Surface Safety

Runtime event logs must not expose:

- secrets (API keys, bearer tokens)
- raw prompts/completions
- raw provider payloads
- raw provider generation ids

Hash-safe references such as provider generation refs are allowed where
required by existing cost-truth behavior.

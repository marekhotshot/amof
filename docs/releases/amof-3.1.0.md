# AMOF v3.1.0

Status: release candidate notes for the current public line
Canonical version: `v3.1.0`
Previous release: `v3.0.3`

AMOF `v3.1.0` packages the public capability stack accumulated since `v3.0.3`
around canonical planning and execution contracts, governed handoff execution,
truthful runtime reporting, and an experimental Studio Session ledger for
correlating governed runs, checkpoints, and evidence.

## Highlights

- Canonical planning and execution contracts
- Governed handoff-to-agent execution
- Truthful planner recovery and clarification handling

## Experimental

- Experimental Studio Session ledger
- Agent and handoff correlation through `studio_session_id`
- Versioned Studio schemas and lifecycle artifacts

Studio is positioned as: Experimental Studio Session ledger for correlating
governed runs, checkpoints, and evidence.

## Reliability

- bounded semantic planner retries
- truthful unknown-cost handling
- stale-base promotion protection
- deterministic execution and handoff evidence

## Compatibility

- legacy handoffs without `studio_session_id` remain valid
- Studio is optional
- no automatic Studio Session creation
- no browser/userscript integration included in this release

## Known limitations

- detached checkouts require adoption knowledge
- raw Studio `runs.json` is attachment-time ledger truth
- browser UX for Studio correlation remains private/operator-side
- no transcript synchronization or active-session discovery

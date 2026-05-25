# Ticket Delivery Protocol

Status: canonical
Scope: `AMOF-280`

## Purpose

This document defines the AMOF ticket-level delivery protocol.

`AMOF-280` does not create a new workflow surface. It consolidates ownership of
existing surfaces and hardens how one bounded ticket moves from planning into
validated delivery.

## Ownership Consolidation

AMOF workflow ownership is split as follows:

- `chat`: intake, clarification, and proposal-only `PlanPacket` shaping
- `agent`: approved execution path
- `ticket`: delivery lifecycle, worktree creation/switching, `PlanItems`,
  checkpoint commits, validation receipts, and readiness state
- `promote-main`: final governed delivery to canonical main
- `UltraPlan`: future multi-ticket planning/controller layer above ticket
  delivery; not part of `AMOF-280`

If an implementation step creates another parallel workflow, duplicates one of
these responsibilities, or blurs these boundaries, the change is out of scope
for `AMOF-280`.

## Planning Hierarchy

AMOF planning hierarchy:

```text
UltraPlan
-> TicketPlan
-> PlanItems
-> Checkpoint commits
-> promote-main
-> fresh verification
```

Ownership by layer:

- `UltraPlan` owns strategic objective, repo scope, public/private boundary
  constraints, ticket decomposition, ticket dependency graph, ticket sequencing,
  deferred/killed/replayed ticket decisions, cross-ticket compatibility risks,
  and final program-level evidence.
- `TicketPlan` owns one bounded delivery slice: canonical repo preflight, clean
  worktree creation from canonical main, `PlanItems`, validation-backed
  checkpoints, final validation, promote readiness, and fresh-clone verification
  state.
- `PlanItems` are ticket-scoped only. They do not replace ticket decomposition.
- checkpoint commits are the smallest resumable evidence unit inside one ticket.

A long run may span multiple tickets over time, but each ticket must keep its
own branch, worktree, validation boundary, promotion boundary, and verification
boundary.

## Canonical Repo Rules

For `AMOF-280`, the authoritative canonical repo rule is:

- repo id `amof` must resolve to `https://github.com/marekhotshot/amof.git`
- repo id `amof-oss` is forbidden as a delivery target
- ticket start must use the current canonical `origin/main`
- dirty historical worktrees are evidence only and must not be reused as ticket
  start state

The published root workspace remains lineage/contracts/audit truth only. Product
implementation changes belong in the canonical AMOF repo or a clean ticket
worktree created from it.

## Ticket Lifecycle

Required ticket flow:

```text
preflight -> start -> status -> checkpoint -> readiness -> promote-main -> fresh verification
```

Behavior by stage:

1. `preflight`
   - verify canonical repo identity
   - reject blocked repos such as `amof-oss`
   - verify `origin/main` is resolvable
   - verify ticket start conditions are clean enough for a bounded delivery run
2. `start`
   - create one clean ticket worktree per selected writable repo
   - branch from canonical `origin/main`
   - persist the `TicketPlan` and initial receipts
3. `status`
   - report ticket-local truth only
   - show `PlanItems`, validation state, checkpoint readiness, and promote
     readiness
4. `checkpoint`
   - create a ticket-local git commit only after required validation passes
   - stage explicit files only
   - reject unrelated dirty files
5. `readiness`
   - record that all required `PlanItems` are done, deferred, or killed with
     rationale
   - report readiness for `promote-main`, but do not run promotion
6. `promote-main`
   - handled by the existing promotion command
7. `fresh verification`
   - record post-promotion fresh-clone verification outcome as ticket evidence

## PlanItems

Each ticket starts with explicit `PlanItems`.

Minimum `PlanItem` fields:

- `id`
- `type`
- `title`
- `expected_files`
- `validation`
- `checkpoint_required`

Example:

```yaml
P1:
  type: CONTRACT
  title: Define ticket receipt schema
  expected_files:
    - scripts/amof/commands/ticket.py
    - tests/test_ticket_delivery_protocol.py
  validation:
    - python -m unittest tests.test_ticket_delivery_protocol
  checkpoint_required: true
```

`PlanItems` stay ticket-local. A large body of work should be decomposed into
multiple tickets under a future `UltraPlan`, not flattened into one giant
ticket branch.

## Checkpoint Policy

Checkpoint commits are allowed only when all of the following are true:

- the checkpoint references one or more `PlanItem` ids
- the referenced `PlanItems` are completed or ready to close
- required validation for those `PlanItems` has passed
- staged files are explicit and within the referenced `PlanItem` scope
- there are no unrelated dirty files
- the commit materially improves resume or review safety

Checkpoint commit message format:

```text
[AMOF-280][P2] add ticket start worktree creation
```

Forbidden checkpoint patterns:

- `git add .`
- checkpointing with failing validation
- checkpointing while core behavior is still TODO
- checkpointing mixed unrelated files
- checkpointing as generic save-progress

If recovery evidence is needed before validation is ready, write a receipt in
AMOF app-data instead of creating a git commit.

## Receipt Taxonomy

`AMOF-280` does not implement a full `UltraPlan` engine, but it must structure
ticket evidence so a future controller can consume it directly.

Ticket-level receipt classes:

- `preflight_receipt`
- `ticket_start_receipt`
- `validation_receipt`
- `checkpoint_receipt`
- `readiness_receipt`
- `fresh_verify_receipt`

Each ticket should retain stable identifiers for:

- ticket id
- repo name
- branch name
- worktree path
- plan item ids
- validation commands and outcomes
- deferred/killed rationale

## Explicit Non-Goals

`AMOF-280` must not:

- introduce a new command family
- change planner/model/provider semantics
- implement indexed or Merkle-backed chat context
- implement chat-to-agent handoff
- wrap or replace `promote-main`
- introduce another generic run or execution abstraction
- implement a full `UltraPlan` engine
- let one long mutable branch absorb unrelated ticket scopes

## Acceptance Frame

`AMOF-280` is complete only when:

- ticket delivery is hardened without creating another workflow surface
- ticket state is structured for future `UltraPlan` consumption
- every ticket remains a bounded delivery unit with its own branch/worktree,
  validation, promotion, and verification boundary

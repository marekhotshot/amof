# AMOF v3.2.0

Status: release candidate notes for the current public line
Canonical version: `v3.2.0`
Previous release: `v3.1.1`

AMOF `v3.2.0` is a governed handoff truthfulness release. It packages the
already-promoted public `main` stack since `v3.1.1` — additive contracts,
structured write-scope proposals, a second governed runner (Claude Code),
critic/cognition receipt fields, Hermes cost accounting, runner/write guards,
and plan/execute hardening — without changing the public product into a new
execution paradigm.

## Highlights

- Canonical mission packet contract and public handoff carrier for bounded
  mission transport
- Structured write-scope proposal emission on governed results
- Claude Code execution backend beside Hermes, with multi-target dispatch
- Risk-gated Critic and optional PlanBundle cognition receipt fields
- Hermes Remote IAL usage/cost accounting on terminal envelopes
- Native plan-execute provider/model/transport provenance (BL-047)

## Reliability

- read-only mutation policy preservation at the Hermes workspace boundary
- canonical-repo write guard and doctor hygiene
- truthful unclean-workspace blocker copy (modified and/or untracked)
- workspace path aliasing, ToolProposal path normalization, and bounded-auto
  capability clamp (BL-027/037/045/046/065/070–073)
- tool-failure semantics, write-advisory nonfatal recovery, and promote-main
  candidate-delta safety
- contract-conformance repairs: `AgentRunResult` serializes nullable
  `proposal_missing_reason` for example round-trip; remote-ial provider-profile
  example template present; runner-authority match golden aligned to current
  `backend` / `dispatch_available` candidate shape

## Compatibility

- existing prepared handoffs and Studio-optional flows remain valid
- contracts in this release are additive (`+489/−0` at the public contract root)
- no CLI file removals in the `v3.1.1..v3.2.0` public delta
- install/update pins move to `v3.2.0`

## Known limitations

- browser/userscript and operator-console UX remain private/operator-side
- live Remote IAL access remains an external prerequisite for end-to-end Hermes
  smoke
- this public release does not claim sandbox/substrate containment, hard
  duration or loop-budget authority, or EU AI Act compliance/certification
- private operative-mission carrier and Predator acceptance surfaces are not
  part of this public artifact

# AMOF-ULTRAPLAN-300

Status: UltraPlan (planning-only)  
Release: AMOF 3.0 — Runtime Authority Release

## Goal

Produce planning/contract artifacts only. No runtime implementation is allowed in this ticket.

## Planning Hierarchy

`UltraPlan -> TicketPlan -> PlanItems -> promote-main -> fresh verification`

## Track Plan

| Track | Current repo/runtime inventory | Classification | Proposed minimal ticket | Acceptance criteria | Validation command | Out-of-scope | Risk level | Public/private boundary impact |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| runtime logs | Public runtime telemetry surfaces exist in `scripts/amof/orchestrator/telemetry.py` and `scripts/amof/orchestrator/events.py` with cost truth fields recently promoted | required | `AMOF-RUNTIME-LOGS-CONTRACT-001` | Runtime logs contract freezes nullable cost and `cost_status` semantics | `python3 tests/test_remote_ial.py` | Logging backend redesign | medium | Public logs expose safe truth fields only |
| config/profile layer | Public profile templates exist; runtime authority default profile contract still missing | blocker | `AMOF-CONFIG-LAYER-MVP-001` | Config/profile layer chooses authority profile without secret material | `amof setup provider --list` | Broad config platform | high | Env var names public; secrets remain private |
| cloud workspace context | Operator workspace has runbooks and deployment receipts proving cloud-dev path | cleanup | `AMOF-REMOTE-EXECUTION-SCAN-REPORT-001` | Cloud context boundaries documented for runtime authority | `python3 scripts/amof.py doctor --json` | Cloud infra migration | medium | Public docs reference boundaries, not private topology internals |
| local CLI intake | Public CLI has intake-adjacent ticket/chat contracts but no dedicated runtime-authority intake example | required | `AMOF-CLI-INTAKE-001` | CLI intake contract documented and validated for bounded planning | `python3 scripts/amof.py ticket --help` | Voice capture | medium | Public intake contract only |
| console intake | Console-facing contract exists operationally but not fully captured in release roadmap contract | required | `AMOF-CONSOLE-INTAKE-001` | Console intake contract mapped to safe runtime authority surfaces | `python3 scripts/amof.py chat --help` | Dashboard analytics | medium | No unsafe private data in public docs |
| installable CLI | Public install/runbooks exist and are fresh-clone validated | required | `AMOF-RUNS-CLI-001` | Installable CLI path includes runtime authority workflow framing | `python3 scripts/amof.py --version` | Packaging rewrite | low | Public installation guidance only |
| remote execution | Remote IAL client path exists; cost-truth prerequisite closed and promoted | required | `AMOF-REMOTE-EXECUTION-SCAN-REPORT-001` | Remote execution evidence contract includes sanitized receipts and truthful cost status | `python3 tests/test_remote_ial.py` | New runner architecture | medium | Safe references only; no unsafe provider payload |
| context switching | Context switching contract is not yet formalized in public release docs | blocker | `AMOF-RUNTIME-CONTEXT-SWITCHING-001` | Explicit rules prevent unsafe/silent fallback behavior | `python3 scripts/amof.py status` | Automatic context orchestration | high | Must enforce no silent remote->local fallback |
| remote IAL usage/cost truth | `AMOF-REMOTE-IAL-OPENROUTER-COST-TRUTH-001` closed with promoted main `feaa393...` and accepted smoke run `run-20260529-220052` | required | `AMOF-REMOTE-IAL-OPENROUTER-COST-TRUTH-001` | `cost_status=observed|unknown` semantics are release-gated and truthful | `python3 tests/test_remote_ial.py` | Provider policy expansion | low | `provider_generation_ref` safe public; raw id remains private |
| receipts/evidence | Promote-main and fresh-clone evidence pipeline already operational | required | `AMOF-300-RELEASE-CLOSEOUT-001` | Release closeout records promotion, fresh verification, and residual risks | `scripts/fresh-clone-verify.sh <sha> --ticket <id>` | Analytics dashboards | medium | Public evidence remains hash-safe/sanitized |

## Required Ticket Sequence

1. `AMOF-REMOTE-IAL-OPENROUTER-COST-TRUTH-001`
2. `AMOF-CONFIG-LAYER-MVP-001`
3. `AMOF-RUNTIME-LOGS-CONTRACT-001`
4. `AMOF-RUNS-CLI-001`
5. `AMOF-RUNTIME-CONTEXT-SWITCHING-001`
6. `AMOF-INTAKE-CONTRACT-001`
7. `AMOF-CLI-INTAKE-001`
8. `AMOF-CONSOLE-INTAKE-001`
9. `AMOF-RUNNER-REGISTRATION-001`
10. `AMOF-REMOTE-EXECUTION-SCAN-REPORT-001`
11. `AMOF-300-RELEASE-CLOSEOUT-001`

## Definition Of Done

- Four public artifacts exist in this ticket worktree:
  - `docs/releases/amof-3.0-runtime-authority.md`
  - `docs/roadmap/AMOF-ULTRAPLAN-300.md`
  - `examples/profiles/remote-ial-openrouter.yaml`
  - `examples/intake/amof-self-scan.yaml`
- Optional operator receipt may exist outside the public repo.
- No runtime implementation is introduced in this ticket.
- No unsafe provider/private leakage appears in public artifacts.
- Final report states one next single implementation slice.

## Product Finding For Next Slice

Record under `AMOF-CONFIG-LAYER-MVP-001`:

`amof chat plan needs a safe minimal-context/no-index mode for bounded operator planning runs.`

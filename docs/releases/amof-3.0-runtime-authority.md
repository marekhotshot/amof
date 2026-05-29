# AMOF 3.0 — Runtime Authority Release

Status: planned  
Canonical version: v3.0.0  
Code name: AMOF-ULTRAPLAN-300  
Date: 2026-05-29  
Related:
- docs/roadmap/AMOF-ULTRAPLAN-300.md
- docs/governed-cognition-runtime.md

## Release Thesis

AMOF owns runtime truth; cognition workers are replaceable.

## Prerequisite Gate

Required prerequisite: `AMOF-REMOTE-IAL-OPENROUTER-COST-TRUTH-001`

Accepted evidence:
- promoted public main: `feaa393fb7fc73eab260eea5b23c6f9a013cb887`
- smoke run: `run-20260529-220052`
- request id: `f5701fd4-61ab-401a-9371-7c3c1e2909c6`
- verdict: `CLIENT_IAL_SMOKE_CONTRACT_OK`
- `cost_status=observed`
- `estimated_cost=0.0003426`
- `prompt_tokens=1456`
- `completion_tokens=207`
- `provider_usage.cost=0.0003426`
- `provider_usage.total_tokens=1663`
- private generation identifier present in private evidence
- safe `provider_generation_ref` present
- console receipt sanitized/hash-safe

## Ordered Ticket Sequence

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

## Per-Track Summary

| Track | Current inventory | Classification | Proposed minimal ticket | Acceptance criteria | Validation command | Out-of-scope | Risk | Boundary impact |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| runtime logs | Session and event telemetry exists in `scripts/amof/orchestrator/telemetry.py` and `scripts/amof/orchestrator/events.py` | required | `AMOF-RUNTIME-LOGS-CONTRACT-001` | Public runtime emits stable cost truth fields and unknown-cost semantics | `python3 tests/test_remote_ial.py` | New execution engine | medium | Public contract only, no private internals |
| config/profile layer | Provider templates exist under `templates/provider-profiles/` | blocker | `AMOF-CONFIG-LAYER-MVP-001` | Profile/config contract chooses runtime authority defaults safely | `amof setup provider --list` | Full config migration | high | Public env-var-only references |
| cloud workspace context | Operator runbooks and cloud-dev receipts exist in operator workspace | cleanup | `AMOF-REMOTE-EXECUTION-SCAN-REPORT-001` | Runtime authority docs map cloud context boundaries | `python3 scripts/amof.py doctor --json` | Cluster redesign | medium | Public/private split explicitly documented |
| local CLI intake | Intake contract examples are JSON under `contracts/examples/` | required | `AMOF-CLI-INTAKE-001` | Local intake path is contractized and validation-backed | `python3 scripts/amof.py ticket --help` | Voice intake | medium | Public intake schema only |
| console intake | Operator console exists privately; public contract needs intake boundary | required | `AMOF-CONSOLE-INTAKE-001` | Console intake contract references safe surfaces only | `python3 scripts/amof.py chat --help` | Dashboard analytics | medium | No private gateway leak |
| installable CLI | Install and first-run guidance already in `README.md`/runbooks | required | `AMOF-RUNS-CLI-001` | Installed CLI exposes runtime authority workflow safely | `python3 scripts/amof.py --version` | Installer rewrite | low | Public install contract only |
| remote execution | Remote IAL path and receipts are proven by cost-truth smoke | required | `AMOF-REMOTE-EXECUTION-SCAN-REPORT-001` | Remote run evidence contract recorded without unsafe payloads | `python3 tests/test_remote_ial.py` | Runner implementation | medium | Hash-safe references only |
| context switching | No finalized runtime context-switch contract yet | blocker | `AMOF-RUNTIME-CONTEXT-SWITCHING-001` | Explicit runtime context switch rules and guardrails documented | `python3 scripts/amof.py status` | Auto-fallback behavior | high | Must forbid silent local fallback |
| remote IAL usage/cost truth | Cost truth promoted on main and smoke evidence accepted | required | `AMOF-REMOTE-IAL-OPENROUTER-COST-TRUTH-001` (closed) | Missing provider cost never treated as truth `0.0`; `cost_status` persisted | `python3 tests/test_remote_ial.py` | Provider policy expansion | low | `provider_generation_ref` public-safe, raw id private |
| receipts and evidence | Receipts taxonomy and fresh-clone verification already used in operations | required | `AMOF-300-RELEASE-CLOSEOUT-001` | Release closeout links promotion and fresh-clone proof | `scripts/fresh-clone-verify.sh <sha> --ticket <id>` | Analytics pipeline | medium | Public receipts remain sanitized |

## Explicit Non-Goals

- no Jira sync
- no voice intake
- no agent arena authority
- no dashboard analytics
- no fake `cost: 0.0` for missing provider cost
- no silent local fallback

## Validation

- Verified prerequisite promotion SHA on canonical public main: `feaa393fb7fc73eab260eea5b23c6f9a013cb887`.
- Verified accepted smoke evidence bundle includes observed cost truth and hash-safe receipt surfaces.
- Verified this release slice creates planning/contract artifacts only and introduces no runtime behavior change.
- Verified public artifact scope remains documentation/examples and excludes private provider internals.

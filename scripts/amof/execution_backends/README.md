# Execution backends

AMOF-owned adapters for governed handoff dispatch. Package marker:
`__init__.py` (“adapters owned by AMOF authority”).

## Duck-typed contract (no ABC)

Handoff selects a module by runner backend type and calls:

| Hook | Role |
| --- | --- |
| `build_selection(...)` | Build the authority slice: runner id, approved capabilities, writable roots, timeout, readable root; optional write-scope binding id |
| `run(manifest=, goal=, request_id=, studio_session_id=, selection=, provider=, model=)` | Execute and return an `agent_run_result` dict (`result_kind` / `agent-run-v1`) |

Dispatch entry point: `scripts/amof/commands/handoff.py` →
`_dispatch_backend_handoff`.

## Shipped adapters

| Module | `BACKEND_TYPE` | Substrate notes |
| --- | --- | --- |
| `hermes_opensandbox.py` | `hermes_opensandbox` | Hermes CLI + Remote IAL |
| `claude_code.py` | `claude_code` | Claude Code CLI (headless) + Anthropic API; reuses shared governance helpers from the Hermes module |

Both declare `isolation_model = runtime_owner_workspace` (workspace-level
containment). Stronger isolation models are listed as future options on the
modules and are not current shipped containment.

When no runner / builtin path is selected, handoff may use the builtin
plan-execute envelope instead of these modules; that path still emits
`agent_run_result` but with different evidence density.

## Ownership reminder

- **AMOF** owns selection, capabilities, writable roots, prompt governance,
  write-scope markers, changed_paths accounting, and the result envelope.
- **Worker** owns cognition inside approved tools.
- **Substrate** owns containment/compute transport differences.

See `docs/canonical-execution-chain.md` for the full chain map and known
uniformity gaps (including backend-flavored verdict recovery and handoff
`max_loops` non-enforcement).

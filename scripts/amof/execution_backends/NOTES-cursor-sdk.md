# Cursor SDK execution backend — limitations (AMOF-BL-193)

Worker / delegated implementation backend only. **Not** Runtime Authority.

## Architecture

| Plane | Cursor SDK allowed? |
| --- | --- |
| Runtime Authority / IAL gateway / operator-console core | **No** — must not import `cursor_sdk` / `@cursor/sdk` |
| `scripts/amof/execution_backends/cursor_sdk.py` | **Yes** — sole AMOF import boundary (lazy) |

- `CURSOR_API_KEY` is a **substrate secret** (process env). Not an RA config surface.
- AMOF `run_id` / `session_id` are minted as `cursor-<timestamp>-<safe_id>`.
- Cursor `agent_id` and run `id` are recorded as `evidence_refs.substrate_agent_id` /
  `substrate_run_id` only.

## Defaults

- `setting_sources` defaults to **empty** (service use; no IDE ambient bleed).
- Override only via `AMOF_CURSOR_SDK_SETTING_SOURCES` (comma list) when intentionally
  loading project/user/team settings.
- Optional gate: `AMOF_CURSOR_SDK_ENABLED=0` blocks dispatch while keeping the
  backend discoverable via runner template `cursor-sdk`.

## Limitations / residuals

1. **Non-K8s cloud VM** — Cursor cloud agents run on Cursor-hosted VMs, not AMOF
   Job/K8s transport. Slice-1 uses **local** `Agent.create` + `cwd` only.
2. **Billing** — SDK runs bill against the Cursor account / request pool; AMOF
   does not meter this as Remote IAL spend.
3. **IDE bleed** — empty `setting_sources` mitigates ambient settings; operators
   must not set `AMOF_CURSOR_SDK_SETTING_SOURCES=all` for unattended service use.
4. **Evidence weaker unless normalized** — SDK transcripts/tool payloads are
   less dense than Hermes/Claude CLI envelopes; normalize into `agent_run_result`
   before treating as approval-grade.
5. **Live dogfood** — requires `CURSOR_API_KEY` + `pip install cursor-sdk`
   (`optional-dependencies.cursor`). Absent key → residual, not a unit-test fail.

## Install

```bash
pip install 'amof[cursor]'   # or: pip install cursor-sdk
export CURSOR_API_KEY=...
amof runner template --kind cursor-sdk
```

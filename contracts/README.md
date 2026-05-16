# Contracts

Status: canonical

This directory captures the retained contract set for the canonical bootstrap
and contract-first slice.

The retained contract families are:

- director intake and handoff contracts used by the current source-first flow
- governed workstation bootstrap schemas used by `amof doctor` and
  `amof bootstrap *`
- examples that prove the bootstrap contract statuses and director intake
  shapes

Active contract set:

- `director-intake-client-contract.md`
- `director-intake-execution-contract.schema.json`
- `director-plan-result.schema.json`
- `workspace-receipt.schema.json`
- `execution-handoff-result.schema.json`
- `governed-workstation-bootstrap-contract.schema.json`
- `bootstrap-source-checkout-receipt.schema.json`
- `bootstrap-toolchain-receipt.schema.json`
- `bootstrap-provider-configuration-receipt.schema.json`
- `bootstrap-failure-receipt.schema.json`
- `up10-bootstrap-summary.schema.json`
- `bootstrap-sha256-manifest.schema.json`
- `examples/director-intake-dirty-workspace.example.json`
- `examples/director-intake-source-fix-ticket.example.json`
- `examples/governed-workstation-bootstrap-pass.example.json`
- `examples/governed-workstation-bootstrap-warn.example.json`
- `examples/governed-workstation-bootstrap-blocked.example.json`

Principles for new work in this directory:

1. Prefer machine-readable schemas and examples over narrative runtime playbooks.
2. Keep source-workspace truth separate from AMOF app-data truth.
3. Treat bootstrap evidence paths and contract names as compatibility surfaces.

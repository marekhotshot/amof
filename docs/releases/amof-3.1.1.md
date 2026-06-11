# AMOF v3.1.1

Status: release candidate notes for the current public line
Canonical version: `v3.1.1`
Previous release: `v3.1.0`

AMOF `v3.1.1` packages the async handoff closeout required to make long-running Hermes work survive the client boundary. The public release now exposes canonical asynchronous acceptance and status polling, materializes a terminal `AgentRunResult` envelope for every Hermes terminal path, and preserves provider/model/transport provenance for operator surfaces that consume the result.

## Highlights

- `amof handoff accept-agent` returns immediate governed acceptance for prepared AMOF-agent handoffs
- `amof handoff status` projects canonical lifecycle truth across active and terminal execution states
- Hermes terminal runs always emit a canonical `AgentRunResult` envelope with recovery-boundary-safe metadata

## Reliability

- blocked, failed, timed-out, cancelled, and missing-result Hermes outcomes now materialize canonical terminal receipts
- runtime summary text is AMOF-authored and no longer depends on agent output shape
- task findings are preserved separately from AMOF-owned runtime envelope fields

## Compatibility

- existing prepared handoffs remain valid
- operator/browser surfaces can migrate from local filesystem scraping to canonical `handoff status`
- `exit_code` now allows the recovery-boundary value `"unknown"` when a terminal envelope must be reported without a numeric exit code

## Known limitations

- browser/userscript and operator-console UX remain private/operator-side integrations
- live Remote IAL access is still an external prerequisite for end-to-end Hermes smoke
- the public release does not bundle private cockpit code

# Director Intake Client Contract

Status: canonical
Date: 2026-05-12

## Source Of Truth

AMOF owns the intake protocol. Editors, chat surfaces, CLIs, and future
UIs are clients that may help an operator capture intent, inspect
context, and prepare a bounded execution handoff, but they are not the
authority for runtime truth, source truth, or mutation policy.

The authoritative machine-readable output of intake is
`director_intake_execution_contract`, defined by
`director-intake-execution-contract.schema.json`.

## Drift That Exists Today

- Rough planning, workspace reading, and executor handoff are spread
  across prompts, operator habits, and editor-specific sessions.
- There is no shared contract for separating source truth, runtime
  truth, and workspace truth before execution starts.
- Read-only planning and execution can blur together, especially in
  editor-native agent experiences that make mutation feel implicit.
- Zed, Cursor, and future VS Code clients can explore the repo, but
  there is no single envelope that says what they are allowed to
  conclude, what they must refuse, and what an executor should receive.
- There is no stable comparison artifact for evaluating multiple intake
  clients against the same task.

## Expected Behavior

### 1. Intake is planning-only

An intake client converts rough operator intent into a bounded execution
contract. Intake is not execution.

An intake client MAY:

- read repository and workspace context
- inspect visible dirty state and local artifacts
- read documented runtime evidence and audit receipts
- ask clarifying questions
- classify risk and ambiguity
- produce a next executor prompt

An intake client MUST NOT:

- edit files
- build, deploy, release, or promote
- push commits or mutate remote systems
- mutate runtime surfaces directly or indirectly
- silently convert unknowns into assumed truth

### 2. Truth domains are separate

Every intake result MUST classify findings into three truth domains:

- `source_truth`: what is backed by canonical implementation sources and
  durable contracts
- `runtime_truth`: what is backed by fresh runtime evidence, live
  readback, or explicit operational receipts
- `workspace_truth`: what is visible in the current working tree,
  untracked files, local config, and operator shell state

These domains MUST NOT be collapsed into a single summary. If a fact is
not proven in the relevant domain, intake must say so explicitly.

### 3. Output must be bounded

The intake result MUST validate against
`director-intake-execution-contract.schema.json` and include:

- the bounded goal and operator-facing ticket summary
- the inspected scope and evidence sources
- the three truth domains
- allowed mutations
- forbidden mutations
- validation gates that must pass before execution
- stop conditions and ambiguities
- a risk classification
- an executor disposition
- the exact next executor prompt

The disposition is one of:

- `replay_now`
- `replay_later`
- `defer`
- `kill`

### 4. Mutation policy must be explicit

Every intake result MUST declare what the eventual executor is allowed
to mutate and what it is forbidden to mutate.

The forbidden set MUST include runtime-bearing production or dev control
surfaces unless the task explicitly authorizes them and the intake
result carries the validation gates needed for that authority transfer.

If the operator intent would require runtime or production mutation but
the required authority is missing, the intake client MUST stop with
`disposition: defer` or `disposition: kill`.

### 5. Ambiguity stops execution

Intake clients must prefer refusal over guesswork.

If any of the following is true, the result MUST include at least one
ambiguity and a matching stop condition:

- the canonical source-of-truth repo or path cannot be established
- the runtime evidence is stale, contradictory, or missing
- the workspace is dirty in a way that changes task classification
- the requested task mixes planning and mutation without a clear cut
- the intended target environment or service cannot be named precisely

An empty `ambiguities` list is only valid when the client can explain why
execution may proceed within the declared bounds.

### 6. Validation gates are first-class

The contract must declare what an executor has to prove before any
mutation begins. Typical gates include:

- confirm canonical repo/path ownership
- confirm fresh runtime evidence or explicitly mark runtime as unknown
- confirm dirty workspace classification
- confirm secret/material access expectations
- confirm scope-limited mutation paths
- confirm acceptance checks for the requested task

Validation gates belong to intake even when the executor will be a
different tool or client.

### 7. Editor clients are adapters, not authorities

VS Code, Zed, Cursor, and future AMOF UI surfaces may implement this
contract in different UX forms, but they all emit the same intake
envelope. No editor-specific behavior may redefine the contract.

This lets AMOF compare client quality without making a specific editor
the system of record.

### 8. Future AMOF bridge surfaces

Editor clients should ultimately hand the same intake contract through
stable AMOF-owned surfaces:

- CLI
- HTTP API
- MCP server
- execution contract files

The bridge surface is future protocol plumbing. It must not hardwire the
business rules of intake into any one editor integration.

## Acceptance Checks

- A planning-only intake result validates against
  `director-intake-execution-contract.schema.json`.
- The result distinguishes `source_truth`, `runtime_truth`, and
  `workspace_truth` and does not silently merge them.
- The result declares both `allowed_mutations` and
  `forbidden_mutations`.
- The result contains at least one validation gate before any executor
  handoff is considered complete.
- Ambiguous or under-evidenced tasks produce explicit stop conditions
  instead of guessed execution instructions.
- The result includes one final executor prompt that stays within the
  declared mutation bounds.
- No client that claims compliance with this contract silently mutates
  production or runtime surfaces during intake.

## Related Artifacts

- `director-intake-execution-contract.schema.json`
- `examples/director-intake-dirty-workspace.example.json`
- `examples/director-intake-runtime-deploy-failure.example.json`
- `examples/director-intake-source-fix-ticket.example.json`
- `director-delivery-contract.md`
- `inference-authority-layer.md`

## Out Of Scope

- Delivery execution, promotion, and release mechanics
- Editor installation and account provisioning
- A shared benchmark arena for clients before real execution receipts
  exist

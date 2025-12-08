# AMOF System Design – Complete Enterprise Architecture v1.0

This document captures the full agentic multi-repo operating framework, including orchestrator,
subagents, memory, VSCode/Cursor UI, and chat console flows. The goal is to enable conversational
tasks, automated planning, parallel agent execution, sandboxed multi-repo changes, PR generation,
and durable institutional memory.

## 0. Purpose
The design enables developers to issue chat-style tasks (e.g., in Cursor), automatically plan and
coordinate solutions across repositories, generate diffs/PRs, and persist learnings in SpacetimeDB.
The system targets enterprise environments, multi-repo architectures, audit/compliance needs, and
large-scale agent parallelization.

## 1. High-Level Architecture
- **Developer Interaction Layer:** VSCode/Cursor extension and web-based AMOF Chat Console.
- **Orchestrator Layer:** task manager, planner engine, subagent scheduler, structured reporting,
  worktree sandbox orchestration, git/PR integration.
- **Agent Execution Layer:** planner, coder, refactor, tester, docs/spec, and research subagents.
- **AMOF Core Layer:** manifest (amof.yaml), guardrails, context extraction engine, multi-repo
  coordination.
- **Memory Layer (SpacetimeDB):** task history, failure patterns, preferences, architectural norms,
  and multi-agent knowledge.
- **Infrastructure Layer:** worktree sandbox storage, CI/CD integration, authentication/RBAC/SSO,
  and LLM connectors (GPT, internal models).

## 2. Developer Experience
Developers interact through a chat UI (VSCode panel or web console):
1. Developer submits a task (e.g., “Add customerRiskScore to CustomerService API and all consumers”).
2. Orchestrator forwards to the planner subagent to produce a multi-repo plan.
3. Orchestrator creates sandbox worktrees per repo, launches coder/test/doc subagents in parallel,
   and aggregates diffs and test results.
4. The chat UI shows the plan, progress, summaries, diffs, and PR links for developer review and
   approval. Developers focus on outcomes, not mechanics.

## 3. AMOF Core
### 3.1 Manifest (amof.yaml)
Defines repositories, their locations, include/exclude paths, guardrails, and context mappings. The
orchestrator uses it to scope work across repos (e.g., services/customer-service, docs,
shared-libs, auth-system).

### 3.2 Guardrails
Enforce what agents may or may not change to maintain safety and architectural boundaries. Examples:
- Do not touch infra/terraform directories.
- Do not modify build.gradle without explicit instruction.
- Do not delete tests.
- Avoid deprecated endpoints.

### 3.3 Context Extraction
Generates minimal, relevant context (ranked files, architecture maps) per subagent. Diffs are
produced only within allowed scope.

### 3.4 Integrity (Merkle trees & git object model)
- Git already provides a Merkle-DAG content store, so all cloned repos and sandbox worktrees inherit
  cryptographic content addressing without extra dependencies.
- Worktrees share the same object database; the orchestrator verifies branch/commit ancestry before
  applying changes so Cursor-style Merkle integrity checks align with native git operations.
- For transport to agents (e.g., context bundles), we rely on git-tracked files; optional hashed
  manifests can be added later for cache-friendly Cursor integrations, but the baseline integrity is
  provided by git itself.

## 4. Orchestrator
Central service responsible for:
- **Task Manager:** ingesting tasks, lifecycle management, persistence.
- **Planner Engine:** extracting intent, producing repo-aware plans, identifying sub-tasks.
- **Subagent Scheduler:** creating sandboxes, launching subagents, tracking status, collecting
  structured reports.
- **Worktree Sandbox Generator:** per-repo worktrees in `repo/.amof/worktrees/task-<id>/` to isolate
  changes safely before PR creation and cleanup.
- **Reporting Engine:** aggregating results, changes, confidence, warnings, and next actions; feeding
  the chat/UI.
- **Git Integration:** pushing branches, creating PRs, handling merge conflict resolution.

## 5. Subagent Model
Short-lived AI workers:
- **Types:** planner, coder (primary), refactor, tester, docs, research.
- **Lifecycle:** spawn task-specific context → isolate memory → load sandbox files → reason → produce
  structured report → return to orchestrator → terminate.
- **Structured Report:** result (success/fail/partial), changed paths, confidence, warnings,
  next_actions; used for evaluation, audit, and orchestrator decisions.

## 6. Worktree Sandbox Layer
Each subagent gets an isolated worktree per repo (`.amof/worktrees/task-XYZ/`). Diff generation
happens inside the sandbox; after PR/merge the sandbox is cleaned up to avoid polluting the main
workspace.

## 7. Memory Layer (SpacetimeDB)
Persistent store for:
- Task history (requests, decisions, contexts, outputs)
- Failure patterns (flaky tests, low-quality repos)
- Developer preferences (e.g., naming, style)
- Architectural norms (forbidden cross-service links)
- Multi-agent learning (effective reasoning strategies)

## 8. Human Roles
- **Developer = Guardian:** submits tasks, reviews results, approves PRs.
- **Orchestrator = Tech Lead:** plans, launches agents, audits, scales execution.
- **Subagents = Workers:** execute isolated tasks with clean context and maximum precision.

## 9. Developer UI Architecture
Two UI layers:
- **VSCode/Cursor Extension:** sidebar (tasks, status, workspace), task detail view (planner plan,
  subagents, reports, warnings, diffs, PR links), command palette actions, and optional React webview
  for richer presentation.
- **AMOF Chat Console (web UI):** Cursor-like chat thread with system messages (planner running…),
  subagent progress, structured results, diff blocks, and PR blocks streamed via SSE.

## 10. API Layer
Key endpoints:
- `POST /chat` – submit message + workspace → orchestrator converts to task
- `POST /tasks` – create tasks
- `GET /tasks` – list tasks
- `GET /tasks/{id}` – task detail (planner plan, status)
- `GET /tasks/{id}/diffs?repo=X` – unified patch
- `POST /tasks/{id}/retryFailed`
- `POST /tasks/{id}/cancel`

## 11. Deployment Model
Options: on-prem Docker, Kubernetes cluster, or serverless orchestrator with air-gapped LLM inference
for enterprise. Orchestrator is stateless; SpacetimeDB stores persistent data.

## 12. Security & Compliance
All operations are audited; token-based auth and RBAC via the orchestrator; PRs reference task IDs;
zero-trust boundaries separate reasoning from execution; memory is mapped to GDPR/ISO 27001
constraints.

## 13. Roadmap (Phases)
- **Phase 1 – Foundation:** manifest, CLI, guardrails, context engine.
- **Phase 2 – Orchestrator v1:** planner agent, sandbox engine, subagent lifecycle, structured
  reporting, PR automation.
- **Phase 3 – Memory Layer:** SpacetimeDB integration, memory-driven planning, preference learning.
- **Phase 4 – Multi-agent Scaling:** parallel subagents, multi-repo batch tasks, auto-resolve
  conflicts.
- **Phase 5 – Enterprise Layer:** audit, RBAC, SSO, dashboards.

## 14. Summary
AMOF delivers a new operating model where AI agents do most of the work, the orchestrator directs
them, developers supervise and approve, memory grows with each task, and multi-repo changes remain
safe through isolation and automation across familiar UIs (VSCode/Cursor chat) without vendor lock-in.

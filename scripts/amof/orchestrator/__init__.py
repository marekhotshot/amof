"""AMOF Orchestrator - custom agent system mirroring Cursor's tool interface.

v0.4.0 — Smart LLM Utilization & Multi-Provider:
- Planner-Executor architecture: strong model plans, cheap models execute subtasks
- OpenAI provider support: GPT-5.x Codex, GPT-4o, o-series with full tool calling
- Anthropic prompt caching: cache_control on system + tools (up to 90% input savings)
- Codebase indexing: structured JSON index via big-context model (.amof/codebase-index.json)
- Guardrail hardening: hardcoded .git/.env/secrets/key protection, sensitive command gates,
  unattended mode blocks for package installs and git push/merge/rebase
- Telemetry: guardrail block tracking, prompt cache hit rate, per-provider metrics
- MODEL_PRICING: GPT-5.2/5.1 Codex (high/medium/max) with 1M token context windows
- CLI: --provider, --plan-execute, --planner-model, --index flags

v0.3.2 — Performance, reliability & observability (merged Cursor + Orchestrator):
- Concurrent tool execution: parallel ThreadPoolExecutor for multiple tool calls
- Smart session truncation: 15K char limit in session, full output in event log
- Empty assistant message fix: skip empty content blocks in API conversion
- Telemetry efficiency metrics: tokens/$, $/tool_call, tools/LLM_call
- EventLog.load_from_file: read-only (no directory creation)
- Circuit breaker: stops after 3 consecutive API failures
- LLM timeout protection: 120s timeout on API calls with automatic recovery
- Tool error categorization: 9 distinct failure categories
- Session validation: warns about excessive context, message imbalance
- Context window warnings at 70% usage
- Session metadata: extensible storage, persisted with save/load

v0.3.1 — Robustness & reliability:
- LLM retry with exponential backoff + jitter (429, 5xx, connection errors)
- Shell safety: blocked destructive commands, process group isolation
- Grep fallback to GNU grep when ripgrep unavailable
- EventLog: query(), summary_stats(), replay_timeline(), load_from_file()
- ModelRouter: auto-demotion after 5 consecutive successes (saves cost)
- Agent: empty response recovery (retry with model promotion)

v0.3.0 — Cost efficiency overhaul:
- ModelRouter: multi-tier model selection (fast/standard/strong)
- ContextSummarizer: compress old turns via cheap model (replaces pruning)
- Per-tier telemetry: track costs by model tier
- Enhanced system prompt with aggressive parallel tool call instructions

v0.2.0 — Foundation:
- Tool-level telemetry (success rates, usage frequency)
- Session persistence (save/load)
- Append-only JSONL event logging
"""

__version__ = "2.6.1"

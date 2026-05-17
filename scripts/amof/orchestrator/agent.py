"""Core agent loop.

Implements the message -> LLM -> tool calls -> execute -> loop cycle,
mirroring Cursor's orchestration with AMOF-specific guardrails,
telemetry, and event logging.

Cost optimizations:
- ModelRouter selects cheap models for exploration, expensive for edits
- ContextSummarizer compresses old turns instead of re-sending them
- Failure-based promotion escalates to stronger models when needed
"""

from __future__ import annotations

import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from .agent_models import EcosystemCommand
from .context.summarizer import ContextSummarizer
from .events import EventLog
from .llm.base import LLMClient, LLMResponse, ProviderError, stop_reason_for_failure_class
from .llm.inference_authority import _client_provider
from .model_router import ModelRouter
from .session import Session
from .telemetry import SessionTelemetry
from .tools.base import ToolCall, ToolRegistry, ToolResult

logger = logging.getLogger(__name__)

# Maximum iterations to prevent infinite loops
# 50 was too low for multi-task plans (benchmark V3 showed AMOF agent
# completed only 7/9 tasks before hitting this). 200 gives room for
# complex plans while still preventing true infinite loops.
MAX_ITERATIONS = 200

# Tools that indicate exploration phase (cheap model is adequate)
EXPLORATION_TOOLS = {"LS", "Read", "InspectFiles", "Glob", "Grep"}

# Max chars of tool output to keep in session (saves context tokens).
# Reduced to avoid 200k token limit when many tools run (e.g. grep + read_file).
# Full output is always preserved in the event log.
_MAX_TOOL_OUTPUT_IN_SESSION = 6_000
_DESTRUCTIVE_SHELL_RE = re.compile(
    r"(\brm\s+-rf\b|\bgit\s+reset\s+--hard\b|\bgit\s+clean\s+-fdx\b|\bmkfs\b|\bdd\s+if=)",
    re.IGNORECASE,
)


class Agent:
    """AMOF Agent -- the core orchestration loop.

    Takes a user message, sends it to the LLM with tool definitions,
    executes any requested tool calls, and loops until the LLM produces
    a final text response (no more tool calls).

    Supports:
    - ModelRouter for multi-tier model selection (fast/standard/strong)
    - ContextSummarizer for compressing old conversation turns
    - Single LLMClient for backward compatibility (no router, no summarizer)
    """

    def __init__(
        self,
        llm: LLMClient,
        tools: ToolRegistry,
        system_prompt: str,
        session: Optional[Session] = None,
        telemetry: Optional[SessionTelemetry] = None,
        events: Optional[EventLog] = None,
        max_iterations: int = MAX_ITERATIONS,
        verbose: bool = False,
        dry_run: bool = False,
        # Cost optimization components (optional)
        model_router: Optional[ModelRouter] = None,
        context_summarizer: Optional[ContextSummarizer] = None,
        # IAL Phase 1b — when set, telemetry recording and the per-step
        # `llm_call` event for the master path are routed through the shared
        # InferenceAuthority so raw events.jsonl carries source="master" and
        # the same emission path is exercised as for summarizer/indexer.
        # Optional and Any-typed to avoid an import cycle with the
        # llm package; CLI callers may leave it None for the legacy path.
        inference_authority: Optional[Any] = None,
    ):
        self.tools = tools
        self.system_prompt = system_prompt
        self.session = session or Session()
        self.telemetry = telemetry or SessionTelemetry()
        self.events = events or EventLog(session_id=self.session.id)
        self.max_iterations = max_iterations
        self.verbose = verbose
        self.dry_run = dry_run

        # If a model_router is provided, use it for model selection.
        # Otherwise, use the single llm client directly.
        self.model_router = model_router
        self.llm = llm  # fallback / default client
        self.context_summarizer = context_summarizer
        self.inference_authority = inference_authority

        # Track last tool calls for phase detection
        self._last_tool_names: Optional[List[str]] = None
        
        # Circuit breaker for repeated failures
        self._consecutive_api_failures = 0
        self._max_consecutive_api_failures = 3

        # Stop reason: set on all exit paths
        # Values: "pending", "completed", "cost_exceeded", "max_iterations",
        #         "circuit_breaker", "interrupted",
        # plus structured provider failure stop reasons surfaced by the
        # ProviderError taxonomy (see llm/base.stop_reason_for_failure_class):
        #   "provider_payment_required", "provider_auth", "provider_rate_limit",
        #   "provider_server_error", "provider_network", "provider_api_error".
        self.stop_reason: str = "pending"

        # Truthful provider failure attribution. When a backend raises
        # ProviderError these are populated so downstream surfaces (run record,
        # /runs/{id} loop_state, dashboard runs page) can show "OpenRouter
        # 402 payment_required (resumable)" instead of a generic api_error.
        self.failure_class: Optional[str] = None
        self.failure_provider: Optional[str] = None
        self.failure_status_code: Optional[int] = None
        self.resumable: bool = False

        # Iteration counter exposed as an attribute so post-run projection
        # (e.g. agent_runner -> run_manager.update_loop_state) can surface
        # truthful "iterations used" instead of leaving the UI on n/a.
        self.iteration_count: int = 0

    def run(self, user_message: str) -> str:
        """Run the agent loop for a single user message.

        Returns the final text response from the LLM.
        """
        # Validate session state before starting
        if self.session.turn_count > 0:
            issues = self.session.validate()
            if issues and self.verbose:
                for issue in issues:
                    _print_status(f"  [WARNING] {issue}")
        
        # Record user message
        self.session.add_user_message(user_message)
        self.events.user_message(user_message)

        if self.verbose:
            from .colors import USER, RESET
            _print_status(f"{USER}User: {user_message[:100]}...{RESET}")

        iteration = 0
        while iteration < self.max_iterations:
            iteration += 1
            self.iteration_count = iteration

            # Check cost ceiling
            if self.telemetry.cost_exceeded:
                msg = f"Cost limit exceeded (${self.telemetry.total_cost:.4f} >= ${self.telemetry.max_cost:.2f}). Stopping."
                self.stop_reason = "cost_exceeded"
                self.events.error("cost_exceeded", msg, fatal=True)
                return msg
            
            # Circuit breaker: stop if too many consecutive API failures
            if self._consecutive_api_failures >= self._max_consecutive_api_failures:
                msg = f"Circuit breaker triggered: {self._consecutive_api_failures} consecutive API failures. Stopping to prevent infinite retry loop."
                self.stop_reason = "circuit_breaker"
                self.events.error("circuit_breaker", msg, fatal=True)
                self.telemetry.record_failure("circuit_breaker")
                return msg

            # Select model for this step (router or single client)
            active_llm = self._select_model()

            # Context management: summarize if approaching window limit
            self._manage_context(active_llm)

            # Call LLM
            try:
                response = self._call_llm(active_llm)
                # Reset failure counter on successful API call
                self._consecutive_api_failures = 0
            except ProviderError as pe:
                # Truthful, attributed provider failure (e.g. OpenRouter 402
                # credit exhausted, OpenAI 401 invalid key, Anthropic 429).
                # Stop the loop NOW with a specific stop_reason so the run
                # record does not collapse into generic "api_error" /
                # "circuit_breaker", and so the UI can advertise resumability.
                provider_label = pe.provider or "unknown"
                self.failure_class = pe.failure_class
                self.failure_provider = provider_label
                self.failure_status_code = pe.status_code
                self.resumable = pe.resumable
                self.stop_reason = stop_reason_for_failure_class(pe.failure_class)

                self.telemetry.record_failure(self.stop_reason)
                # Truthful event for the run timeline. Keep the original
                # "api_call_failed" type for backward-compat readers, but
                # carry attributed metadata so dashboards can render it.
                self.events.error(
                    "api_call_failed",
                    str(pe),
                    fatal=True,
                    provider=provider_label,
                    status_code=pe.status_code,
                    failure_class=pe.failure_class,
                    resumable=pe.resumable,
                )

                if self.model_router:
                    self.model_router.report_provider_failure(provider_label)

                if self.verbose:
                    _print_status(
                        f"  [{iteration}] {provider_label} provider error "
                        f"({pe.failure_class}"
                        + (f", status={pe.status_code}" if pe.status_code else "")
                        + f", resumable={pe.resumable}): {pe}"
                    )

                return f"{provider_label} provider error ({pe.failure_class}): {pe}"
            except Exception as e:
                # Track API failures for circuit breaker
                self._consecutive_api_failures += 1
                self.telemetry.record_failure("api_error")
                self.events.error("api_call_failed", str(e), fatal=False)

                # Report provider-level failure for health tracking / failover
                if self.model_router:
                    self.model_router.report_provider_failure()
                
                if self._consecutive_api_failures >= self._max_consecutive_api_failures:
                    # Let the circuit breaker catch it on next iteration
                    continue
                
                # Promote model and retry
                if self.model_router:
                    self.model_router.record_failure()
                
                if self.verbose:
                    _print_status(f"  [{iteration}] API call failed: {e}, retrying...")
                
                continue

            tier_label = ""
            if self.model_router:
                tier_label = f" [{self.model_router.active_tier}]"

            if self.verbose:
                _print_status(
                    f"  [{iteration}]{tier_label} {response.usage.model} "
                    f"{response.usage.prompt_tokens}+{response.usage.completion_tokens}tok "
                    f"${response.usage.estimated_cost:.4f} "
                    f"{response.usage.latency_ms}ms"
                    + (f" | {len(response.tool_calls)} tool calls" if response.tool_calls else " | final response")
                )
                # Show extended thinking if present
                if response.thinking:
                    from .colors import THINKING, RESET, INFO
                    thinking_preview = response.thinking.strip().splitlines()
                    max_lines = 10
                    for tl in thinking_preview[:max_lines]:
                        _print_status(f"  {THINKING}{tl}{RESET}")
                    if len(thinking_preview) > max_lines:
                        _print_status(
                            f"  {INFO}... ({len(thinking_preview) - max_lines} more thinking lines){RESET}"
                        )

            # Record telemetry. When an InferenceAuthority is wired in
            # (live API path, Phase 1b), it owns both the per-step
            # `record_from_usage` + `record_agent_cost(<source>, ...)` and
            # the `llm_call` event emission below — so we deliberately do
            # NOT call them again here, to avoid double-counting cost and
            # duplicate llm_call entries in events.jsonl. CLI callers
            # without authority keep the legacy in-Agent emission.
            #
            # Mini Ultra Plan 2 / Phase L2: honor the authority's
            # ``default_source`` instead of hard-coding ``"master"``.
            # When this Agent was constructed from RunnerFactory with a
            # sibling authority obtained via
            # ``parent_authority.with_source(f"runner:{name}")``, the
            # source attribution must read ``runner:<name>`` end to end
            # (CallMetrics, agent_costs, ``llm_call`` events, SSE
            # rebroadcast). Falling back to "master" only when no
            # authority is wired preserves CLI back-compat.
            tier = self.model_router.active_tier if self.model_router else "default"
            tool_calls_count = len(response.tool_calls) if response.tool_calls else 0
            if self.inference_authority is not None:
                authority_source = (
                    getattr(self.inference_authority, "default_source", None)
                    or "master"
                )
                # Mini Ultra Plan 2 / Phase L2: pass the truthful upstream
                # provider extracted from the LLM that ACTUALLY served this
                # call (``self.llm`` — possibly a runner-scoped local override
                # client) instead of letting the authority default to the
                # underlying cloud router. Without this, a runner using a
                # local Qwen override would record ``provider="anthropic"``
                # because the master authority's underlying is the cloud
                # cascade. ``_client_provider`` returns "" when the active
                # llm doesn't expose a provider, so the existing fallback to
                # ``self._underlying`` still applies for unknown shapes.
                self.inference_authority.record_external_call(
                    response.usage,
                    source=authority_source,
                    tier=tier,
                    tool_calls=tool_calls_count,
                    provider=_client_provider(self.llm),
                )
            else:
                self.telemetry.record_from_usage(response.usage, tier=tier)
                if response.usage:
                    self.telemetry.record_agent_cost(
                        "master", response.usage.estimated_cost
                    )

            # Budget early warnings
            warning = self.telemetry.check_budget_warning()
            if warning:
                self.events.log("budget_warning", message=warning)
                if self.verbose:
                    _print_status(f"  [BUDGET] {warning}")
                else:
                    print(f"  [agent] {warning}", file=sys.stderr)

            # Report provider success (for health tracking)
            if self.model_router:
                self.model_router.report_provider_success()

            # Track retries if present
            if hasattr(response, "_retry_count") and response._retry_count > 0:
                for _ in range(response._retry_count):
                    self.telemetry.record_retry()

            # Track prompt cache metrics if present (Anthropic)
            cache_creation = getattr(response, "_cache_creation_tokens", 0)
            cache_read = getattr(response, "_cache_read_tokens", 0)
            if cache_creation or cache_read:
                self.telemetry.record_cache_usage(cache_creation, cache_read)

            # Phase 1b: when authority is wired, the `llm_call` event was
            # already emitted with `source="master"` by record_external_call
            # above. Skip the legacy in-Agent emission to avoid duplicate
            # llm_call entries in events.jsonl. CLI back-compat path keeps
            # the original emission (no source field, UI defaults to master).
            if self.inference_authority is None:
                self.events.llm_call(
                    model=response.usage.model,
                    prompt_tokens=response.usage.prompt_tokens,
                    completion_tokens=response.usage.completion_tokens,
                    cost=response.usage.estimated_cost,
                    latency_ms=response.usage.latency_ms,
                    tool_calls_count=tool_calls_count,
                )

            if response.has_tool_calls:
                # Execute tool calls and add results to conversation
                all_succeeded = self._handle_tool_calls(response)
                if self.stop_reason == "cancelled":
                    self.events.log(
                        "stop_requested_honored",
                        phase="tool_execution",
                        reason="active_tool_cancelled",
                    )
                    return "Stop requested; active tool interrupted."

                # Update router with success/failure feedback
                if self.model_router:
                    if all_succeeded:
                        self.model_router.record_success()
                    else:
                        self.model_router.record_failure()
            elif response.text and response.text.strip():
                # Before declaring complete: end-of-task linting
                lint_output = self.tools.lint_modified_files()
                if lint_output:
                    # Linter found issues — inject back into conversation
                    # so the agent can fix them before finishing
                    if self.verbose:
                        _print_status(f"  [LINT] Found issues in {len(self.tools.modified_files)} modified files")
                    self.session.add_assistant_message(content=response.text)
                    lint_msg = (
                        "Before you finish, the linter found issues in files you modified. "
                        "Please fix them:\n\n" + lint_output
                    )
                    self.session.add_user_message(lint_msg)
                    continue  # Loop again to let the agent fix lint errors

                # Final response with actual content
                self.session.add_assistant_message(content=response.text)
                self.events.agent_response(content=response.text)
                self.stop_reason = "completed"

                if self.model_router:
                    self.model_router.record_success()

                return response.text
            else:
                # Empty response -- treat as failure and retry (promote model if router active)
                self.telemetry.record_empty_response()
                self.telemetry.record_failure("empty_response")
                
                if self.verbose:
                    _print_status(f"  [{iteration}] Empty response from LLM (stop_reason={response.stop_reason}), retrying...")

                self.events.log(
                    "empty_response",
                    stop_reason=response.stop_reason,
                    model=response.usage.model if response.usage else "unknown",
                )

                if self.model_router:
                    self.model_router.record_failure()

                # Add a nudge to the conversation to get the LLM to respond
                self.session.add_assistant_message(content="")
                self.session.add_user_message(
                    "Your previous response was empty. Please continue with the task."
                )
                continue  # retry the loop

        # Exceeded max iterations
        self.stop_reason = "max_iterations"
        msg = f"Max iterations ({self.max_iterations}) exceeded. Stopping."
        self.events.error("max_iterations", msg, fatal=True)
        return msg

    def run_interactive(self) -> None:
        """Run agent in interactive REPL mode (Python-shell style).

        Supports multi-line input: end a line with \\ to continue on the next line.
        Slash commands: /status, /cost, /help, /quit
        """
        self.events.session_start(
            mode=self.session.mode,
            goal="interactive",
            ecosystem=self.session.ecosystem,
        )

        model_info = self.llm.model_name()
        if self.model_router:
            names = self.model_router.tier_model_names()
            model_info = " / ".join(f"{t}={n}" for t, n in names.items())

        eco = self.session.ecosystem or ""
        print(f"AMOF Agent ({eco}) — interactive shell")
        print(f"Models: {model_info}")
        print(f"Session: {self.session.id}")
        print(f"Type /help for commands, Ctrl+C to cancel, Ctrl+D to exit.")
        print()

        try:
            while True:
                try:
                    line = input(">>> ").strip()
                except EOFError:
                    print()
                    break

                if not line:
                    continue

                # Multi-line: if line ends with \, keep reading
                while line.endswith("\\"):
                    line = line[:-1]
                    try:
                        continuation = input("... ")
                    except EOFError:
                        break
                    line = line + "\n" + continuation

                line = line.strip()
                if not line:
                    continue

                # Slash commands
                if line.startswith("/"):
                    cmd = line.lower().split()[0]
                    if cmd in ("/quit", "/exit", "/q"):
                        break
                    elif cmd in ("/status", "/cost"):
                        print(f"\n{self.telemetry.summary()}\n")
                        continue
                    elif cmd == "/help":
                        print()
                        print("  /help     Show this help")
                        print("  /status   Show session telemetry (cost, tokens, calls)")
                        print("  /cost     Same as /status")
                        print("  /quit     Exit the shell")
                        print()
                        print("  Type any task and press Enter to send it to the agent.")
                        print("  End a line with \\ to continue on the next line.")
                        print("  Ctrl+C cancels the current agent run.")
                        print("  Ctrl+D exits the shell.")
                        print()
                        continue
                    else:
                        print(f"  Unknown command: {cmd}. Type /help for available commands.")
                        continue

                # Run agent
                try:
                    response = self.run(line)
                    print(f"\n{response}\n")
                except KeyboardInterrupt:
                    print("\n  (cancelled)\n")
                    continue

        except KeyboardInterrupt:
            print("\n")

        # Session end
        self.events.session_end(self.telemetry.to_dict())
        print(f"\n{self.telemetry.summary()}")
        print(f"Event log: {self.events.log_path}")

    def _select_model(self) -> LLMClient:
        """Select the appropriate model for the current step.

        Uses ModelRouter if available, otherwise returns the single LLM client.
        """
        if not self.model_router:
            return self.llm

        # Auto-detect phase from last tool calls
        return self.model_router.select_for_tools(self._last_tool_names)

    def _manage_context(self, active_llm: LLMClient) -> None:
        """Manage context window: summarize or prune as needed."""
        context_window = active_llm.context_window()
        system_tokens = len(self.system_prompt) // 4
        current_tokens = self.session.estimate_tokens()
        
        # Warn if approaching context limit without summarizer
        if not self.context_summarizer and current_tokens > context_window * 0.70:
            pct = (current_tokens / context_window) * 100
            if self.verbose:
                _print_status(
                    f"  [WARNING] Context at {pct:.0f}% of window "
                    f"({current_tokens:,}/{context_window:,} tokens). "
                    f"Consider enabling ContextSummarizer to avoid pruning."
                )
            self.events.log(
                "context_warning",
                current_tokens=current_tokens,
                context_window=context_window,
                usage_pct=round(pct, 1),
            )

        # Try summarization first (preserves information)
        if self.context_summarizer:
            did_summarize = self.context_summarizer.summarize(
                self.session, context_window, system_tokens,
            )
            if did_summarize:
                stats = self.context_summarizer.stats()
                if self.verbose:
                    _print_status(
                        f"  [SUMMARIZE] Compressed context "
                        f"(saved ~{stats['tokens_saved']:,} tokens, "
                        f"cost ${stats['summarization_cost']:.4f})"
                    )
                self.events.log(
                    "context_summarized",
                    tokens_saved=stats["tokens_saved"],
                    summarization_cost=stats["summarization_cost"],
                    summarizations=stats["summarizations"],
                )
                # Record summarization cost in telemetry
                self.telemetry.record_summarization_cost(
                    self.context_summarizer.total_summarization_cost
                )
                self.telemetry.record_context_summarization()
                return

        # Fallback: prune if still over budget (safety net)
        max_conversation_tokens = int(context_window * 0.80) - system_tokens
        pruned = self.session.prune_context(max_conversation_tokens, system_tokens)
        if pruned > 0:
            if self.verbose:
                _print_status(
                    f"  [PRUNE] Pruned {pruned} items to fit context window "
                    f"({self.session.estimate_tokens()} tokens remaining)"
                )
            self.events.log(
                "context_pruned",
                items_pruned=pruned,
                tokens_after=self.session.estimate_tokens(),
            )

    def _call_llm(self, active_llm: LLMClient) -> LLMResponse:
        """Make a single LLM API call with current conversation state.

        Includes 120s timeout protection and error recovery for API failures.
        """
        messages = self.session.get_messages_for_api()
        tool_schemas = self.tools.schemas()
        sys_tokens = len(self.system_prompt) // 4
        msg_tokens = sum(len(str(m.get("content", ""))) // 4 for m in messages)

        if self.dry_run:
            payload_summary = (
                "**Debug is enabled.** No LLM call was sent.\n\n"
                f"Estimated prompt tokens: {sys_tokens + msg_tokens}\n\n"
                f"**System Prompt** ({sys_tokens} tokens):\n"
                f"```text\n{self.system_prompt[:2000]}...\n```\n\n"
                f"**Messages** ({len(messages)}):\n"
            )
            for i, m in enumerate(messages):
                role = m.get("role", "unknown")
                content = str(m.get("content", "")) if not isinstance(m.get("content"), list) else f"[Complex content: {len(m['content'])} items]"
                payload_summary += f"- **{role}** ({len(content) // 4} tokens): {content[:100].replace(chr(10), ' ')}...\n"
            from .llm.base import Usage
            return LLMResponse(
                text=payload_summary,
                tool_calls=[],
                usage=Usage(model="dry-run", prompt_tokens=sys_tokens + msg_tokens, completion_tokens=0, latency_ms=0, estimated_cost=0.0),
                stop_reason="dry_run",
            )

        try:
            # LLM calls have a 120s timeout to prevent hanging
            import signal
            
            def timeout_handler(signum, frame):
                raise TimeoutError("LLM API call exceeded 120s timeout")
            
            # Set timeout (Unix only)
            old_handler = None
            try:
                old_handler = signal.signal(signal.SIGALRM, timeout_handler)
                signal.alarm(120)
            except (AttributeError, ValueError):
                # Windows or signal not available
                pass
            
            try:
                response = active_llm.chat(
                    system=self.system_prompt,
                    messages=messages,
                    tools=tool_schemas if tool_schemas else None,
                )
            finally:
                # Cancel timeout
                try:
                    signal.alarm(0)
                    if old_handler is not None:
                        signal.signal(signal.SIGALRM, old_handler)
                except (AttributeError, ValueError):
                    pass
            
            return response
            
        except TimeoutError as e:
            # Record timeout and promote model
            self.events.error("llm_timeout", str(e), fatal=False)
            if self.model_router:
                self.model_router.record_failure()
            # Return empty response to trigger retry with promoted model
            return LLMResponse(text=None, tool_calls=None, usage=None, stop_reason="timeout")

    @staticmethod
    def _execute_single_tool(
        tools: ToolRegistry, tc_request: Any
    ) -> tuple:
        """Execute a single tool call. Returns (tc_request, result, duration_ms).

        Static method so it can be used with ThreadPoolExecutor.
        """
        # Enforce typed shell command validation before execution.
        if tc_request.name == "Shell":
            args = dict(tc_request.arguments or {})
            try:
                structured_cmd = EcosystemCommand.model_validate({
                    "target_repo": args.get("working_directory"),
                    "command": args.get("command", ""),
                    "is_destructive": bool(_DESTRUCTIVE_SHELL_RE.search(args.get("command", ""))),
                    "rationale": "Generated by agent tool-calling step",
                })
            except ValidationError as e:
                return tc_request, ToolResult(
                    success=False,
                    output="",
                    error=f"Invalid structured shell command: {e}",
                ), 0
            tc_request.arguments["command"] = structured_cmd.command

        tool_call = ToolCall(
            id=tc_request.id,
            name=tc_request.name,
            arguments=tc_request.arguments,
        )
        start = time.monotonic()
        result = tools.execute(tool_call)
        duration_ms = int((time.monotonic() - start) * 1000)
        return tc_request, result, duration_ms

    def _handle_tool_calls(self, response: LLMResponse) -> bool:
        """Execute tool calls from LLM response and add results to session.

        Returns True if all tool calls succeeded, False if any failed.
        """
        # Add assistant message with tool calls
        tool_calls_data = [
            {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
            for tc in response.tool_calls
        ]
        self.session.add_assistant_message(
            content=response.text,
            tool_calls=tool_calls_data,
        )

        # Track tool names for phase detection on next iteration
        self._last_tool_names = [tc.name for tc in response.tool_calls]

        # Execute tool calls — use concurrent execution when multiple independent calls
        tool_calls_list = response.tool_calls
        execution_results = []

        if len(tool_calls_list) > 1:
            # Parallel execution for multiple tool calls
            with ThreadPoolExecutor(max_workers=min(len(tool_calls_list), 8)) as pool:
                futures = {
                    pool.submit(self._execute_single_tool, self.tools, tc): tc
                    for tc in tool_calls_list
                }
                for future in as_completed(futures):
                    execution_results.append(future.result())
            # Re-order to match original tool call order
            order = {tc.id: i for i, tc in enumerate(tool_calls_list)}
            execution_results.sort(key=lambda x: order.get(x[0].id, 0))
        else:
            # Single tool call — no overhead
            for tc in tool_calls_list:
                execution_results.append(self._execute_single_tool(self.tools, tc))

        # Process results
        results = []
        all_succeeded = True
        for tc_request, result, duration_ms in execution_results:
            if result.cancelled:
                self.stop_reason = "cancelled"
            if not result.success:
                all_succeeded = False
                # Categorize tool failures for better telemetry
                error_category = self._categorize_tool_error(tc_request.name, result.error)
                self.telemetry.record_failure(error_category)

            if self.verbose:
                status = "OK" if result.success else "FAIL"
                output_preview = result.output[:80].replace("\n", " ") if result.output else ""
                _print_status(f"    [{status}] {tc_request.name}({_brief_args(tc_request.arguments)}) {duration_ms}ms | {output_preview}")

            # Log tool execution
            self.events.tool_call(
                tool_name=tc_request.name,
                arguments=tc_request.arguments,
                success=result.success,
                duration_ms=duration_ms,
                output_preview=result.output[:200] if result.output else None,
                error=result.error,
                metadata=result.metadata,
            )

            # Record tool-level telemetry
            self.telemetry.record_tool_call(tc_request.name, result.success, duration_ms, metadata=result.metadata)

            # Truncate large tool outputs in session to save context tokens
            # (full output is preserved in the event log above)
            session_output = result.to_text()
            if len(session_output) > _MAX_TOOL_OUTPUT_IN_SESSION:
                session_output = (
                    session_output[:_MAX_TOOL_OUTPUT_IN_SESSION]
                    + f"\n\n... (truncated for context; {len(session_output)} chars total)"
                )

            results.append({
                "id": tc_request.id,
                "tool_call_id": tc_request.id,
                "content": session_output,
                "is_error": not result.success,
                "output": result.output,
            })

        # Add tool results to session
        self.session.add_tool_results(results)
        return all_succeeded
    
    def _categorize_tool_error(self, tool_name: str, error: Optional[str]) -> str:
        """Categorize tool errors for telemetry tracking."""
        if not error:
            return "tool_unknown"
        
        error_lower = error.lower()
        
        # File/path errors
        if "not found" in error_lower or "does not exist" in error_lower:
            return "tool_not_found"
        if "permission denied" in error_lower or "readonly" in error_lower:
            return "tool_permission"
        if "no_touch_path" in error_lower or "guardrail" in error_lower:
            return "tool_guardrail"
        
        # Shell errors
        if "cancelled" in error_lower and "stop request" in error_lower:
            return "tool_cancelled"
        if "timeout" in error_lower or "timed out" in error_lower:
            return "tool_timeout"
        if "command not found" in error_lower:
            return "tool_command_not_found"
        if "blocked" in error_lower:
            return "tool_blocked"
        
        # StrReplace errors
        if "found" in error_lower and "times" in error_lower:
            return "tool_ambiguous_match"
        
        # Generic
        if "invalid" in error_lower or "argument" in error_lower:
            return "tool_invalid_args"
        
        return "tool_other"


def _print_status(msg: str) -> None:
    """Print colored status message to stderr (so it doesn't mix with agent output)."""
    from .colors import ACTION, RESET, RED, GREEN, YELLOW
    # Colorize common patterns in status messages
    colored = msg
    if "[OK]" in colored:
        colored = colored.replace("[OK]", f"{GREEN}[OK]{ACTION}")
    if "[FAIL]" in colored:
        colored = colored.replace("[FAIL]", f"{RED}[FAIL]{ACTION}")
    if "[WARNING]" in colored:
        colored = colored.replace("[WARNING]", f"{YELLOW}[WARNING]{ACTION}")
    if "[SUMMARIZE]" in colored:
        colored = colored.replace("[SUMMARIZE]", f"{YELLOW}[SUMMARIZE]{ACTION}")
    if "[PRUNE]" in colored:
        colored = colored.replace("[PRUNE]", f"{YELLOW}[PRUNE]{ACTION}")
    print(f"{ACTION}{colored}{RESET}", file=sys.stderr)


def _brief_args(args: Dict[str, Any], max_len: int = 60) -> str:
    """Format arguments briefly for status display."""
    parts = []
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 30:
            v = v[:27] + "..."
        parts.append(f"{k}={v!r}")
    result = ", ".join(parts)
    if len(result) > max_len:
        result = result[:max_len - 3] + "..."
    return result

"""Append-only event log for agent sessions.

Writes JSONL to the AMOF app-data runs directory (for example
`~/.local/share/amof/runs/<session-id>/events.jsonl` under XDG defaults, or
the configured `AMOF_HOME` / XDG override equivalent).
Each event is a single JSON line with timestamp, type, and payload.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from amof.app_paths import runs_dir as default_runs_dir


class EventLog:
    """Append-only JSONL event logger for a single session."""

    def __init__(
        self,
        session_id: Optional[str] = None,
        runs_dir: Optional[Path] = None,
        *,
        run_id: Optional[str] = None,
        studio_session_id: Optional[str] = None,
        ticket_id: Optional[str] = None,
        planning_mode: Optional[str] = None,
        context: Optional[str] = None,
        actor: str = "amof.chat",
    ):
        self.session_id = session_id or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        self.run_id = str(run_id or self.session_id)
        self.studio_session_id = (
            str(studio_session_id).strip() if studio_session_id is not None else None
        )
        self.ticket_id = str(ticket_id).strip() if ticket_id is not None else None
        self.planning_mode = str(planning_mode).strip() if planning_mode is not None else None
        self.context = str(context).strip() if context is not None else None
        self.actor = actor
        self._runs_dir = runs_dir or default_runs_dir()
        self._session_dir = self._runs_dir / self.session_id
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = self._session_dir / "events.jsonl"
        self._events: List[Dict[str, Any]] = []
        self._event_counter = 0

    @property
    def session_dir(self) -> Path:
        return self._session_dir

    @property
    def log_path(self) -> Path:
        return self._log_path

    def log(self, event_type: str, **payload: Any) -> Dict[str, Any]:
        """Log an event. Returns the event dict."""
        self._event_counter += 1
        timestamp = datetime.now(timezone.utc).isoformat()
        severity = str(payload.pop("severity", "info") or "info")
        actor = str(payload.pop("actor", self.actor) or self.actor)
        ticket_id = payload.pop("ticket_id", self.ticket_id)
        studio_session_id = payload.pop("studio_session_id", self.studio_session_id)
        planning_mode = payload.pop("planning_mode", self.planning_mode)
        context = payload.pop("context", self.context)
        event = {
            "event_id": f"{self.run_id}:{self._event_counter:04d}",
            "run_id": self.run_id,
            "session_id": self.session_id,
            "timestamp": timestamp,
            "event_type": event_type,
            "severity": severity,
            "actor": actor,
            "ticket_id": ticket_id,
            "planning_mode": planning_mode,
            "context": context,
            # Legacy aliases retained for existing readers/tests.
            "ts": timestamp,
            "type": event_type,
            **payload,
        }
        if studio_session_id is not None:
            event["studio_session_id"] = studio_session_id
        self._events.append(event)
        self._append_to_file(event)
        return event

    def session_start(self, mode: str, goal: str, ecosystem: Optional[str] = None) -> Dict[str, Any]:
        """Log session start."""
        return self.log(
            "session_start",
            session_id=self.session_id,
            mode=mode,
            goal=goal,
            ecosystem=ecosystem,
        )

    def llm_call(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost: float | None,
        latency_ms: int,
        tool_calls_count: int = 0,
        **extra: Any,
    ) -> Dict[str, Any]:
        """Log an LLM API call."""
        raw_cost_status = str(extra.get("cost_status") or "").strip().lower()
        cost_status = (
            raw_cost_status
            if raw_cost_status in {"observed", "unknown"}
            else ("observed" if cost is not None else "unknown")
        )
        payload: Dict[str, Any] = {
            "model": model,
            "tokens": {"in": prompt_tokens, "out": completion_tokens},
            "cost": round(float(cost), 6) if cost is not None else None,
            "cost_status": cost_status,
            "latency_ms": latency_ms,
            "tool_calls": tool_calls_count,
        }
        for key in (
            "source",
            "provider",
            "upstream_provider",
            "upstream_model",
            "request_id",
            "policy_decision",
            "input_hash",
            "output_hash",
            "provider_generation_ref",
        ):
            value = extra.get(key)
            if value is not None:
                payload[key] = value
        return self.log("llm_call", **payload)

    def tool_call(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        success: bool,
        duration_ms: int,
        output_preview: Optional[str] = None,
        error: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        """Log a tool execution."""
        event_data: Dict[str, Any] = {
            "tool": tool_name,
            "args": _truncate_args(arguments),
            "success": success,
            "duration_ms": duration_ms,
        }
        if output_preview:
            event_data["output_preview"] = output_preview[:200]
        if error:
            event_data["error"] = error[:500]
        if metadata:
            event_data["metadata"] = metadata
        if extra:
            event_data.update(extra)
        return self.log("tool_call", **event_data)

    def capability_elevation(
        self,
        *,
        session_id: str,
        plan_id: str,
        approved_capabilities: List[str],
        base_ceiling: List[str],
        approved_tools: Optional[List[str]] = None,
        approved_paths: Optional[List[str]] = None,
        approval_source: str = "interactive",
    ) -> Dict[str, Any]:
        """Log scoped plan-execute capability approval (no secret values)."""
        return self.log(
            "capability_elevation",
            session_id=session_id,
            plan_id=plan_id,
            approved_capabilities=approved_capabilities,
            base_ceiling=base_ceiling,
            approved_tools=approved_tools or [],
            approved_paths=approved_paths or [],
            approval_source=approval_source,
        )

    def resume_followup(
        self,
        *,
        session_id: str,
        source: str,
        chars: int,
        sha256: str,
        preview: str,
    ) -> Dict[str, Any]:
        """Log operator follow-up on resume (preview only; no secret values)."""
        return self.log(
            "resume_followup",
            session_id=session_id,
            source=source,
            chars=chars,
            sha256=sha256,
            preview=preview,
        )

    def budget_approval(
        self,
        *,
        session_id: str,
        amount: float,
        new_limit: float,
        source: str = "cli_flag",
    ) -> Dict[str, Any]:
        """Log explicit additional budget approval for resume."""
        return self.log(
            "budget_approval",
            session_id=session_id,
            amount=round(amount, 4),
            new_limit=round(new_limit, 4),
            source=source,
        )

    def writable_root_approval(
        self,
        *,
        session_id: str,
        path: str,
        approval_source: str = "cli_flag",
    ) -> Dict[str, Any]:
        """Log plan-scoped writable root approval (path only; no secrets)."""
        return self.log(
            "writable_root_approval",
            session_id=session_id,
            path=path,
            approval_source=approval_source,
        )

    def tool_pack_approval(
        self,
        *,
        session_id: str,
        tool_pack: str,
        approval_source: str = "cli_flag",
    ) -> Dict[str, Any]:
        """Log plan-scoped tool-pack approval."""
        return self.log(
            "tool_pack_approval",
            session_id=session_id,
            tool_pack=tool_pack,
            approval_source=approval_source,
        )

    def policy_gate(
        self,
        *,
        tool_name: str,
        source: str,
        requested_caps: List[str],
        trusted_intent_caps: List[str],
        untrusted_context_present: bool,
        untrusted_sources: List[str],
        allowed: bool,
        reason_code: str,
        matched_rule: str,
        message: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Log a trust-boundary policy decision before tool execution."""
        payload: Dict[str, Any] = {
            "tool": tool_name,
            "source": source,
            "requested_caps": requested_caps,
            "trusted_intent_caps": trusted_intent_caps,
            "untrusted_context_present": untrusted_context_present,
            "untrusted_sources": untrusted_sources,
            "allowed": allowed,
            "reason_code": reason_code,
            "matched_rule": matched_rule,
        }
        if message:
            payload["message"] = message[:500]
        return self.log("policy_gate", **payload)

    def checkpoint(self, step: int, commit: str, message: str, tag: Optional[str] = None) -> Dict[str, Any]:
        """Log a checkpoint (commit after a plan step)."""
        return self.log(
            "checkpoint",
            step=step,
            commit=commit,
            message=message,
            tag=tag,
        )

    def session_end(self, telemetry_summary: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Log session end with cumulative metrics."""
        return self.log("session_end", telemetry=telemetry_summary)

    def user_message(self, content: str) -> Dict[str, Any]:
        """Log a user message."""
        return self.log("user_message", content=content[:500])

    def agent_response(self, content: Optional[str] = None, tool_calls_count: int = 0) -> Dict[str, Any]:
        """Log agent response summary."""
        data: Dict[str, Any] = {"tool_calls": tool_calls_count}
        if content:
            data["content_preview"] = content[:200]
        return self.log("agent_response", **data)

    def error(
        self,
        error_type: str,
        message: str,
        fatal: bool = False,
        **extra: Any,
    ) -> Dict[str, Any]:
        """Log an error.

        Accepts arbitrary ``**extra`` metadata so structured failures (e.g.
        attributed provider errors carrying ``provider`` / ``status_code`` /
        ``failure_class`` / ``resumable``) can be persisted without forcing
        every reader to upgrade.
        """
        return self.log(
            "error",
            error_type=error_type,
            message=message[:500],
            fatal=fatal,
            **extra,
        )

    def get_events(self, event_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get logged events, optionally filtered by type."""
        if event_type:
            return [e for e in self._events if e["type"] == event_type]
        return list(self._events)

    def _append_to_file(self, event: Dict[str, Any]) -> None:
        """Append event as JSONL line."""
        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, default=str) + "\n")
        except Exception:
            pass  # Don't fail agent operations due to logging errors


    def query(
        self,
        event_type: Optional[str] = None,
        tool_name: Optional[str] = None,
        success_only: bool = False,
        failure_only: bool = False,
    ) -> List[Dict[str, Any]]:
        """Query events with filters.

        Args:
            event_type: Filter by event type (e.g., "tool_call", "llm_call").
            tool_name: Filter tool_call events by tool name.
            success_only: Only return successful events.
            failure_only: Only return failed events.
        """
        results = self._events

        if event_type:
            results = [e for e in results if e.get("type") == event_type]

        if tool_name:
            results = [e for e in results if e.get("tool") == tool_name]

        if success_only:
            results = [e for e in results if e.get("success") is True]

        if failure_only:
            results = [e for e in results if e.get("success") is False]

        return results

    def summary_stats(self) -> Dict[str, Any]:
        """Compute summary statistics from logged events."""
        llm_calls = self.query(event_type="llm_call")
        tool_calls = self.query(event_type="tool_call")
        errors = self.query(event_type="error")

        total_cost = sum(
            float(e.get("cost"))
            for e in llm_calls
            if isinstance(e.get("cost"), (int, float))
        )
        unknown_cost_calls = sum(1 for e in llm_calls if e.get("cost_status") == "unknown")
        total_tokens_in = sum(e.get("tokens", {}).get("in", 0) for e in llm_calls)
        total_tokens_out = sum(e.get("tokens", {}).get("out", 0) for e in llm_calls)
        total_latency = sum(e.get("latency_ms", 0) for e in llm_calls)

        tool_success = sum(1 for e in tool_calls if e.get("success"))
        tool_failure = len(tool_calls) - tool_success

        # Tool usage breakdown
        tool_usage: Dict[str, int] = {}
        for e in tool_calls:
            name = e.get("tool", "unknown")
            tool_usage[name] = tool_usage.get(name, 0) + 1
        
        # Error breakdown
        error_types: Dict[str, int] = {}
        for e in errors:
            error_type = e.get("error_type", "unknown")
            error_types[error_type] = error_types.get(error_type, 0) + 1
        
        # Context management events
        summarizations = len(self.query(event_type="context_summarized"))
        prunings = len(self.query(event_type="context_pruned"))

        result = {
            "llm_calls": len(llm_calls),
            "total_cost": None if unknown_cost_calls > 0 and total_cost == 0.0 else round(total_cost, 6),
            "cost_status": "unknown" if unknown_cost_calls > 0 and total_cost == 0.0 else "observed",
            "unknown_cost_calls": unknown_cost_calls,
            "total_tokens": {"in": total_tokens_in, "out": total_tokens_out},
            "avg_latency_ms": total_latency // max(len(llm_calls), 1),
            "tool_calls": len(tool_calls),
            "tool_success": tool_success,
            "tool_failure": tool_failure,
            "tool_success_rate": round(tool_success / max(len(tool_calls), 1) * 100, 1),
            "tool_usage": dict(sorted(tool_usage.items(), key=lambda x: x[1], reverse=True)),
        }
        
        if errors:
            result["errors"] = {
                "total": len(errors),
                "by_type": dict(sorted(error_types.items(), key=lambda x: x[1], reverse=True)),
            }
        
        if summarizations > 0 or prunings > 0:
            result["context_management"] = {
                "summarizations": summarizations,
                "prunings": prunings,
            }
        
        return result

    def replay_timeline(self, max_entries: int = 50) -> str:
        """Generate a human-readable timeline of the session.

        Returns a formatted string showing the sequence of events.
        """
        lines = []
        for i, event in enumerate(self._events[:max_entries]):
            ts = event.get("ts", "?")
            etype = event.get("type", "?")

            if etype == "session_start":
                lines.append(f"[{ts}] SESSION START — {event.get('goal', '')[:80]}")
            elif etype == "llm_call":
                tokens = event.get("tokens", {})
                lines.append(
                    f"[{ts}] LLM {event.get('model', '?')} "
                    f"{tokens.get('in', 0)}+{tokens.get('out', 0)}tok "
                    + (
                        f"${float(event.get('cost')):.4f} "
                        if isinstance(event.get("cost"), (int, float))
                        else "cost=unknown "
                    )
                    + f"{event.get('latency_ms', 0)}ms "
                    + f"({event.get('tool_calls', 0)} tools)"
                )
            elif etype == "tool_call":
                status = "OK" if event.get("success") else "FAIL"
                lines.append(
                    f"[{ts}] TOOL [{status}] {event.get('tool', '?')} "
                    f"{event.get('duration_ms', 0)}ms"
                )
            elif etype == "agent_response":
                preview = event.get("content_preview", "")[:60]
                lines.append(f"[{ts}] RESPONSE: {preview}...")
            elif etype == "error":
                lines.append(f"[{ts}] ERROR [{event.get('error_type', '?')}] {event.get('message', '')[:80]}")
            elif etype == "session_end":
                telem = event.get("telemetry", {})
                total_cost = telem.get("total_cost")
                cost_text = (
                    f"${float(total_cost):.4f}"
                    if isinstance(total_cost, (int, float))
                    else "cost=unknown"
                )
                lines.append(
                    f"[{ts}] SESSION END — "
                    f"{cost_text} "
                    f"{telem.get('total_calls', 0)} calls"
                )
            elif etype == "context_summarized":
                lines.append(
                    f"[{ts}] SUMMARIZE — saved ~{event.get('tokens_saved', 0):,} tokens "
                    f"(${event.get('summarization_cost', 0):.4f})"
                )
            else:
                lines.append(f"[{ts}] {etype}")

        if len(self._events) > max_entries:
            lines.append(f"... ({len(self._events) - max_entries} more events)")

        return "\n".join(lines)

    def analyze_failures(self) -> Dict[str, Any]:
        """Analyze failure patterns in the session.
        
        Returns insights about what went wrong and when.
        """
        tool_failures = self.query(event_type="tool_call", failure_only=True)
        errors = self.query(event_type="error")
        
        # Group tool failures by tool name
        failures_by_tool: Dict[str, List[str]] = {}
        for failure in tool_failures:
            tool = failure.get("tool", "unknown")
            error = failure.get("error", "unknown error")
            if tool not in failures_by_tool:
                failures_by_tool[tool] = []
            failures_by_tool[tool].append(error)
        
        # Find repeated failures (same error multiple times)
        repeated_errors: Dict[str, int] = {}
        for tool, error_list in failures_by_tool.items():
            for error in error_list:
                key = f"{tool}: {error[:100]}"
                repeated_errors[key] = repeated_errors.get(key, 0) + 1
        
        # Filter to only repeated errors (>1 occurrence)
        repeated_errors = {k: v for k, v in repeated_errors.items() if v > 1}
        
        return {
            "total_tool_failures": len(tool_failures),
            "total_errors": len(errors),
            "failures_by_tool": {k: len(v) for k, v in failures_by_tool.items()},
            "repeated_errors": dict(sorted(repeated_errors.items(), key=lambda x: x[1], reverse=True)),
            "fatal_errors": [e for e in errors if e.get("fatal")],
        }
    
    @classmethod
    def load_from_file(cls, log_path: Path) -> "EventLog":
        """Load an EventLog from an existing JSONL file (for analysis).

        Read-only: does not create any directories or files.
        """
        session_id = log_path.parent.name
        # Create instance without triggering directory creation
        instance = object.__new__(cls)
        instance.session_id = session_id
        instance.run_id = session_id
        instance.ticket_id = None
        instance.planning_mode = None
        instance.context = None
        instance.actor = "amof.chat"
        instance._runs_dir = log_path.parent.parent
        instance._session_dir = log_path.parent
        instance._log_path = log_path
        instance._events = []
        instance._event_counter = 0

        if log_path.exists():
            for line in log_path.read_text(encoding="utf-8").strip().split("\n"):
                if line.strip():
                    try:
                        event = json.loads(line)
                        instance._events.append(event)
                        if isinstance(event.get("event_id"), str):
                            maybe_counter = str(event["event_id"]).split(":")[-1]
                            if maybe_counter.isdigit():
                                instance._event_counter = max(instance._event_counter, int(maybe_counter))
                        if isinstance(event.get("run_id"), str) and event.get("run_id"):
                            instance.run_id = event["run_id"]
                        if isinstance(event.get("context"), str) and event.get("context"):
                            instance.context = event["context"]
                    except json.JSONDecodeError:
                        pass
        return instance


def _truncate_args(args: Dict[str, Any], max_len: int = 200) -> Dict[str, Any]:
    """Truncate long argument values for logging."""
    truncated = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > max_len:
            truncated[k] = v[:max_len] + f"... ({len(v)} chars)"
        else:
            truncated[k] = v
    return truncated

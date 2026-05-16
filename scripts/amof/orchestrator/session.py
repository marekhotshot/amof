"""Session management for agent conversations.

Tracks conversation state, message history, and provides
serialization for persistence and context management.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class Message:
    """A single message in the conversation."""

    role: str  # "user", "assistant", "tool"
    content: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    results: Optional[List[Dict[str, Any]]] = None  # for tool role
    timestamp: float = field(default_factory=lambda: datetime.now(timezone.utc).timestamp())

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for LLM API consumption."""
        d: Dict[str, Any] = {"role": self.role}

        if self.role == "user":
            d["content"] = self.content or ""

        elif self.role == "assistant":
            if self.content:
                d["content"] = self.content
            if self.tool_calls:
                d["tool_calls"] = self.tool_calls

        elif self.role == "tool":
            if self.results:
                d["results"] = self.results

        return d


class Session:
    """Manages a single agent conversation session.

    Holds the message history and provides methods for adding messages,
    serializing/deserializing, and computing context size estimates.
    """

    def __init__(self, session_id: Optional[str] = None, mode: str = "build"):
        self.id = session_id or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        self.mode = mode
        self.messages: List[Message] = []
        self.created_at = datetime.now(timezone.utc)
        self.goal: Optional[str] = None
        self.ecosystem: Optional[str] = None
        self.step: int = 0  # current plan step
        self.metadata: Dict[str, Any] = {}  # extensible metadata storage

    def add_user_message(self, content: str) -> Message:
        """Add a user message."""
        msg = Message(role="user", content=content)
        self.messages.append(msg)
        if not self.goal:
            self.goal = content[:200]
        return msg

    def add_assistant_message(
        self,
        content: Optional[str] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
    ) -> Message:
        """Add an assistant message (text and/or tool calls)."""
        msg = Message(role="assistant", content=content, tool_calls=tool_calls)
        self.messages.append(msg)
        return msg

    def add_tool_results(self, results: List[Dict[str, Any]]) -> Message:
        """Add tool execution results."""
        msg = Message(role="tool", results=results)
        self.messages.append(msg)
        return msg

    def get_messages_for_api(self) -> List[Dict[str, Any]]:
        """Get messages in format suitable for LLM API."""
        return [m.to_dict() for m in self.messages]

    @property
    def turn_count(self) -> int:
        """Number of user messages (turns)."""
        return sum(1 for m in self.messages if m.role == "user")

    @property
    def last_user_message(self) -> Optional[str]:
        """Get the last user message content."""
        for m in reversed(self.messages):
            if m.role == "user":
                return m.content
        return None

    def estimate_tokens(self) -> int:
        """Rough estimate of conversation tokens (~4 chars per token)."""
        total_chars = 0
        for msg in self.messages:
            if msg.content:
                total_chars += len(msg.content)
            if msg.tool_calls:
                total_chars += len(json.dumps(msg.tool_calls))
            if msg.results:
                total_chars += len(json.dumps(msg.results))
        return total_chars // 4

    def prune_context(self, max_tokens: int, system_tokens: int = 0) -> int:
        """Remove oldest non-essential messages to fit within token budget.

        Keeps the first user message (goal) and the most recent messages.
        Returns the number of messages pruned.
        """
        current = self.estimate_tokens()
        if current <= max_tokens:
            return 0

        # Never prune if fewer than 4 messages
        if len(self.messages) <= 4:
            return 0

        pruned = 0
        # Remove from position 1 (keep first user message at [0])
        # until we're under budget or only 4 messages remain
        while self.estimate_tokens() > max_tokens and len(self.messages) > 4:
            self.messages.pop(1)
            pruned += 1

        return pruned

    def validate(self) -> List[str]:
        """Validate session state and return list of warnings/issues."""
        issues = []
        
        if not self.messages:
            issues.append("Session has no messages")
        
        if not self.goal:
            issues.append("Session goal not set")
        
        if self.turn_count == 0:
            issues.append("No user turns recorded")
        
        # Check for excessive context
        tokens = self.estimate_tokens()
        if tokens > 150_000:
            issues.append(f"Context very large ({tokens:,} tokens) - may need summarization")
        
        # Check for message balance (too many tool results vs user messages)
        tool_msg_count = sum(1 for m in self.messages if m.role == "tool")
        user_msg_count = sum(1 for m in self.messages if m.role == "user")
        if tool_msg_count > user_msg_count * 10:
            issues.append(f"Excessive tool messages ({tool_msg_count}) vs user messages ({user_msg_count})")
        
        return issues
    
    def save(self, path: Path) -> None:
        """Save session to JSON file."""
        data = {
            "id": self.id,
            "mode": self.mode,
            "goal": self.goal,
            "ecosystem": self.ecosystem,
            "step": self.step,
            "created_at": self.created_at.isoformat(),
            "turn_count": self.turn_count,
            "messages": [m.to_dict() for m in self.messages],
            "metadata": self.metadata,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Session:
        """Load session from JSON file."""
        data = json.loads(path.read_text(encoding="utf-8"))
        session = cls(session_id=data["id"], mode=data.get("mode", "build"))
        session.goal = data.get("goal")
        session.ecosystem = data.get("ecosystem")
        session.step = data.get("step", 0)
        session.created_at = datetime.fromisoformat(data["created_at"])
        session.metadata = data.get("metadata", {})

        for msg_data in data.get("messages", []):
            msg = Message(
                role=msg_data["role"],
                content=msg_data.get("content"),
                tool_calls=msg_data.get("tool_calls"),
                results=msg_data.get("results"),
            )
            session.messages.append(msg)

        return session

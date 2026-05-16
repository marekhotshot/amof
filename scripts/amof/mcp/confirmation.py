"""Two-phase confirmation for dangerous MCP operations.

Flow:
  1. Tool handler returns a preview + confirmation token
  2. User calls ``amof_confirm`` with the token to proceed
  3. Token expires after TTL (default 5 minutes)

This prevents accidental destructive actions while keeping
the conversational flow natural.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


DEFAULT_TTL = 300  # 5 minutes


@dataclass
class PendingConfirmation:
    """A staged dangerous action waiting for user confirmation."""
    token: str
    tool_name: str
    description: str
    preview: str
    execute_fn: Callable[[], Any]
    created_at: float
    ttl: float = DEFAULT_TTL
    confirm_type: str = "simple"
    type_target: Optional[str] = None

    @property
    def expired(self) -> bool:
        return (time.time() - self.created_at) > self.ttl


class ConfirmationStore:
    """In-memory store for pending confirmations."""

    def __init__(self) -> None:
        self._pending: Dict[str, PendingConfirmation] = {}

    def create(
        self,
        tool_name: str,
        description: str,
        preview: str,
        execute_fn: Callable[[], Any],
        confirm_type: str = "simple",
        type_target: Optional[str] = None,
        ttl: float = DEFAULT_TTL,
    ) -> PendingConfirmation:
        """Stage a dangerous action and return a confirmation token."""
        self._gc()
        token = secrets.token_hex(8)
        entry = PendingConfirmation(
            token=token,
            tool_name=tool_name,
            description=description,
            preview=preview,
            execute_fn=execute_fn,
            created_at=time.time(),
            ttl=ttl,
            confirm_type=confirm_type,
            type_target=type_target,
        )
        self._pending[token] = entry
        return entry

    def get(self, token: str) -> Optional[PendingConfirmation]:
        """Look up a pending confirmation. Returns None if expired or unknown."""
        self._gc()
        entry = self._pending.get(token)
        if entry and entry.expired:
            del self._pending[token]
            return None
        return entry

    def consume(self, token: str, typed_value: Optional[str] = None) -> Optional[PendingConfirmation]:
        """Consume a confirmation token if valid.

        For ``type-confirm`` entries, ``typed_value`` must match ``type_target``.
        Returns the entry on success, None on failure.
        """
        entry = self.get(token)
        if not entry:
            return None
        if entry.confirm_type == "type-confirm":
            if typed_value is None or typed_value != entry.type_target:
                return None
        del self._pending[token]
        return entry

    def cancel(self, token: str) -> bool:
        """Cancel a pending confirmation. Returns True if it existed."""
        if token in self._pending:
            del self._pending[token]
            return True
        return False

    def list_pending(self) -> List[PendingConfirmation]:
        """Return all non-expired pending confirmations."""
        self._gc()
        return list(self._pending.values())

    def _gc(self) -> None:
        """Remove expired entries."""
        expired = [k for k, v in self._pending.items() if v.expired]
        for k in expired:
            del self._pending[k]

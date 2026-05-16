"""MCP session context: ephemeral navigational scope state.

Tracks which ecosystem/run/release the user is focused on.
In-memory only; resets on reconnect by design.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional


Scope = Literal["global", "ecosystem", "run", "release"]


@dataclass
class ScopeSnapshot:
    """One entry in the scope history stack."""
    scope: Scope
    ecosystem: Optional[str]
    run_id: Optional[str]
    release_tag: Optional[str]


@dataclass
class SessionContext:
    """Per-connection navigational state.

    - Hierarchical: global -> ecosystem -> run | release
    - Push/pop navigation with back stack
    - No disk persistence (ephemeral by design)
    """

    current_scope: Scope = "global"
    selected_ecosystem: Optional[str] = None
    selected_run_id: Optional[str] = None
    selected_release_tag: Optional[str] = None
    mode: Literal["ask", "plan", "execute"] = "execute"
    _history: List[ScopeSnapshot] = field(default_factory=list)

    def _snapshot(self) -> ScopeSnapshot:
        return ScopeSnapshot(
            scope=self.current_scope,
            ecosystem=self.selected_ecosystem,
            run_id=self.selected_run_id,
            release_tag=self.selected_release_tag,
        )

    def _push(self) -> None:
        self._history.append(self._snapshot())

    # -- Navigation --

    def enter_ecosystem(self, name: str) -> None:
        self._push()
        self.current_scope = "ecosystem"
        self.selected_ecosystem = name
        self.selected_run_id = None
        self.selected_release_tag = None

    def enter_run(self, run_id: str) -> None:
        self._push()
        self.current_scope = "run"
        self.selected_run_id = run_id

    def enter_release(self, tag: str) -> None:
        self._push()
        self.current_scope = "release"
        self.selected_release_tag = tag

    def go_back(self) -> bool:
        """Pop the last scope. Returns False if already at global."""
        if not self._history:
            return False
        prev = self._history.pop()
        self.current_scope = prev.scope
        self.selected_ecosystem = prev.ecosystem
        self.selected_run_id = prev.run_id
        self.selected_release_tag = prev.release_tag
        return True

    def go_global(self) -> None:
        """Reset to global scope, clearing history."""
        self._history.clear()
        self.current_scope = "global"
        self.selected_ecosystem = None
        self.selected_run_id = None
        self.selected_release_tag = None

    def set_mode(self, mode: str) -> bool:
        """Set mode. Returns False if invalid."""
        if mode not in ("ask", "plan", "execute"):
            return False
        self.mode = mode  # type: ignore[assignment]
        return True

    # -- Query --

    def breadcrumb(self) -> str:
        """Human-readable scope path, e.g. ``[global > amof-platform > run:abc12]``."""
        parts = ["global"]
        if self.selected_ecosystem:
            parts.append(self.selected_ecosystem)
        if self.current_scope == "run" and self.selected_run_id:
            parts.append(f"run:{self.selected_run_id[:8]}")
        elif self.current_scope == "release" and self.selected_release_tag:
            parts.append(f"release:{self.selected_release_tag}")
        return "[" + " > ".join(parts) + "]"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scope": self.current_scope,
            "ecosystem": self.selected_ecosystem,
            "run_id": self.selected_run_id,
            "release_tag": self.selected_release_tag,
            "mode": self.mode,
            "breadcrumb": self.breadcrumb(),
            "can_go_back": len(self._history) > 0,
        }

    def requires_ecosystem(self) -> str:
        """Return the selected ecosystem or raise."""
        if not self.selected_ecosystem:
            raise ScopeError("No ecosystem selected. Use amof_use_ecosystem first.")
        return self.selected_ecosystem

    def requires_run(self) -> str:
        """Return the selected run_id or raise."""
        if not self.selected_run_id:
            raise ScopeError("No run selected. Use amof_use_run first.")
        return self.selected_run_id

    def requires_release(self) -> str:
        """Return the selected release tag or raise."""
        if not self.selected_release_tag:
            raise ScopeError("No release selected. Use amof_use_release first.")
        return self.selected_release_tag


class ScopeError(Exception):
    """Raised when a tool requires a scope that isn't active."""
    pass

"""ToolProposal -- bounded read-only script escalation through app-data."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from amof.app_paths import evidence_dir

from .base import Tool, ToolResult


_BLOCKED_GIT_RE = re.compile(r"\bgit\s+(commit|push|tag)\b", re.IGNORECASE)
_NETWORK_RE = re.compile(r"\b(curl|wget|nc|netcat|ssh|scp|rsync)\b|https?://", re.IGNORECASE)
_SECRET_RE = re.compile(r"\b(API[_-]?KEY|TOKEN|PASSWORD|SECRET|printenv|os\.environ)\b|\.env\b", re.IGNORECASE)
_BROAD_FS_RE = re.compile(r"(^|\s)(/|~|/home/|/etc/|/var/|/usr/|find\s+/)\b", re.IGNORECASE)
_WRITE_RE = re.compile(r"(^|\s)(rm|mv|cp|touch|mkdir|tee)\b|sed\s+-i|>>|(?<!<)>", re.IGNORECASE)


class ToolProposalTool(Tool):
    name = "ToolProposal"
    description = (
        "Propose and execute a bounded read-only helper script when a capability is "
        "missing. The script is statically checked, stored in AMOF app-data, and "
        "executed with captured rc/stdout/stderr/hash evidence. Direct Shell remains unavailable."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "purpose": {"type": "string", "description": "Why this helper is needed."},
            "mutation_intent": {"type": "boolean", "description": "Whether the proposal mutates target files."},
            "allowed_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Target paths this proposal may inspect or affect.",
            },
            "allow_network": {"type": "boolean", "description": "Whether network access is explicitly allowed."},
            "timeout_seconds": {"type": "integer", "description": "Execution timeout in seconds."},
            "inputs": {"type": "array", "items": {"type": "string"}, "description": "Declared inputs."},
            "outputs": {"type": "array", "items": {"type": "string"}, "description": "Declared outputs."},
            "rollback": {"type": "string", "description": "Rollback plan or why none is needed."},
            "script": {"type": "string", "description": "The helper script content."},
        },
        "required": [
            "purpose",
            "mutation_intent",
            "allowed_paths",
            "allow_network",
            "timeout_seconds",
            "inputs",
            "outputs",
            "rollback",
            "script",
        ],
    }

    def execute(
        self,
        purpose: str,
        mutation_intent: bool,
        allowed_paths: List[str],
        allow_network: bool,
        timeout_seconds: int,
        inputs: List[str],
        outputs: List[str],
        rollback: str,
        script: str,
    ) -> ToolResult:
        proposal = {
            "purpose": purpose,
            "mutation_intent": mutation_intent,
            "allowed_paths": allowed_paths,
            "allow_network": allow_network,
            "timeout_seconds": timeout_seconds,
            "inputs": inputs,
            "outputs": outputs,
            "rollback": rollback,
        }
        gate_error = _validate_static_gates(
            proposal=proposal,
            script=script,
            workspace_root=Path.cwd(),
        )
        if gate_error:
            return ToolResult(success=False, output="", error=gate_error)

        script_hash = hashlib.sha256(script.encode("utf-8")).hexdigest()
        proposal_dir = evidence_dir() / "tool-proposals" / script_hash[:12]
        proposal_dir.mkdir(parents=True, exist_ok=True)
        script_path = proposal_dir / "proposal.sh"
        script_path.write_text(script, encoding="utf-8")

        try:
            completed = subprocess.run(
                ["bash", str(script_path)],
                cwd=Path.cwd(),
                capture_output=True,
                text=True,
                timeout=max(1, min(int(timeout_seconds), 120)),
                check=False,
            )
            rc = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
        except subprocess.TimeoutExpired as exc:
            rc = 124
            stdout = exc.stdout or ""
            stderr = (exc.stderr or "") + f"\nTimed out after {timeout_seconds}s"

        metadata = {
            **proposal,
            "script_hash": script_hash,
            "script_path": str(script_path),
            "rc": rc,
            "stdout": stdout[:4000],
            "stderr": stderr[:4000],
        }
        output = (
            f"ToolProposal executed: rc={rc}\n"
            f"script_hash={script_hash}\n"
            f"script_path={script_path}\n"
            f"stdout:\n{stdout[:4000]}\n"
            f"stderr:\n{stderr[:4000]}"
        ).rstrip()
        return ToolResult(success=rc == 0, output=output, error=None if rc == 0 else f"proposal exited with rc={rc}", metadata=metadata)


def _validate_static_gates(*, proposal: Dict[str, Any], script: str, workspace_root: Path) -> str | None:
    if proposal["mutation_intent"]:
        return "invalid_tool_proposal_static_gate: mutation proposals are not executable in this prototype"
    if not isinstance(script, str) or not script.strip():
        return "invalid_tool_proposal_static_gate: script must be non-empty"
    timeout = proposal["timeout_seconds"]
    if not isinstance(timeout, int) or timeout <= 0 or timeout > 120:
        return "invalid_tool_proposal_static_gate: timeout_seconds must be between 1 and 120"

    allowed_paths = proposal["allowed_paths"]
    if not isinstance(allowed_paths, list) or not allowed_paths:
        return "invalid_tool_proposal_static_gate: allowed_paths must be a non-empty list"
    for raw in allowed_paths:
        if not isinstance(raw, str) or not raw.strip():
            return "invalid_tool_proposal_static_gate: allowed_paths entries must be non-empty strings"
        path = Path(raw)
        if raw.strip() in {".", "/", "*", "**", "~"} or path.is_absolute():
            return "invalid_tool_proposal_static_gate: broad or absolute allowed_paths are not allowed"
        resolved = (workspace_root / path).resolve(strict=False)
        try:
            resolved.relative_to(workspace_root.resolve(strict=False))
        except ValueError:
            return "invalid_tool_proposal_static_gate: allowed_paths must stay within the target workspace"

    if _BLOCKED_GIT_RE.search(script):
        return "invalid_tool_proposal_static_gate: git commit/push/tag are not allowed"
    if not proposal["allow_network"] and _NETWORK_RE.search(script):
        return "invalid_tool_proposal_static_gate: network use requires allow_network=true"
    if _SECRET_RE.search(script):
        return "invalid_tool_proposal_static_gate: secret or environment access is not allowed"
    if _BROAD_FS_RE.search(script):
        return "invalid_tool_proposal_static_gate: broad filesystem access is not allowed"
    if _WRITE_RE.search(script):
        return "invalid_tool_proposal_static_gate: read-only proposals cannot contain write commands or redirection"
    return None

"""GitCheckpoint tool — manages checkpoints on a dedicated helper branch.

Checkpoints are saved on a helper branch (e.g. feature/TICKET-helper) so
the main feature branch stays clean. The agent can:
- Create checkpoints (save progress)
- List checkpoints (show all saved points)
- Restore checkpoints (roll back to a previous state)

Branch naming: <feature-branch>-helper
  e.g. feature/PROJ-123 → feature/PROJ-123-helper
"""

from __future__ import annotations

import subprocess
from typing import Any, Dict, List, Optional

from .base import Tool, ToolResult


class GitCheckpointTool(Tool):
    """Manage git checkpoints on a helper branch.

    Actions:
    - save: Create a checkpoint (git add + commit on helper branch)
    - list: Show all checkpoints with hashes and messages
    - restore: Roll back to a specific checkpoint by hash
    """

    name = "GitCheckpoint"
    description = (
        "Manage git checkpoints for safe progress tracking. "
        "Actions: 'save' (commit changes), 'list' (show checkpoints), "
        "'restore' (rollback to a checkpoint hash). "
        "Checkpoints go to a helper branch — the feature branch stays clean."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["save", "list", "restore"],
                "description": (
                    "Action to perform. 'save' creates a checkpoint, "
                    "'list' shows all checkpoints, 'restore' rolls back."
                ),
            },
            "message": {
                "type": "string",
                "description": (
                    "Checkpoint message (required for 'save'). "
                    "E.g. 'Add user auth middleware'"
                ),
            },
            "commit_hash": {
                "type": "string",
                "description": (
                    "Commit hash to restore to (required for 'restore'). "
                    "Use 'list' to find available checkpoints."
                ),
            },
        },
        "required": ["action"],
    }

    def __init__(self, helper_branch: Optional[str] = None) -> None:
        """Initialize.

        Args:
            helper_branch: Explicit helper branch name. If None, derived
                          from the current branch + '-helper' suffix.
        """
        self._helper_branch = helper_branch
        self._feature_branch: Optional[str] = None
        self._checkpoint_count = 0
        self._initialized = False

    def execute(self, action: str, **kwargs: Any) -> ToolResult:
        """Dispatch to the appropriate action handler."""
        if action == "save":
            message = kwargs.get("message", "")
            if not message:
                return ToolResult(
                    success=False, output="",
                    error="'message' is required for save action.",
                )
            return self._save(message)
        elif action == "list":
            return self._list()
        elif action == "restore":
            commit_hash = kwargs.get("commit_hash", "")
            if not commit_hash:
                return ToolResult(
                    success=False, output="",
                    error="'commit_hash' is required for restore action. Use 'list' to find checkpoints.",
                )
            return self._restore(commit_hash)
        else:
            return ToolResult(
                success=False, output="",
                error=f"Unknown action: '{action}'. Use 'save', 'list', or 'restore'.",
            )

    def _ensure_helper_branch(self) -> Optional[str]:
        """Ensure we're on the helper branch, creating it if needed.

        Returns error string if failed, None on success.
        """
        if self._initialized:
            return None

        try:
            # Get current branch
            result = subprocess.run(
                ["git", "branch", "--show-current"],
                capture_output=True, text=True, timeout=10,
            )
            current = result.stdout.strip()
            if not current:
                return "Not on any branch (detached HEAD?)"

            # Determine branch names
            if current.endswith("-helper"):
                # Already on helper branch
                self._helper_branch = current
                self._feature_branch = current[:-7]  # strip '-helper'
            else:
                self._feature_branch = current
                self._helper_branch = self._helper_branch or f"{current}-helper"

                # Check if helper branch exists
                check = subprocess.run(
                    ["git", "rev-parse", "--verify", self._helper_branch],
                    capture_output=True, text=True, timeout=10,
                )
                if check.returncode != 0:
                    # Create helper branch from current state
                    create = subprocess.run(
                        ["git", "checkout", "-b", self._helper_branch],
                        capture_output=True, text=True, timeout=10,
                    )
                    if create.returncode != 0:
                        return f"Failed to create helper branch: {create.stderr.strip()}"
                else:
                    # Switch to existing helper branch (merge current state)
                    switch = subprocess.run(
                        ["git", "checkout", self._helper_branch],
                        capture_output=True, text=True, timeout=10,
                    )
                    if switch.returncode != 0:
                        return f"Failed to switch to helper branch: {switch.stderr.strip()}"

                    # Merge feature branch changes into helper
                    merge = subprocess.run(
                        ["git", "merge", self._feature_branch, "--no-edit", "-X", "theirs"],
                        capture_output=True, text=True, timeout=30,
                    )
                    # Merge failures are non-fatal — we'll still commit new changes

            self._initialized = True
            return None

        except subprocess.TimeoutExpired:
            return "Git command timed out"
        except FileNotFoundError:
            return "git not found — is it installed?"

    def _save(self, message: str) -> ToolResult:
        """Create a checkpoint on the helper branch."""
        try:
            # Ensure we're on helper branch
            err = self._ensure_helper_branch()
            if err:
                return ToolResult(success=False, output="", error=err)

            # Stage all changes
            add = subprocess.run(
                ["git", "add", "-A"],
                capture_output=True, text=True, timeout=30,
            )
            if add.returncode != 0:
                return ToolResult(
                    success=False,
                    output=add.stderr.strip(),
                    error="git add -A failed",
                )

            # Check for changes
            status = subprocess.run(
                ["git", "diff", "--cached", "--stat"],
                capture_output=True, text=True, timeout=10,
            )
            if not status.stdout.strip():
                return ToolResult(
                    success=True,
                    output="No changes to checkpoint — working tree is clean.",
                )

            # Count files
            files_changed = len([
                line for line in status.stdout.strip().splitlines()
                if line.strip() and "|" in line
            ])

            # Commit
            self._checkpoint_count += 1
            commit_msg = f"checkpoint: {message}"
            commit = subprocess.run(
                ["git", "commit", "-m", commit_msg],
                capture_output=True, text=True, timeout=30,
            )
            if commit.returncode != 0:
                return ToolResult(
                    success=False,
                    output=commit.stderr.strip(),
                    error="git commit failed",
                )

            # Get hash
            hash_result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=10,
            )
            commit_hash = hash_result.stdout.strip()

            return ToolResult(
                success=True,
                output=(
                    f"Checkpoint #{self._checkpoint_count} created.\n"
                    f"  Hash: {commit_hash}\n"
                    f"  Message: {message}\n"
                    f"  Files: {files_changed}\n"
                    f"  Branch: {self._helper_branch}\n"
                    f"  Feature: {self._feature_branch}"
                ),
            )

        except subprocess.TimeoutExpired:
            return ToolResult(success=False, output="", error="Git timed out")
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Checkpoint save failed: {e}")

    def _list(self) -> ToolResult:
        """List all checkpoints on the helper branch."""
        try:
            # Ensure we know the branch names
            err = self._ensure_helper_branch()
            if err:
                return ToolResult(success=False, output="", error=err)

            # Get checkpoint commits (only those with "checkpoint:" prefix)
            log = subprocess.run(
                [
                    "git", "log", "--oneline", "--grep=checkpoint:",
                    "--format=%h | %s | %ci",
                    self._helper_branch,
                ],
                capture_output=True, text=True, timeout=10,
            )

            lines = [l.strip() for l in log.stdout.strip().splitlines() if l.strip()]
            if not lines:
                return ToolResult(
                    success=True,
                    output=(
                        f"No checkpoints found on {self._helper_branch}.\n"
                        "Use GitCheckpoint(action='save', message='...') to create one."
                    ),
                )

            header = f"Checkpoints on {self._helper_branch} ({len(lines)} total):\n\n"
            header += "  Hash    | Message                              | Date\n"
            header += "  --------+--------------------------------------+---------------------\n"
            for line in lines:
                header += f"  {line}\n"

            header += (
                f"\nFeature branch: {self._feature_branch}\n"
                "Use GitCheckpoint(action='restore', commit_hash='<hash>') to restore."
            )

            return ToolResult(success=True, output=header)

        except subprocess.TimeoutExpired:
            return ToolResult(success=False, output="", error="Git timed out")
        except Exception as e:
            return ToolResult(success=False, output="", error=f"List failed: {e}")

    def _restore(self, commit_hash: str) -> ToolResult:
        """Restore working tree to a checkpoint state.

        Uses 'git checkout <hash> -- .' to restore files without changing
        the branch pointer. This is safe and reversible.
        """
        try:
            err = self._ensure_helper_branch()
            if err:
                return ToolResult(success=False, output="", error=err)

            # Verify the commit exists and is a checkpoint
            verify = subprocess.run(
                ["git", "log", "--oneline", "-1", commit_hash],
                capture_output=True, text=True, timeout=10,
            )
            if verify.returncode != 0 or not verify.stdout.strip():
                return ToolResult(
                    success=False, output="",
                    error=f"Commit {commit_hash} not found. Use 'list' to see available checkpoints.",
                )

            commit_info = verify.stdout.strip()

            # Restore files from that commit
            restore = subprocess.run(
                ["git", "checkout", commit_hash, "--", "."],
                capture_output=True, text=True, timeout=30,
            )
            if restore.returncode != 0:
                return ToolResult(
                    success=False,
                    output=restore.stderr.strip(),
                    error=f"Failed to restore checkpoint {commit_hash}",
                )

            # Stage the restored state
            subprocess.run(
                ["git", "add", "-A"],
                capture_output=True, text=True, timeout=30,
            )

            # Show what changed
            diff = subprocess.run(
                ["git", "diff", "--cached", "--stat"],
                capture_output=True, text=True, timeout=10,
            )
            diff_text = diff.stdout.strip() or "(no file changes)"

            return ToolResult(
                success=True,
                output=(
                    f"Restored to checkpoint: {commit_info}\n"
                    f"Branch: {self._helper_branch}\n\n"
                    f"Files restored:\n{diff_text}\n\n"
                    "The restored state is staged. Use save to commit it, or "
                    "make further changes."
                ),
            )

        except subprocess.TimeoutExpired:
            return ToolResult(success=False, output="", error="Git timed out")
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Restore failed: {e}")

    @property
    def checkpoint_count(self) -> int:
        """Number of checkpoints created in this session."""
        return self._checkpoint_count

    @property
    def helper_branch(self) -> Optional[str]:
        """The helper branch name (None if not yet initialized)."""
        return self._helper_branch

    @property
    def feature_branch(self) -> Optional[str]:
        """The original feature branch name."""
        return self._feature_branch

    def switch_to_feature_branch(self) -> Optional[str]:
        """Switch back to the feature branch. Returns error string or None."""
        if not self._feature_branch:
            return "Feature branch unknown"
        try:
            result = subprocess.run(
                ["git", "checkout", self._feature_branch],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return f"Failed to switch: {result.stderr.strip()}"
            return None
        except Exception as e:
            return str(e)

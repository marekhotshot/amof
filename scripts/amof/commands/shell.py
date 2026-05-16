"""AMOF shell integration helpers."""

from __future__ import annotations

import sys
from typing import Any


def _bash_init_snippet() -> str:
    return """# AMOF shell prompt integration
_amof_prompt_cache_file() {
  if [[ -n "${AMOF_HOME:-}" ]]; then
    printf '%s\\n' "${AMOF_HOME%/}/state/current-context"
    return
  fi
  local state_base="${AMOF_STATE_HOME:-${XDG_STATE_HOME:-$HOME/.local/state}}"
  printf '%s\\n' "${state_base%/}/amof/current-context"
}

_amof_prompt_context() {
  local cache_file context
  cache_file="$(_amof_prompt_cache_file)"
  [[ -r "$cache_file" ]] || return 0
  IFS= read -r context < "$cache_file" || return 0
  [[ -n "$context" ]] || return 0
  [[ "$context" == "local" ]] && return 0
  printf '(%s)' "$context"
}
"""


def cmd_shell(args: Any) -> int:
    action = str(getattr(args, "shell_cmd", "") or "").strip()
    shell_name = str(getattr(args, "shell_name", "") or "").strip()

    if action == "init" and shell_name == "bash":
        print(_bash_init_snippet())
        return 0

    sys.stderr.write("Usage: amof shell init bash\n")
    return 1


__all__ = ["cmd_shell"]

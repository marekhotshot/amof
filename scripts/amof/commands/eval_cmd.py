"""Public fail-closed stub for the maintainer eval command."""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional


def cmd_eval(
    manifest: Dict[str, Any],
    tiers: Optional[List[str]] = None,
    tasks_file: Optional[str] = None,
    task_filter: Optional[List[str]] = None,
    provider: Optional[str] = None,
    verbose: bool = False,
    output_dir: Optional[str] = None,
) -> int:
    """Return a maintainer-only refusal in public OSS builds."""
    del manifest, tiers, tasks_file, task_filter, provider, verbose, output_dir
    sys.stderr.write(
        "[eval] This command is unavailable in the public AMOF build. "
        "The eval harness is maintainer-only.\n"
    )
    return 1

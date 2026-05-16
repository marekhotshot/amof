"""Generated-build lane (UP9-1).

Public surface for the read-only runtime detector that classifies
codebases without trustworthy upstream Dockerfiles. See
contracts/generated-build-contract.md and
contracts/generated-build-runtime-detector-matrix.md for the canonical
contract and taxonomy this module implements against.

This package is intentionally narrow:

* Phase 1 (UP9-1) implements detection only. No template rendering, no
  build proof, no runtime proof, no API/UI surfacing, no integration
  with the existing build_gmd_app_lifecycle_build_command path.
* Only the python family path is end-to-end. Node and Go hard signals
  are *recognised* (so polyglot conflicts and "non-first-wave" refusals
  can be returned truthfully), but generation for those families is not
  implemented.
"""

from .detector import (
    detect_runtime,
    render_dockerfile,
    run_build_proof,
    run_runtime_proof,
    SUPPORTED_FAMILIES,
    PYTHON_HARD_SIGNAL_FILES,
    NODE_HARD_SIGNAL_FILES,
    GO_HARD_SIGNAL_FILES,
)
from .store import persist_artifact, load_artifact, load_index
from .admission import evaluate_admission
from .candidate import list_candidates, load_candidate, promote_candidate
from .release_admission import evaluate_release_admission_preview

__all__ = [
    "detect_runtime",
    "render_dockerfile",
    "run_build_proof",
    "run_runtime_proof",
    "SUPPORTED_FAMILIES",
    "PYTHON_HARD_SIGNAL_FILES",
    "NODE_HARD_SIGNAL_FILES",
    "GO_HARD_SIGNAL_FILES",
    "persist_artifact",
    "load_artifact",
    "load_index",
    "evaluate_admission",
    "promote_candidate",
    "list_candidates",
    "load_candidate",
    "evaluate_release_admission_preview",
]

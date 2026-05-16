#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"

log() {
  printf '[install-amof] %s\n' "$*"
}

warn() {
  printf '[install-amof] WARN: %s\n' "$*"
}

detect_legacy_amof_override() {
  local shell_bin="${SHELL:-bash}"
  if ! command -v "${shell_bin}" >/dev/null 2>&1; then
    shell_bin="bash"
  fi
  if ! command -v "${shell_bin}" >/dev/null 2>&1; then
    return 0
  fi
  local detected_type
  detected_type="$("${shell_bin}" -ic 'type -t amof 2>/dev/null || true' 2>/dev/null | tr -d '\r' | tail -n 1)"
  case "${detected_type}" in
    function)
      warn "legacy AMOF shell function detected"
      warn "Use ${ROOT_DIR}/.venv/bin/amof or remove the old function from your shell rc."
      ;;
    alias)
      warn "legacy AMOF shell alias detected"
      warn "Use ${ROOT_DIR}/.venv/bin/amof or remove the old alias from your shell rc."
      ;;
  esac
}

log "using repo root: ${ROOT_DIR}"
cd "${ROOT_DIR}"
detect_legacy_amof_override

if [[ ! -d "${VENV_DIR}" ]]; then
  log "creating virtualenv at ${VENV_DIR}"
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

VENV_PYTHON="${VENV_DIR}/bin/python"
VENV_PIP="${VENV_DIR}/bin/pip"
AMOF_BIN="${VENV_DIR}/bin/amof"

log "upgrading packaging tools"
"${VENV_PYTHON}" -m pip install --upgrade pip setuptools wheel

if [[ -f "${ROOT_DIR}/requirements.txt" ]]; then
  log "installing python dependencies"
  "${VENV_PIP}" install -r "${ROOT_DIR}/requirements.txt"
fi

log "installing editable amof cli"
"${VENV_PIP}" install -e "${ROOT_DIR}"

if [[ ":$PATH:" != *":${ROOT_DIR}/.venv/bin:"* ]]; then
  log "PATH hint: export PATH=\"${ROOT_DIR}/.venv/bin:\$PATH\""
fi

log "validating cli entrypoint"
"${AMOF_BIN}" --help >/dev/null
"${VENV_PYTHON}" -m amof --help >/dev/null

log "bootstrapping app-data roots and default context"
"${AMOF_BIN}" paths --json >/dev/null
"${AMOF_BIN}" context current >/dev/null

log "validating git fetch and push dry-run auth"
"${VENV_PYTHON}" - <<'PY'
from __future__ import annotations

import sys
import time
from pathlib import Path

from amof.commands.promote_main import (
    _classify_git_failure,
    _fetch_origin_main,
    _git_with_credentials,
    _origin_remote_url,
)

repo_root = Path.cwd().resolve()
if not (repo_root / ".git").exists():
    repo_root = Path(__file__).resolve().parents[1]
workspace_root = repo_root.parent.parent if repo_root.name == "amof" and repo_root.parent.name == "repos" else repo_root

remote_url = _origin_remote_url(repo_root, {})
if not remote_url:
    raise SystemExit("[install-amof] git auth check failed: origin remote is not configured")

fetch_ok, fetch_output = _fetch_origin_main(repo_root, workspace_root)
if not fetch_ok:
    classification = _classify_git_failure(fetch_output) or "unknown_error"
    raise SystemExit(
        f"[install-amof] git fetch auth check failed ({classification}): {fetch_output or 'git fetch failed'}"
    )

probe_ref = f"HEAD:refs/heads/amof-auth-check-{int(time.time())}"
push_completed = _git_with_credentials(
    repo_root,
    workspace_root,
    "push",
    "--dry-run",
    "origin",
    probe_ref,
)
push_output = (push_completed.stderr or push_completed.stdout or "").strip()
if push_completed.returncode != 0:
    classification = _classify_git_failure(push_output) or "unknown_error"
    raise SystemExit(
        f"[install-amof] git push --dry-run auth check failed ({classification}): {push_output or 'git push --dry-run failed'}"
    )

print("[install-amof] git auth check passed")
PY

log "running amof doctor"
"${AMOF_BIN}" doctor

printf 'ready for promote\n'

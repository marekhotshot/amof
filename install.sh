#!/usr/bin/env bash
# Safer flow:
#   1. Download this script for inspection instead of piping directly to a shell.
#   2. Review it locally.
#   3. Run `bash install.sh --dry-run ...` first.
#
# In this initial slice, install.sh only delegates to the local checkout installer
# when it is run from a repository checkout that contains scripts/install-local.sh.
# Release artifact download is intentionally not implemented yet.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: install.sh [options]

Safe AMOF installer skeleton.

Safer review flow:
  curl -fsSLo install.sh <url>
  less install.sh
  bash install.sh --dry-run --install-dir ~/.local/bin --context local

Options:
  --dry-run                 Print planned actions without writing files
  --channel <name>          stable | dev | pinned (default: stable)
  --version <value>         Release version or source SHA for future artifact mode
  --install-dir <path>      Directory to place the amof wrapper in
  --amof-home <path>        Persist AMOF_HOME in the installed wrapper
  --context <name>          Context to initialize (currently: local)
  --register-workspace <name>
                            Register a workspace in AMOF app config after install
  --workspace-repo <path>   Repo path to register; defaults to current git repo when available
  --no-shell-profile        Skip shell profile guidance output
  --help                    Show this help text
EOF
}

log() {
  printf '[install] %s\n' "$*"
}

die() {
  printf '[install] %s\n' "$*" >&2
  exit 1
}

DRY_RUN=0
NO_SHELL_PROFILE=0
CHANNEL="stable"
VERSION_VALUE=""
INSTALL_DIR=""
AMOF_HOME_VALUE=""
CONTEXT_NAME="local"
REGISTER_WORKSPACE_NAME=""
WORKSPACE_REPO_PATH=""

write_metadata() {
  local repo_root="$1"
  local channel="$2"
  local version="$3"
  local install_method="$4"
  local resolved_amof_home="$5"
  if [[ -n "$resolved_amof_home" ]]; then
    export AMOF_HOME="$resolved_amof_home"
  fi
  PYTHONPATH="${repo_root}/scripts${PYTHONPATH:+:${PYTHONPATH}}" python3 - "$channel" "$version" "$install_method" <<'PY'
import sys

from amof.version_metadata import save_install_metadata

save_install_metadata(
    channel=sys.argv[1],
    version=sys.argv[2],
    install_method=sys.argv[3],
)
PY
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --channel)
      [[ $# -ge 2 ]] || die "--channel requires a value"
      CHANNEL="$2"
      shift 2
      ;;
    --version)
      [[ $# -ge 2 ]] || die "--version requires a value"
      VERSION_VALUE="$2"
      shift 2
      ;;
    --install-dir)
      [[ $# -ge 2 ]] || die "--install-dir requires a value"
      INSTALL_DIR="$2"
      shift 2
      ;;
    --amof-home)
      [[ $# -ge 2 ]] || die "--amof-home requires a value"
      AMOF_HOME_VALUE="$2"
      shift 2
      ;;
    --context)
      [[ $# -ge 2 ]] || die "--context requires a value"
      CONTEXT_NAME="$2"
      shift 2
      ;;
    --register-workspace)
      [[ $# -ge 2 ]] || die "--register-workspace requires a value"
      REGISTER_WORKSPACE_NAME="$2"
      shift 2
      ;;
    --workspace-repo)
      [[ $# -ge 2 ]] || die "--workspace-repo requires a value"
      WORKSPACE_REPO_PATH="$2"
      shift 2
      ;;
    --no-shell-profile)
      NO_SHELL_PROFILE=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      die "Unknown option: $1"
      ;;
  esac
done

case "$CHANNEL" in
  stable|dev|pinned)
    ;;
  *)
    die "Unsupported --channel value: $CHANNEL"
    ;;
esac

if [[ "$CONTEXT_NAME" != "local" ]]; then
  die "Only --context local is supported in this installer slice"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_INSTALLER="${SCRIPT_DIR}/scripts/install-local.sh"
if [[ -n "$AMOF_HOME_VALUE" ]]; then
  RESOLVED_AMOF_HOME="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).expanduser().resolve(strict=False))' "$AMOF_HOME_VALUE")"
else
  RESOLVED_AMOF_HOME=""
fi
if [[ -z "$VERSION_VALUE" ]] && git -C "$SCRIPT_DIR" rev-parse HEAD >/dev/null 2>&1; then
  VERSION_VALUE="$(git -C "$SCRIPT_DIR" rev-parse HEAD)"
fi

log "channel: $CHANNEL"
if [[ -n "$VERSION_VALUE" ]]; then
  log "version request: $VERSION_VALUE"
else
  log "version request: <current checkout or future default>"
fi

if [[ -x "$LOCAL_INSTALLER" ]]; then
  log "repo checkout detected; delegating to scripts/install-local.sh"
  delegate_args=()
  if [[ "$DRY_RUN" -eq 1 ]]; then
    delegate_args+=("--dry-run")
  fi
  if [[ -n "$INSTALL_DIR" ]]; then
    delegate_args+=("--install-dir" "$INSTALL_DIR")
  fi
  if [[ -n "$AMOF_HOME_VALUE" ]]; then
    delegate_args+=("--amof-home" "$AMOF_HOME_VALUE")
  fi
  delegate_args+=("--context" "$CONTEXT_NAME")
  if [[ -n "$REGISTER_WORKSPACE_NAME" ]]; then
    delegate_args+=("--register-workspace" "$REGISTER_WORKSPACE_NAME")
  fi
  if [[ -n "$WORKSPACE_REPO_PATH" ]]; then
    delegate_args+=("--workspace-repo" "$WORKSPACE_REPO_PATH")
  fi
  delegate_args+=("--channel" "$CHANNEL")
  if [[ -n "$VERSION_VALUE" ]]; then
    delegate_args+=("--version" "$VERSION_VALUE")
  fi
  if [[ "$NO_SHELL_PROFILE" -eq 1 ]]; then
    delegate_args+=("--no-shell-profile")
  fi

  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "dry-run: would run $LOCAL_INSTALLER ${delegate_args[*]}"
    log "dry-run: would record remote installer metadata in AMOF app-data"
    exit 0
  fi

  "$LOCAL_INSTALLER" "${delegate_args[@]}"
  write_metadata "$SCRIPT_DIR" "$CHANNEL" "$VERSION_VALUE" "remote-installer-skeleton" "$RESOLVED_AMOF_HOME"
  exit 0
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  log "dry-run: release artifact download is not implemented outside a repo checkout"
  log "download this script, inspect it, then run from a repo checkout or wait for release artifacts"
  exit 0
fi

die "Release artifact download not implemented yet outside a repo checkout"

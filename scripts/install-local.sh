#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/install-local.sh [options]

Install a local development AMOF wrapper from this repo checkout.

Options:
  --dry-run                 Print planned actions without writing files
  --install-dir <path>      Directory to place the amof wrapper in
  --amof-home <path>        Persist AMOF_HOME in the installed wrapper
  --context <name>          Context to initialize (currently: local)
  --channel <name>          stable | dev | pinned (default: dev)
  --version <value>         Installed version or source SHA metadata
  --register-workspace <name>
                            Register a workspace in AMOF app config after install
  --workspace-repo <path>   Repo path to register; defaults to current git repo when available
  --no-shell-profile        Skip shell profile guidance output
  --help                    Show this help text
EOF
}

log() {
  printf '[install-local] %s\n' "$*"
}

die() {
  printf '[install-local] %s\n' "$*" >&2
  exit 1
}

DRY_RUN=0
NO_SHELL_PROFILE=0
INSTALL_DIR="${HOME:-}/.local/bin"
AMOF_HOME_VALUE=""
CONTEXT_NAME="local"
CHANNEL="dev"
VERSION_VALUE=""
REGISTER_WORKSPACE_NAME=""
WORKSPACE_REPO_PATH=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
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

if [[ -z "${HOME:-}" ]]; then
  die "HOME must be set so the installer can choose a user-local install dir"
fi

if [[ "$CONTEXT_NAME" != "local" ]]; then
  die "Only --context local is supported in this installer slice"
fi

case "$CHANNEL" in
  stable|dev|pinned)
    ;;
  *)
    die "Unsupported --channel value: $CHANNEL"
    ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CLI_SCRIPT="${REPO_ROOT}/scripts/amof.py"

[[ -f "$CLI_SCRIPT" ]] || die "Expected CLI entrypoint at $CLI_SCRIPT"

INSTALL_DIR="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).expanduser().resolve(strict=False))' "$INSTALL_DIR")"
TARGET_PATH="${INSTALL_DIR}/amof"
if [[ -n "$AMOF_HOME_VALUE" ]]; then
  RESOLVED_AMOF_HOME="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).expanduser().resolve(strict=False))' "$AMOF_HOME_VALUE")"
else
  RESOLVED_AMOF_HOME=""
fi
if [[ -z "$VERSION_VALUE" ]]; then
  if git -C "$REPO_ROOT" rev-parse HEAD >/dev/null 2>&1; then
    VERSION_VALUE="$(git -C "$REPO_ROOT" rev-parse HEAD)"
  else
    VERSION_VALUE="repo-checkout"
  fi
fi
if [[ -n "$REGISTER_WORKSPACE_NAME" ]]; then
  if [[ -n "$WORKSPACE_REPO_PATH" ]]; then
    WORKSPACE_REPO_PATH="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).expanduser().resolve(strict=False))' "$WORKSPACE_REPO_PATH")"
  elif git -C "$PWD" rev-parse --show-toplevel >/dev/null 2>&1; then
    WORKSPACE_REPO_PATH="$(git -C "$PWD" rev-parse --show-toplevel)"
  else
    die "--register-workspace requires --workspace-repo or a current working directory inside a git repo"
  fi
  git -C "$WORKSPACE_REPO_PATH" rev-parse --show-toplevel >/dev/null 2>&1 || die "workspace repo is not a git repo: $WORKSPACE_REPO_PATH"
fi

WRAPPER_CONTENT="$(cat <<EOF
#!/usr/bin/env bash
set -euo pipefail
$(if [[ -n "$RESOLVED_AMOF_HOME" ]]; then printf 'export AMOF_HOME=%q\n' "$RESOLVED_AMOF_HOME"; fi)
exec python3 $(printf '%q' "$CLI_SCRIPT") "\$@"
EOF
)"

log "repo root: $REPO_ROOT"
log "install dir: $INSTALL_DIR"
if [[ -n "$RESOLVED_AMOF_HOME" ]]; then
  log "amof home override: $RESOLVED_AMOF_HOME"
else
  log "amof home override: <default XDG layout>"
fi
log "context: $CONTEXT_NAME"
log "channel: $CHANNEL"
log "version: $VERSION_VALUE"
if [[ -n "$REGISTER_WORKSPACE_NAME" ]]; then
  log "workspace registration: $REGISTER_WORKSPACE_NAME -> $WORKSPACE_REPO_PATH"
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  log "dry-run: would create $TARGET_PATH"
  log "dry-run: would verify '$TARGET_PATH paths --json'"
  log "dry-run: would verify '$TARGET_PATH context add local'"
  log "dry-run: would verify '$TARGET_PATH context use local'"
  log "dry-run: would verify '$TARGET_PATH context current'"
  log "dry-run: would verify '$TARGET_PATH doctor --json'"
  log "dry-run: would record install metadata in AMOF app-data"
  if [[ -n "$REGISTER_WORKSPACE_NAME" ]]; then
    log "dry-run: would register workspace '$REGISTER_WORKSPACE_NAME' for '$WORKSPACE_REPO_PATH'"
  fi
  exit 0
fi

mkdir -p "$INSTALL_DIR"
TMP_TARGET="${TARGET_PATH}.tmp"
printf '%s\n' "$WRAPPER_CONTENT" > "$TMP_TARGET"
chmod 0755 "$TMP_TARGET"
mv "$TMP_TARGET" "$TARGET_PATH"

log "verifying installed wrapper"
"$TARGET_PATH" paths --json >/dev/null
"$TARGET_PATH" context add "$CONTEXT_NAME" >/dev/null
"$TARGET_PATH" context use "$CONTEXT_NAME" >/dev/null
"$TARGET_PATH" context current >/dev/null
"$TARGET_PATH" doctor --json >/dev/null
if [[ -n "$RESOLVED_AMOF_HOME" ]]; then
  export AMOF_HOME="$RESOLVED_AMOF_HOME"
fi
PYTHONPATH="${REPO_ROOT}/scripts${PYTHONPATH:+:${PYTHONPATH}}" python3 - "$CHANNEL" "$VERSION_VALUE" <<'PY'
import sys

from amof.version_metadata import save_install_metadata

save_install_metadata(
    channel=sys.argv[1],
    version=sys.argv[2],
    install_method="local-dev-wrapper",
)
PY
if [[ -n "$REGISTER_WORKSPACE_NAME" ]]; then
  "$TARGET_PATH" workspace register --name "$REGISTER_WORKSPACE_NAME" --repo "$WORKSPACE_REPO_PATH" >/dev/null
  "$TARGET_PATH" workspace show "$REGISTER_WORKSPACE_NAME" >/dev/null
fi

if [[ "$NO_SHELL_PROFILE" -eq 1 ]]; then
  log "shell profile changes skipped"
else
  log "shell profile changes are not automatic in this installer slice"
  log "add to PATH manually if needed: export PATH=\"$INSTALL_DIR:\$PATH\""
fi

printf '\n'
printf 'Next commands:\n'
printf '  %q paths --json\n' "$TARGET_PATH"
printf '  %q context current\n' "$TARGET_PATH"

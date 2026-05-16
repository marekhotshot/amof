#!/usr/bin/env bash

set -euo pipefail

release_lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/common.sh
source "${release_lib_dir}/common.sh"

get_platform_root() {
    get_amof_root
}

load_platform_env() {
    local root="${1:-$(get_platform_root)}"
    local saved_kubeconfig="${KUBECONFIG:-}"
    if [[ -f "${root}/.env" ]]; then
        set -a
        # shellcheck disable=SC1091
        source "${root}/.env"
        set +a
    fi
    # Preserve KUBECONFIG when caller (e.g. spin.sh) set it; .env must not override
    if [[ -n "$saved_kubeconfig" ]]; then
        export KUBECONFIG="$saved_kubeconfig"
    fi
}

get_active_ticket() {
    local root="${1:-$(get_platform_root)}"
    python3 - <<'PY' "$root"
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
state_path = root / ".amof" / "state.json"
if not state_path.exists():
    print("")
    raise SystemExit(0)

data = json.loads(state_path.read_text(encoding="utf-8"))
print(data.get("active_ticket") or "")
PY
}

resolve_workspace_repo_dir() {
    local repo_name="$1"
    local root="${2:-$(get_platform_root)}"
    local active_ticket="${3:-$(get_active_ticket "$root")}"
    local source_mode="${4:-${AMOF_REPO_SOURCE_MODE:-worktree}}"
    local canonical_dir="${root}/repos/${repo_name}"

    case "$repo_name" in
        amof)
            if [[ -d "${root}/scripts/amof" ]]; then
                canonical_dir="${root}"
            fi
            ;;
        amof-ui)
            if [[ -d "${root}/apps/amof-ui" ]]; then
                canonical_dir="${root}/apps/amof-ui"
            elif [[ -d "${root}/repos/amof/apps/amof-ui" ]]; then
                canonical_dir="${root}/repos/amof/apps/amof-ui"
            else
                canonical_dir="${root}/apps/amof-ui"
            fi
            ;;
    esac

    if [[ "$source_mode" == "canonical" ]]; then
        printf '%s\n' "${canonical_dir}"
        return 0
    fi
    if [[ -n "$active_ticket" && -d "${root}/.amof-worktrees/${active_ticket}/${repo_name}" ]]; then
        printf '%s\n' "${root}/.amof-worktrees/${active_ticket}/${repo_name}"
        return 0
    fi
    printf '%s\n' "${canonical_dir}"
}

default_image_tag() {
    date +"dev-%Y%m%d%H%M%S"
}

resolve_registry_base() {
    local owner="${GITHUB_OWNER:-}"
    if [[ -z "$owner" ]]; then
        log_error "GITHUB_OWNER must be set in the environment or .env."
        return 1
    fi
    printf '%s\n' "${GHCR_REGISTRY:-ghcr.io/${owner}}"
}

resolve_ghcr_username() {
    local owner="${GITHUB_OWNER:-}"
    printf '%s\n' "${GHCR_USERNAME:-${GIT_USER:-$owner}}"
}

resolve_ghcr_token() {
    printf '%s\n' "${GHCR_TOKEN:-${GITHUB_TOKEN:-${GIT_TOKEN:-}}}"
}

is_truthy() {
    local value="${1:-}"
    case "$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|y|on)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

assert_local_profile_isolation() {
    local profile="${1:-}"
    local registry_base="${2:-}"
    local api_base_url="${3:-}"
    local supabase_public_url="${4:-}"
    local amof_host="${5:-}"
    local supabase_host="${6:-}"

    if [[ "$profile" != "local" ]]; then
        return 0
    fi

    if is_truthy "${AMOF_ALLOW_REMOTE_DB:-}"; then
        log_warn "AMOF_ALLOW_REMOTE_DB is enabled; skipping local isolation checks."
        return 0
    fi

    local violations=()
    if [[ "$registry_base" =~ ^ghcr\.io/ ]]; then
        violations+=("registry base '${registry_base}' points to GHCR")
    fi
    if [[ "$api_base_url" =~ amof\.dev|demo\.amof\.dev|supabase-demo\.amof\.dev ]]; then
        violations+=("api base '${api_base_url}' points to a cloud host")
    fi
    if [[ "$supabase_public_url" =~ amof\.dev|demo\.amof\.dev|supabase-demo\.amof\.dev ]]; then
        violations+=("supabase url '${supabase_public_url}' points to a cloud host")
    fi
    if [[ "$amof_host" =~ amof\.dev|demo\.amof\.dev ]]; then
        violations+=("amof host '${amof_host}' points to a cloud host")
    fi
    if [[ "$supabase_host" =~ amof\.dev|supabase-demo\.amof\.dev ]]; then
        violations+=("supabase host '${supabase_host}' points to a cloud host")
    fi

    if (( ${#violations[@]} > 0 )); then
        log_error "Refusing local profile deploy because remote endpoints were detected."
        for violation in "${violations[@]}"; do
            log_error " - ${violation}"
        done
        log_error "Set AMOF_ALLOW_REMOTE_DB=1 to bypass this guard intentionally."
        return 1
    fi
}

workspace_release_dir() {
    local root="${1:-$(get_platform_root)}"
    printf '%s\n' "${root}/infrastructure/helm/amof-stack"
}

#!/usr/bin/env bash
# Source-truth provenance enforcement for AMOF shell-image deploys.
#
# Bash entrypoint that delegates to scripts/lib/source_provenance.py. Sourced
# by build-cloud-images.sh and deploy-shell-image.sh so both chokepoints
# share the exact same admission decision.

# Resolve the directory of this lib file once.
_PROVENANCE_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# enforce_shell_source_provenance <component> <repo_dir>
#
# Hard-fails (non-zero) when the candidate `repo_dir` is not admissible as
# the source of an `amof-dashboard`/`amof-controlplane` image deploy.
# Component must be either `dashboard` or `controlplane`.
enforce_shell_source_provenance() {
    local component="${1:?component required (dashboard|controlplane)}"
    local repo_dir="${2:?repo_dir required}"
    shift 2

    if ! command -v python3 >/dev/null 2>&1; then
        echo "[ERROR] python3 is required to run source provenance check" >&2
        return 2
    fi

    # The AMOF workspace root is always known by the calling chokepoint
    # script (`build-cloud-images.sh`, `deploy-shell-image.sh`) as `$ROOT`,
    # so we pass it explicitly. This avoids auto-detection picking up nested
    # repos that look like workspaces (e.g. `repos/amof` ships its own
    # `repos/` and `ecosystems/` directories).
    local workspace_root="${AMOF_PROVENANCE_ROOT:-${ROOT:-}}"
    local extra_args=()
    if [[ -n "${workspace_root}" ]]; then
        extra_args+=(--workspace-root "${workspace_root}")
    fi

    python3 "${_PROVENANCE_LIB_DIR}/source_provenance.py" \
        --component "${component}" \
        --repo-dir "${repo_dir}" \
        "${extra_args[@]}" \
        "$@"
    local rc=$?
    if [[ ${rc} -ne 0 ]]; then
        echo "[ERROR] Source provenance gate rejected ${component} build/deploy from ${repo_dir}" >&2
    fi
    return ${rc}
}

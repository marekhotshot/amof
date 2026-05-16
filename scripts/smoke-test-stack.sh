#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./scripts/smoke-test-stack.sh [options]

Options:
  --namespace <name>       Namespace. Default: amof-system
  --amof-url <url>         Public AMOF URL to probe
  --supabase-url <url>     Public Supabase URL to probe
  --timeout <duration>     Rollout wait timeout. Default: 600s
EOF
}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/release.sh
source "${ROOT}/scripts/lib/release.sh"
cd "$ROOT"

NAMESPACE="amof-system"
AMOF_URL=""
SUPABASE_URL=""
TIMEOUT="600s"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace)
      NAMESPACE="${2:-}"
      shift 2
      ;;
    --amof-url)
      AMOF_URL="${2:-}"
      shift 2
      ;;
    --supabase-url)
      SUPABASE_URL="${2:-}"
      shift 2
      ;;
    --timeout)
      TIMEOUT="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

require_cmds kubectl curl

log_info "Waiting for deployments in ${NAMESPACE}"
while IFS= read -r deployment; do
  [[ -n "$deployment" ]] || continue
  kubectl -n "${NAMESPACE}" rollout status "$deployment" --timeout="${TIMEOUT}"
done < <(kubectl -n "${NAMESPACE}" get deployments -o name)

log_info "Waiting for statefulsets in ${NAMESPACE}"
while IFS= read -r statefulset; do
  [[ -n "$statefulset" ]] || continue
  kubectl -n "${NAMESPACE}" rollout status "$statefulset" --timeout="${TIMEOUT}"
done < <(kubectl -n "${NAMESPACE}" get statefulsets -o name)

probe_url() {
  local url="$1"
  local label="$2"
  local code=""
  local attempt
  for attempt in $(seq 1 30); do
    code="$(curl -ksS -o /dev/null -w "%{http_code}" "$url" || true)"
    case "$code" in
      200|302|401)
        log_success "${label} probe returned HTTP ${code}: ${url}"
        return 0
        ;;
      503|000)
        sleep 2
        ;;
      *)
        log_error "${label} probe failed with HTTP ${code}: ${url}"
        return 1
        ;;
    esac
  done
  log_error "${label} probe failed with HTTP ${code}: ${url}"
  return 1
}

if [[ -n "$AMOF_URL" ]]; then
  probe_url "${AMOF_URL}" "AMOF"
  probe_url "${AMOF_URL%/}/ready" "AMOF readiness"
fi

if [[ -n "$SUPABASE_URL" ]]; then
  probe_url "${SUPABASE_URL}" "Supabase"
fi

kubectl -n "${NAMESPACE}" get pods

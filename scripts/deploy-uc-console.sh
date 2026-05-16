#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./scripts/deploy-uc-console.sh [options]

Deploy the isolated `uc-amof.hotshot.sk` UltraConsole foundation against the prod-dev control base.

Options:
  --release <name>                  Helm release name. Default: amof-uc-console
  --namespace <name>                Namespace. Default: amof-uc
  --host <host>                     Public UltraConsole host. Default: uc-amof.hotshot.sk
  --tls-secret <name>               Optional TLS secret name. Default: none
  --image-tag <tag>                 Thinking Assistant image tag. Required unless AMOF_UC_IMAGE_TAG is set.
  --api-base-url <url>              AMOF API base URL. Default: https://amof.hotshot.sk/api/v1
  --supabase-url <url>              Supabase URL. Default: https://amof-supabase.hotshot.sk
  --supabase-anon-secret <name>     Secret containing anonKey. Default: amof-uc-supabase-jwt
  --pull-secret <name>              Image pull secret. Default: ghcr-auth
  --dry-run                         Render manifests instead of applying them
  -h, --help                        Show this help
EOF
}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/release.sh
source "${ROOT}/scripts/lib/release.sh"
cd "$ROOT"
load_platform_env "$ROOT"

CHART_DIR="${ROOT}/infrastructure/helm/amof-stack/charts/amof-thinking-assistant"
BASE_VALUES="${ROOT}/infrastructure/helm/uc-console/values-prod-dev.yaml"

RELEASE_NAME="${AMOF_UC_RELEASE_NAME:-amof-uc-console}"
NAMESPACE="${AMOF_UC_NAMESPACE:-amof-uc}"
CONSOLE_HOST="${AMOF_UC_HOST:-uc-amof.hotshot.sk}"
TLS_SECRET="${AMOF_UC_TLS_SECRET:-}"
IMAGE_TAG="${AMOF_UC_IMAGE_TAG:-}"
API_BASE_URL="${AMOF_UC_API_BASE_URL:-https://amof.hotshot.sk/api/v1}"
SUPABASE_URL="${AMOF_UC_SUPABASE_URL:-https://amof-supabase.hotshot.sk}"
SUPABASE_ANON_SECRET="${AMOF_UC_SUPABASE_ANON_SECRET:-amof-uc-supabase-jwt}"
PULL_SECRET="${AMOF_UC_PULL_SECRET:-ghcr-auth}"
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --release)
      RELEASE_NAME="${2:-}"
      shift 2
      ;;
    --namespace)
      NAMESPACE="${2:-}"
      shift 2
      ;;
    --host)
      CONSOLE_HOST="${2:-}"
      shift 2
      ;;
    --tls-secret)
      TLS_SECRET="${2:-}"
      shift 2
      ;;
    --image-tag)
      IMAGE_TAG="${2:-}"
      shift 2
      ;;
    --api-base-url)
      API_BASE_URL="${2:-}"
      shift 2
      ;;
    --supabase-url)
      SUPABASE_URL="${2:-}"
      shift 2
      ;;
    --supabase-anon-secret)
      SUPABASE_ANON_SECRET="${2:-}"
      shift 2
      ;;
    --pull-secret)
      PULL_SECRET="${2:-}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
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

if [[ -z "$IMAGE_TAG" ]]; then
  log_error "Provide --image-tag or set AMOF_UC_IMAGE_TAG."
  exit 1
fi

if [[ ! -f "$BASE_VALUES" ]]; then
  log_error "Base values file not found: $BASE_VALUES"
  exit 1
fi

if [[ ! -d "$CHART_DIR" ]]; then
  log_error "Chart directory not found: $CHART_DIR"
  exit 1
fi

temp_values="$(mktemp)"
cleanup() {
  rm -f "$temp_values"
}
trap cleanup EXIT

tls_yaml=""
if [[ -n "$TLS_SECRET" ]]; then
  tls_yaml=$(cat <<EOF
  tls:
    - hosts:
        - ${CONSOLE_HOST}
      secretName: ${TLS_SECRET}
EOF
)
fi

cat >"$temp_values" <<EOF
image:
  tag: ${IMAGE_TAG}
ingress:
${tls_yaml}
  hosts:
    - host: ${CONSOLE_HOST}
      paths:
        - path: /
          pathType: Prefix
env:
  NEXT_PUBLIC_AMOF_API: ${API_BASE_URL}
  NEXT_PUBLIC_AMOF_REGISTRATION_ENABLED: "false"
  NEXT_PUBLIC_SUPABASE_URL: ${SUPABASE_URL}
  NEXT_PUBLIC_AMOF_ECOSYSTEM: amof-platform
  NEXT_PUBLIC_AMOF_AGENT_ID: director
  NEXT_PUBLIC_AMOF_ARENA_MODE: main
secretEnv:
  NEXT_PUBLIC_SUPABASE_ANON_KEY:
    secretName: ${SUPABASE_ANON_SECRET}
    key: anonKey
    optional: false
pullSecretName: ${PULL_SECRET}
EOF

log_info "UC release: ${RELEASE_NAME}"
log_info "UC namespace: ${NAMESPACE}"
log_info "UC host: ${CONSOLE_HOST}"
log_info "UC API base: ${API_BASE_URL}"
log_info "UC Supabase URL: ${SUPABASE_URL}"
log_info "UC TLS secret: ${TLS_SECRET:-<edge/default>}"
log_info "UC image tag: ${IMAGE_TAG}"

if [[ "$DRY_RUN" -eq 1 ]]; then
  helm template "$RELEASE_NAME" "$CHART_DIR" \
    --namespace "$NAMESPACE" \
    -f "$BASE_VALUES" \
    -f "$temp_values"
  exit 0
fi

helm upgrade --install "$RELEASE_NAME" "$CHART_DIR" \
  --namespace "$NAMESPACE" \
  --create-namespace \
  -f "$BASE_VALUES" \
  -f "$temp_values"

log_info "UC rollout foundation applied for ${CONSOLE_HOST}."

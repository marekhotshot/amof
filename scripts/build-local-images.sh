#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./scripts/build-local-images.sh [tag]

Builds the AMOF control plane, dashboard, and agent images from the active
workspace and pushes them to the local k3d registry exposed on localhost:5000.
EOF
}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/release.sh
source "${ROOT}/scripts/lib/release.sh"
cd "$ROOT"
load_platform_env "$ROOT"

case "${1:-}" in
  -h|--help)
    usage
    exit 0
    ;;
esac

IMAGE_TAG="${1:-local}"
LOCAL_REGISTRY="${AMOF_LOCAL_REGISTRY:-localhost:5000}"
CLUSTER_REGISTRY="${AMOF_CLUSTER_REGISTRY:-k3d-amof-registry:5000}"
ACTIVE_TICKET="$(get_active_ticket "$ROOT")"
AMOF_DIR="$(resolve_workspace_repo_dir amof "$ROOT" "$ACTIVE_TICKET")"
AMOF_UI_DIR="$(resolve_workspace_repo_dir amof-ui "$ROOT" "$ACTIVE_TICKET")"
ASSISTANT_DIR="${ROOT}/repos/amof-assistant"
AUTHORITY_DEMO_DIR="${ROOT}/authority-demo-frontend"

require_cmds docker

CONTROLPLANE_LOCAL="${LOCAL_REGISTRY}/amof-controlplane:${IMAGE_TAG}"
DASHBOARD_LOCAL="${LOCAL_REGISTRY}/amof-dashboard:${IMAGE_TAG}"
AGENT_LOCAL="${LOCAL_REGISTRY}/amof-agent:${IMAGE_TAG}"
ASSISTANT_LOCAL="${LOCAL_REGISTRY}/amof-assistant:${IMAGE_TAG}"
AUTHORITY_DEMO_LOCAL="${LOCAL_REGISTRY}/amof-authority-demo:${IMAGE_TAG}"
CONTROLPLANE_CLUSTER="${CLUSTER_REGISTRY}/amof-controlplane:${IMAGE_TAG}"
DASHBOARD_CLUSTER="${CLUSTER_REGISTRY}/amof-dashboard:${IMAGE_TAG}"
AGENT_CLUSTER="${CLUSTER_REGISTRY}/amof-agent:${IMAGE_TAG}"
ASSISTANT_CLUSTER="${CLUSTER_REGISTRY}/amof-assistant:${IMAGE_TAG}"
AUTHORITY_DEMO_CLUSTER="${CLUSTER_REGISTRY}/amof-authority-demo:${IMAGE_TAG}"

log_info "Building local images from:"
echo "  amof:    ${AMOF_DIR}"
echo "  apps/amof-ui: ${AMOF_UI_DIR}"
if [[ -d "${ASSISTANT_DIR}" ]]; then
  echo "  assistant: ${ASSISTANT_DIR}"
fi
if [[ -d "${AUTHORITY_DEMO_DIR}" ]]; then
  echo "  authority-demo-frontend: ${AUTHORITY_DEMO_DIR}"
fi

docker build -f "${AMOF_DIR}/Dockerfile.controlplane" -t "${CONTROLPLANE_LOCAL}" "${ROOT}"
docker build -f "${AMOF_DIR}/amof-control-plane/agent/Dockerfile" -t "${AGENT_LOCAL}" "${AMOF_DIR}"
docker build -f "${AMOF_UI_DIR}/Dockerfile" -t "${DASHBOARD_LOCAL}" "${AMOF_UI_DIR}"
if [[ -d "${ASSISTANT_DIR}" ]]; then
  docker build -f "${ASSISTANT_DIR}/Dockerfile" -t "${ASSISTANT_LOCAL}" "${ASSISTANT_DIR}"
fi
if [[ -d "${AUTHORITY_DEMO_DIR}" ]]; then
  docker build -f "${AUTHORITY_DEMO_DIR}/Dockerfile" -t "${AUTHORITY_DEMO_LOCAL}" "${AUTHORITY_DEMO_DIR}"
fi

docker tag "${CONTROLPLANE_LOCAL}" "${CONTROLPLANE_CLUSTER}"
docker tag "${AGENT_LOCAL}" "${AGENT_CLUSTER}"
docker tag "${DASHBOARD_LOCAL}" "${DASHBOARD_CLUSTER}"
if [[ -d "${ASSISTANT_DIR}" ]]; then
  docker tag "${ASSISTANT_LOCAL}" "${ASSISTANT_CLUSTER}"
fi
if [[ -d "${AUTHORITY_DEMO_DIR}" ]]; then
  docker tag "${AUTHORITY_DEMO_LOCAL}" "${AUTHORITY_DEMO_CLUSTER}"
fi

docker push "${CONTROLPLANE_LOCAL}"
docker push "${AGENT_LOCAL}"
docker push "${DASHBOARD_LOCAL}"
if [[ -d "${ASSISTANT_DIR}" ]]; then
  docker push "${ASSISTANT_LOCAL}"
fi
if [[ -d "${AUTHORITY_DEMO_DIR}" ]]; then
  docker push "${AUTHORITY_DEMO_LOCAL}"
fi

cat <<EOF

Local images are ready:
  ${CONTROLPLANE_CLUSTER}
  ${AGENT_CLUSTER}
  ${DASHBOARD_CLUSTER}
  ${ASSISTANT_CLUSTER}
  ${AUTHORITY_DEMO_CLUSTER}
EOF

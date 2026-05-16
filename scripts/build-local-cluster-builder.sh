#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./scripts/build-local-cluster-builder.sh [--tag <tag>] [--host-registry <host>] [--cluster-registry <host>]

Build the AMOF cluster-builder image locally and push it to the local k3d registry.

Defaults:
  --tag               local
  --host-registry     localhost:5000
  --cluster-registry  k3d-amof-registry:5000
EOF
}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAG="local"
HOST_REGISTRY="${AMOF_LOCAL_REGISTRY_HOST:-localhost:5000}"
CLUSTER_REGISTRY="${AMOF_LOCAL_CLUSTER_REGISTRY:-k3d-amof-registry:5000}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag)
      TAG="${2:-}"
      shift 2
      ;;
    --host-registry)
      HOST_REGISTRY="${2:-}"
      shift 2
      ;;
    --cluster-registry)
      CLUSTER_REGISTRY="${2:-}"
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

HOST_IMAGE="${HOST_REGISTRY}/amof-cluster-builder:${TAG}"
CLUSTER_IMAGE="${CLUSTER_REGISTRY}/amof-cluster-builder:${TAG}"

docker build -f "${ROOT}/Dockerfile.cluster-builder" -t "${HOST_IMAGE}" "${ROOT}"
docker tag "${HOST_IMAGE}" "${CLUSTER_IMAGE}"
docker push "${HOST_IMAGE}"

cat <<EOF
Local builder image ready:
  host push ref: ${HOST_IMAGE}
  cluster pull ref: ${CLUSTER_IMAGE}
EOF

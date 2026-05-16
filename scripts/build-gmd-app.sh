#!/usr/bin/env bash
#
# build-gmd-app.sh — first-party gmd-app source build (PoC slices UP8-14..UP8-20).
#
# Builds a single gmd-app service from the canonical source repo at
# repos/gmd-app and publishes the resulting image to the local k3d
# registry at host.k3d.internal:5000/gmd/<service>:<tag>.
#
# ARCHITECTURAL LAYERS (UP8-20 — extraction of shared runtime build
# primitives). This script is intentionally one-file and exposes a
# very small surface, but the layers it composes are explicit:
#
#   * Shared runtime primitive: `build_one_image` below. Takes a
#     Dockerfile path, a build context, two image references (host
#     push alias + in-cluster pull alias), and a push flag. This is
#     all the shared shell-level build logic that each runtime family
#     (Go, Python, Node, .NET, Java) needs from us. Per-language
#     details (base images, multi-stage flow, language-specific
#     install steps) live in each service's upstream Dockerfile, NOT
#     here.
#   * Per-ecosystem mapping: `SERVICE_DOCKERFILE` / `SERVICE_CONTEXT`
#     associative arrays below. They translate `--service <name>` to
#     a (dockerfile_relpath, context_relpath) pair scoped to one
#     ecosystem source tree (`repos/gmd-app`). A second ecosystem
#     would supply its own copy of these maps in its own
#     `build-<ecosystem>-app.sh` and reuse `build_one_image` as-is.
#   * Per-substrate registry: `AMOF_GMD_APP_PUSH_REGISTRY` and
#     `AMOF_GMD_APP_PULL_REGISTRY` env vars (with local-k3d defaults).
#     They control where docker push goes and what alias gets baked
#     into the image tags so in-cluster pulls work. A different
#     substrate (e.g. cloud-dev) supplies different values.
#
# This slice is a contract proof, not a generic downstream framework:
#
#   * Only the explicitly-listed services in the SERVICE_DOCKERFILE
#     dispatch table are supported. Other services are intentionally
#     rejected so the boundary stays explicit and reviewable.
#     Currently supported: frontend, emailservice, currencyservice,
#     shippingservice, productcatalogservice, checkoutservice,
#     recommendationservice, paymentservice, loadgenerator,
#     cartservice (nested context src/cartservice/src), adservice.
#     All 11 chart-default first-party gmd services are now covered.
#     Intentionally deferred: shoppingassistantservice (chart-default
#     off).
#   * The script is host-only; it is not yet routed into the in-cluster
#     lifecycle build dispatcher and the gmd ecosystem build contract is
#     still `mirror_upstream` (UP8-13). Build remains hidden in the UI.
#
# Usage:
#   scripts/build-gmd-app.sh --service <name> --tag <tag> [--no-push]
#
# Environment overrides:
#   AMOF_GMD_APP_DIR              path to the gmd-app source checkout
#                                 (default: <workspace>/repos/gmd-app)
#   AMOF_GMD_APP_PUSH_REGISTRY    where the host pushes the built image
#                                 (default: localhost:5000)
#   AMOF_GMD_APP_PULL_REGISTRY    in-cluster pull alias used in the
#                                 final image tag (default:
#                                 host.k3d.internal:5000)
#   AMOF_GMD_APP_REGISTRY_NAMESPACE  registry namespace for gmd-app
#                                    images (default: gmd)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AMOF_REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

usage() {
  cat <<'EOF'
Usage: scripts/build-gmd-app.sh --service <name> --tag <tag> [--no-push]

Required arguments:
  --service <name>   gmd-app service to build. PoC supports the
                     services listed in the SERVICE_DOCKERFILE dispatch
                     table only (currently: frontend, emailservice,
                     currencyservice, shippingservice,
                     productcatalogservice, checkoutservice,
                     recommendationservice, paymentservice,
                     loadgenerator, cartservice, adservice).
  --tag <tag>        Image tag to apply (e.g. gmd-dev-c9857ee).

Optional:
  --no-push          Build only; do not push to the local registry.
  -h, --help         Show this help.

Environment overrides:
  AMOF_GMD_APP_DIR
  AMOF_GMD_APP_PUSH_REGISTRY
  AMOF_GMD_APP_PULL_REGISTRY
  AMOF_GMD_APP_REGISTRY_NAMESPACE
EOF
}

# Shared runtime primitive (UP8-20). Builds one image from a Dockerfile
# and a build context, applies both the host push alias and the
# in-cluster pull alias, and optionally pushes the host alias to the
# registry. Behavior is intentionally identical for every runtime
# family: language-specific concerns belong in the Dockerfile, not
# here. Substrate concerns (which registry to push to, which alias to
# bake into the image) are controlled by the caller via the image
# arguments. A second ecosystem can reuse this function verbatim by
# supplying its own ecosystem mapping and substrate config.
build_one_image() {
  local dockerfile_path="$1"
  local context_path="$2"
  local push_image="$3"
  local pull_image="$4"
  local no_push="$5"

  if [[ ! -f "$dockerfile_path" ]]; then
    echo "Dockerfile missing: ${dockerfile_path}" >&2
    return 1
  fi
  if [[ ! -d "$context_path" ]]; then
    echo "Build context missing: ${context_path}" >&2
    return 1
  fi

  docker build -f "$dockerfile_path" -t "$push_image" -t "$pull_image" "$context_path"

  if [[ "$no_push" -eq 0 ]]; then
    docker push "$push_image"
    echo "Pushed: ${push_image}"
    echo "Pullable inside k3d as: ${pull_image}"
  else
    echo "Skipping push (--no-push). Local image: ${push_image}"
  fi
}

resolve_workspace_root() {
  local repo_root="$1"
  local override="${AMOF_WORKSPACE_ROOT:-}"
  if [[ -n "$override" ]]; then
    if [[ ! -d "$override" ]]; then
      echo "Configured AMOF_WORKSPACE_ROOT does not exist: ${override}" >&2
      exit 1
    fi
    (cd "$override" && pwd)
    return 0
  fi
  local repo_parent
  repo_parent="$(dirname "$repo_root")"
  local repo_grandparent
  repo_grandparent="$(dirname "$repo_parent")"
  if [[ "$(basename "$repo_parent")" == "repos" && -d "${repo_grandparent}/repos/amof" ]]; then
    printf '%s\n' "$repo_grandparent"
    return 0
  fi
  if [[ -d "${repo_parent}/amof" || -d "${repo_parent}/amof-ui" ]]; then
    printf '%s\n' "$repo_parent"
    return 0
  fi
  printf '%s\n' "$repo_root"
}

WORKSPACE_ROOT="$(resolve_workspace_root "$AMOF_REPO_ROOT")"

SERVICE=""
TAG=""
NO_PUSH=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --service)
      SERVICE="${2:-}"
      shift 2
      ;;
    --tag)
      TAG="${2:-}"
      shift 2
      ;;
    --no-push)
      NO_PUSH=1
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

if [[ -z "$SERVICE" ]]; then
  echo "--service is required" >&2
  usage >&2
  exit 1
fi
if [[ -z "$TAG" ]]; then
  echo "--tag is required" >&2
  usage >&2
  exit 1
fi

GMD_APP_DIR="${AMOF_GMD_APP_DIR:-${WORKSPACE_ROOT}/repos/gmd-app}"
PUSH_REGISTRY="${AMOF_GMD_APP_PUSH_REGISTRY:-localhost:5000}"
PULL_REGISTRY="${AMOF_GMD_APP_PULL_REGISTRY:-host.k3d.internal:5000}"
REGISTRY_NAMESPACE="${AMOF_GMD_APP_REGISTRY_NAMESPACE:-gmd}"

if [[ ! -d "$GMD_APP_DIR" ]]; then
  echo "gmd-app source not found at: ${GMD_APP_DIR}" >&2
  exit 1
fi

# PoC scope: explicit per-service dispatch. Adding a new service is a
# deliberate two-line change here. The wider service set is deferred to
# later slices so the boundary stays explicit and reviewable.
declare -A SERVICE_DOCKERFILE
declare -A SERVICE_CONTEXT
SERVICE_DOCKERFILE[frontend]="src/frontend/Dockerfile"
SERVICE_CONTEXT[frontend]="src/frontend"
SERVICE_DOCKERFILE[emailservice]="src/emailservice/Dockerfile"
SERVICE_CONTEXT[emailservice]="src/emailservice"
SERVICE_DOCKERFILE[currencyservice]="src/currencyservice/Dockerfile"
SERVICE_CONTEXT[currencyservice]="src/currencyservice"
SERVICE_DOCKERFILE[shippingservice]="src/shippingservice/Dockerfile"
SERVICE_CONTEXT[shippingservice]="src/shippingservice"
SERVICE_DOCKERFILE[productcatalogservice]="src/productcatalogservice/Dockerfile"
SERVICE_CONTEXT[productcatalogservice]="src/productcatalogservice"
SERVICE_DOCKERFILE[checkoutservice]="src/checkoutservice/Dockerfile"
SERVICE_CONTEXT[checkoutservice]="src/checkoutservice"
SERVICE_DOCKERFILE[recommendationservice]="src/recommendationservice/Dockerfile"
SERVICE_CONTEXT[recommendationservice]="src/recommendationservice"
SERVICE_DOCKERFILE[paymentservice]="src/paymentservice/Dockerfile"
SERVICE_CONTEXT[paymentservice]="src/paymentservice"
SERVICE_DOCKERFILE[loadgenerator]="src/loadgenerator/Dockerfile"
SERVICE_CONTEXT[loadgenerator]="src/loadgenerator"
# cartservice is the only service whose canonical Dockerfile lives one
# directory deeper than the service folder. Both DOCKERFILE and CONTEXT
# point at `src/cartservice/src`, mirroring the upstream skaffold.yaml.
SERVICE_DOCKERFILE[cartservice]="src/cartservice/src/Dockerfile"
SERVICE_CONTEXT[cartservice]="src/cartservice/src"
SERVICE_DOCKERFILE[adservice]="src/adservice/Dockerfile"
SERVICE_CONTEXT[adservice]="src/adservice"

if [[ -z "${SERVICE_DOCKERFILE[$SERVICE]:-}" ]]; then
  supported="$(printf '%s ' "${!SERVICE_DOCKERFILE[@]}" | sed 's/ $//')"
  echo "Unsupported service: ${SERVICE}." >&2
  echo "Supported services: ${supported}." >&2
  exit 2
fi

DOCKERFILE_RELPATH="${SERVICE_DOCKERFILE[$SERVICE]}"
CONTEXT_RELPATH="${SERVICE_CONTEXT[$SERVICE]}"
DOCKERFILE_PATH="${GMD_APP_DIR}/${DOCKERFILE_RELPATH}"
CONTEXT_PATH="${GMD_APP_DIR}/${CONTEXT_RELPATH}"

PUSH_IMAGE="${PUSH_REGISTRY}/${REGISTRY_NAMESPACE}/${SERVICE}:${TAG}"
PULL_IMAGE="${PULL_REGISTRY}/${REGISTRY_NAMESPACE}/${SERVICE}:${TAG}"

echo "gmd-app source build summary:"
echo "  workspace root:    ${WORKSPACE_ROOT}"
echo "  amof repo root:    ${AMOF_REPO_ROOT}"
echo "  gmd-app dir:       ${GMD_APP_DIR}"
echo "  service:           ${SERVICE}"
echo "  dockerfile:        ${DOCKERFILE_PATH}"
echo "  build context:     ${CONTEXT_PATH}"
echo "  push image:        ${PUSH_IMAGE}"
echo "  pull image (k3d):  ${PULL_IMAGE}"
echo "  push:              $([[ $NO_PUSH -eq 1 ]] && echo no || echo yes)"
echo ""

build_one_image "$DOCKERFILE_PATH" "$CONTEXT_PATH" "$PUSH_IMAGE" "$PULL_IMAGE" "$NO_PUSH"

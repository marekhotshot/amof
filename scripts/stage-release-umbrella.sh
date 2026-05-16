#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./scripts/stage-release-umbrella.sh

Create a temporary umbrella chart directory that points directly at the current
canonical chart sources for this workspace/worktree context.

The script prints the staged directory path on stdout.
EOF
}

if [[ $# -gt 0 ]]; then
  case "${1:-}" in
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
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/release.sh
source "${ROOT}/scripts/lib/release.sh"

load_platform_env "$ROOT"

STAGED_DIR="$(mktemp -d)"

BASE_CHART_DIR="${ROOT}/charts/amof-platform"
CONTROL_PLANE_SOURCE_DIR="${ROOT}/infrastructure/helm/amof-stack/charts/amof-control-plane"
SUPABASE_SOURCE_DIR="${ROOT}/infrastructure/helm/amof-stack/charts/supabase"
SUPABASE_PACKAGED_CHART="${ROOT}/infrastructure/helm/amof-stack/charts/supabase-0.5.0.tgz"
ASSISTANT_SOURCE_DIR="${ROOT}/infrastructure/helm/amof-stack/charts/amof-assistant"
AUTHORITY_DEMO_SOURCE_DIR="${ROOT}/infrastructure/helm/amof-stack/charts/amof-authority-demo"
N8N_SOURCE_DIR="${ROOT}/infrastructure/helm/amof-stack/charts/n8n"

if [[ ! -d "${BASE_CHART_DIR}" ]]; then
  log_error "Umbrella chart not found at ${BASE_CHART_DIR}"
  exit 1
fi

if [[ ! -d "${CONTROL_PLANE_SOURCE_DIR}" ]]; then
  log_error "AMOF control-plane chart not found at ${CONTROL_PLANE_SOURCE_DIR}"
  exit 1
fi

if [[ ! -d "${SUPABASE_SOURCE_DIR}" ]]; then
  log_error "Supabase chart not found at ${SUPABASE_SOURCE_DIR}"
  exit 1
fi

if [[ ! -f "${SUPABASE_SOURCE_DIR}/values.yaml" ]]; then
  if [[ ! -f "${SUPABASE_PACKAGED_CHART}" ]]; then
    log_error "Supabase chart at ${SUPABASE_SOURCE_DIR} is incomplete and no packaged fallback exists at ${SUPABASE_PACKAGED_CHART}"
    exit 1
  fi
  EXTRACTED_SUPABASE_DIR="${STAGED_DIR}/_sources/supabase"
  mkdir -p "${EXTRACTED_SUPABASE_DIR}"
  tar -xzf "${SUPABASE_PACKAGED_CHART}" -C "${EXTRACTED_SUPABASE_DIR}"
  SUPABASE_SOURCE_DIR="${EXTRACTED_SUPABASE_DIR}/supabase"
fi

if [[ ! -d "${ASSISTANT_SOURCE_DIR}" ]]; then
  log_error "AMOF assistant chart not found at ${ASSISTANT_SOURCE_DIR}"
  exit 1
fi

if [[ ! -d "${AUTHORITY_DEMO_SOURCE_DIR}" ]]; then
  log_error "AMOF authority-demo chart not found at ${AUTHORITY_DEMO_SOURCE_DIR}"
  exit 1
fi

if [[ ! -d "${N8N_SOURCE_DIR}" ]]; then
  log_error "n8n chart not found at ${N8N_SOURCE_DIR}"
  exit 1
fi

cp -a "${BASE_CHART_DIR}/." "${STAGED_DIR}/"
rm -rf "${STAGED_DIR}/charts" "${STAGED_DIR}/Chart.lock"

python3 - <<'PY' "${STAGED_DIR}/Chart.yaml" "${CONTROL_PLANE_SOURCE_DIR}" "${SUPABASE_SOURCE_DIR}" "${ASSISTANT_SOURCE_DIR}" "${AUTHORITY_DEMO_SOURCE_DIR}" "${N8N_SOURCE_DIR}"
from pathlib import Path
import re
import sys

chart_path = Path(sys.argv[1])
control_plane_dir = sys.argv[2]
supabase_dir = sys.argv[3]
assistant_dir = sys.argv[4]
authority_demo_dir = sys.argv[5]
n8n_dir = sys.argv[6]

text = chart_path.read_text(encoding="utf-8")

text = re.sub(
    r'(- name: amof-control-plane\s+version: [^\n]+\s+repository: )file://[^\n]+',
    rf'\1file://{control_plane_dir}',
    text,
    count=1,
    flags=re.MULTILINE,
)
text = re.sub(
    r'(- name: supabase\s+version: [^\n]+\s+repository: )file://[^\n]+',
    rf'\1file://{supabase_dir}',
    text,
    count=1,
    flags=re.MULTILINE,
)
text = re.sub(
    r'(- name: amof-assistant\s+version: [^\n]+\s+repository: )file://[^\n]+',
    rf'\1file://{assistant_dir}',
    text,
    count=1,
    flags=re.MULTILINE,
)
text = re.sub(
    r'(- name: amof-authority-demo\s+version: [^\n]+\s+repository: )file://[^\n]+',
    rf'\1file://{authority_demo_dir}',
    text,
    count=1,
    flags=re.MULTILINE,
)
text = re.sub(
    r'(- name: n8n\s+version: [^\n]+\s+repository: )file://[^\n]+',
    rf'\1file://{n8n_dir}',
    text,
    count=1,
    flags=re.MULTILINE,
)

chart_path.write_text(text, encoding="utf-8")
PY

printf '%s\n' "${STAGED_DIR}"

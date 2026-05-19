#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${ROOT_DIR}/dist"
ARTIFACT_PATH="${AMOF_STANDALONE_ARTIFACT_PATH:-${DIST_DIR}/amof}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
BUILD_ROOT="$(mktemp -d /tmp/amof-standalone-build.XXXXXX)"
VENV_DIR="${BUILD_ROOT}/venv"

cleanup() {
  rm -rf "${BUILD_ROOT}"
}
trap cleanup EXIT

log() {
  printf '[build-standalone-amof] %s\n' "$*"
}

"${PYTHON_BIN}" - <<'PY'
from __future__ import annotations

import sys

if sys.version_info < (3, 11):
    raise SystemExit("AMOF standalone build requires Python 3.11+")
PY

log "using repo root: ${ROOT_DIR}"
mkdir -p "${DIST_DIR}"

log "creating build virtualenv"
"${PYTHON_BIN}" -m venv "${VENV_DIR}"

VENV_PYTHON="${VENV_DIR}/bin/python"
PEX_BIN="${VENV_DIR}/bin/pex"

log "installing pex build dependency"
"${VENV_PYTHON}" -m pip install --upgrade pip setuptools wheel pex

log "building standalone pex artifact"
rm -f "${ARTIFACT_PATH}"
(
  cd "${ROOT_DIR}"
  "${PEX_BIN}" . \
    -c amof \
    -o "${ARTIFACT_PATH}" \
    --python-shebang='/usr/bin/env python3'
)
chmod 0755 "${ARTIFACT_PATH}"

log "validating standalone artifact entrypoint"
"${ARTIFACT_PATH}" --version

printf 'AMOF_STANDALONE_BUILD_PASS\n'
printf 'Artifact: %s\n' "${ARTIFACT_PATH}"

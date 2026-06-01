#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARTIFACT_PATH="${1:-${AMOF_STANDALONE_ARTIFACT_PATH:-${ROOT_DIR}/dist/amof}}"
SMOKE_ROOT="$(mktemp -d /tmp/amof-standalone-smoke.XXXXXX)"
TARGET_REPO="${SMOKE_ROOT}/target-repo"
FAKE_BIN="${SMOKE_ROOT}/fake-bin"
LOG_DIR="${SMOKE_ROOT}/logs"
AMOF_HOME_DIR="${SMOKE_ROOT}/amof-home"

mkdir -p "${TARGET_REPO}" "${FAKE_BIN}" "${LOG_DIR}"

if [[ ! -f "${ARTIFACT_PATH}" ]]; then
  echo "FAIL: standalone artifact not found at ${ARTIFACT_PATH}" >&2
  exit 1
fi

if [[ ! -x "${ARTIFACT_PATH}" ]]; then
  echo "FAIL: standalone artifact is not executable at ${ARTIFACT_PATH}" >&2
  exit 1
fi

cat > "${FAKE_BIN}/cursor" <<'EOF'
#!/usr/bin/env bash
sleep 10
EOF
chmod 0755 "${FAKE_BIN}/cursor"

(
  cd "${SMOKE_ROOT}"
  env -u AMOF_ROOT -u AMOF_CWD -u PYTHONPATH \
    PATH="${FAKE_BIN}:${PATH}" \
    AMOF_HOME="${AMOF_HOME_DIR}" \
    "${ARTIFACT_PATH}" --version > "${LOG_DIR}/version.txt" 2>&1
  env -u AMOF_ROOT -u AMOF_CWD -u PYTHONPATH \
    PATH="${FAKE_BIN}:${PATH}" \
    AMOF_HOME="${AMOF_HOME_DIR}" \
    "${ARTIFACT_PATH}" check > "${LOG_DIR}/check.txt" 2>&1
  env -u AMOF_ROOT -u AMOF_CWD -u PYTHONPATH \
    PATH="${FAKE_BIN}:${PATH}" \
    AMOF_HOME="${AMOF_HOME_DIR}" \
    "${ARTIFACT_PATH}" doctor > "${LOG_DIR}/doctor.txt" 2>&1
  env -u AMOF_ROOT -u AMOF_CWD -u PYTHONPATH \
    PATH="${FAKE_BIN}:${PATH}" \
    AMOF_HOME="${AMOF_HOME_DIR}" \
    "${ARTIFACT_PATH}" setup provider --list > "${LOG_DIR}/provider-list.txt" 2>&1
  env -u AMOF_ROOT -u AMOF_CWD -u PYTHONPATH \
    PATH="${FAKE_BIN}:${PATH}" \
    AMOF_HOME="${AMOF_HOME_DIR}" \
    "${ARTIFACT_PATH}" setup provider bedrock --print-template > "${LOG_DIR}/bedrock-template.txt" 2>&1
)

(
  cd "${TARGET_REPO}"
  git init -q -b main
  git config user.email smoke@example.local
  git config user.name "AMOF Smoke"
  printf '# AMOF standalone smoke\n' > README.md
  git add README.md
  git commit -q -m "init"
  env -u AMOF_ROOT -u AMOF_CWD -u PYTHONPATH \
    PATH="${FAKE_BIN}:${PATH}" \
    AMOF_HOME="${AMOF_HOME_DIR}" \
    "${ARTIFACT_PATH}" init --adopt . --name standalone-smoke > "${LOG_DIR}/init.txt" 2>&1
  git status --short > "${LOG_DIR}/target-status.txt"
)

if ! rg -q '^AMOF v3\.0\.3$' "${LOG_DIR}/version.txt"; then
  echo "FAIL: standalone artifact did not report AMOF v3.0.3" >&2
  exit 1
fi

if ! rg -q 'installed \(version probe timed out: cursor --version\)' "${LOG_DIR}/check.txt"; then
  echo "FAIL: standalone artifact check did not report the timed out cursor version probe" >&2
  exit 1
fi

if ! rg -q '^AMOF doctor: ' "${LOG_DIR}/doctor.txt"; then
  echo "FAIL: standalone artifact doctor did not run" >&2
  exit 1
fi

if ! rg -q '^  - bedrock:' "${LOG_DIR}/provider-list.txt"; then
  echo "FAIL: standalone artifact provider list did not include bedrock" >&2
  exit 1
fi

if ! rg -q '^provider: bedrock$' "${LOG_DIR}/bedrock-template.txt"; then
  echo "FAIL: standalone artifact bedrock template did not render" >&2
  exit 1
fi

if [[ -s "${LOG_DIR}/target-status.txt" ]]; then
  echo "FAIL: target repo was modified by standalone artifact init --adopt ." >&2
  cat "${LOG_DIR}/target-status.txt" >&2
  exit 1
fi

python3 - "${TARGET_REPO}" "${LOG_DIR}/source-noise.txt" <<'PY'
from __future__ import annotations

import pathlib
import sys

target_repo = pathlib.Path(sys.argv[1])
log_path = pathlib.Path(sys.argv[2])
hits = []
for candidate in target_repo.rglob("*"):
    if candidate.is_dir() and candidate.name in {".amof", "ecosystems", "context"}:
        hits.append(str(candidate))
log_path.write_text("\n".join(hits), encoding="utf-8")
PY

if [[ -s "${LOG_DIR}/source-noise.txt" ]]; then
  echo "FAIL: source pollution detected in target repo" >&2
  cat "${LOG_DIR}/source-noise.txt" >&2
  exit 1
fi

printf 'AMOF_STANDALONE_SMOKE_PASS\n'
printf 'Artifact: %s\n' "${ARTIFACT_PATH}"
printf 'Smoke root: %s\n' "${SMOKE_ROOT}"

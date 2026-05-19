#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SMOKE_ROOT="$(mktemp -d /tmp/amof-no-pipx-smoke.XXXXXX)"
SRC_DIR="${SMOKE_ROOT}/src"
TARGET_REPO="${SMOKE_ROOT}/target-repo"
FAKE_BIN="${SMOKE_ROOT}/fake-bin"
LOG_DIR="${SMOKE_ROOT}/logs"
AMOF_HOME_DIR="${SMOKE_ROOT}/amof-home"

mkdir -p "${SRC_DIR}" "${TARGET_REPO}" "${FAKE_BIN}" "${LOG_DIR}"

python3 - "${ROOT_DIR}" "${SRC_DIR}" <<'PY'
from __future__ import annotations

import pathlib
import shutil
import sys

src = pathlib.Path(sys.argv[1])
dst = pathlib.Path(sys.argv[2])

ignore = shutil.ignore_patterns(
    ".git",
    ".venv",
    "build",
    "__pycache__",
    "*.pyc",
    ".pytest_cache",
    ".mypy_cache",
)

for child in src.iterdir():
    if ignore(src, [child.name]):
        continue
    target = dst / child.name
    if child.is_dir():
        shutil.copytree(child, target, ignore=ignore)
    else:
        shutil.copy2(child, target)
PY

cat > "${FAKE_BIN}/cursor" <<'EOF'
#!/usr/bin/env bash
sleep 10
EOF
chmod 0755 "${FAKE_BIN}/cursor"

(
  cd "${SRC_DIR}"
  ./scripts/install-amof.sh > "${LOG_DIR}/install.txt" 2>&1
)

AMOF_BIN="${SRC_DIR}/.venv/bin/amof"
[[ -x "${AMOF_BIN}" ]] || {
  echo "FAIL: amof executable missing after no-pipx install" >&2
  exit 1
}

(
  cd "${SRC_DIR}"
  env -u AMOF_ROOT -u AMOF_CWD -u PYTHONPATH \
    PATH="${FAKE_BIN}:${PATH}" \
    AMOF_HOME="${AMOF_HOME_DIR}" \
    "${AMOF_BIN}" --version > "${LOG_DIR}/version.txt" 2>&1
  env -u AMOF_ROOT -u AMOF_CWD -u PYTHONPATH \
    PATH="${FAKE_BIN}:${PATH}" \
    AMOF_HOME="${AMOF_HOME_DIR}" \
    "${AMOF_BIN}" check > "${LOG_DIR}/check.txt" 2>&1
  env -u AMOF_ROOT -u AMOF_CWD -u PYTHONPATH \
    PATH="${FAKE_BIN}:${PATH}" \
    AMOF_HOME="${AMOF_HOME_DIR}" \
    "${AMOF_BIN}" doctor > "${LOG_DIR}/doctor.txt" 2>&1
  env -u AMOF_ROOT -u AMOF_CWD -u PYTHONPATH \
    PATH="${FAKE_BIN}:${PATH}" \
    AMOF_HOME="${AMOF_HOME_DIR}" \
    "${AMOF_BIN}" setup provider --list > "${LOG_DIR}/provider-list.txt" 2>&1
  env -u AMOF_ROOT -u AMOF_CWD -u PYTHONPATH \
    PATH="${FAKE_BIN}:${PATH}" \
    AMOF_HOME="${AMOF_HOME_DIR}" \
    "${AMOF_BIN}" setup provider bedrock --print-template > "${LOG_DIR}/bedrock-template.txt" 2>&1
)

(
  cd "${TARGET_REPO}"
  git init -q -b main
  git config user.email smoke@example.local
  git config user.name "AMOF Smoke"
  printf '# AMOF smoke\n' > README.md
  git add README.md
  git commit -q -m "init"
  env -u AMOF_ROOT -u AMOF_CWD -u PYTHONPATH \
    PATH="${FAKE_BIN}:${PATH}" \
    AMOF_HOME="${AMOF_HOME_DIR}" \
    "${AMOF_BIN}" init --adopt . --name smoke > "${LOG_DIR}/init.txt" 2>&1
  git status --short > "${LOG_DIR}/target-status.txt"
)

if ! rg -q '^AMOF v' "${LOG_DIR}/version.txt"; then
  echo "FAIL: amof --version did not report a version" >&2
  exit 1
fi

if ! rg -q 'installed \(version probe timed out: cursor --version\)' "${LOG_DIR}/check.txt"; then
  echo "FAIL: amof check did not report the timed out cursor version probe" >&2
  exit 1
fi

if ! rg -q '^  - bedrock:' "${LOG_DIR}/provider-list.txt"; then
  echo "FAIL: provider list did not include bedrock" >&2
  exit 1
fi

if ! rg -q '^provider: bedrock$' "${LOG_DIR}/bedrock-template.txt"; then
  echo "FAIL: bedrock template did not render" >&2
  exit 1
fi

if [[ -s "${LOG_DIR}/target-status.txt" ]]; then
  echo "FAIL: target repo was modified by amof init --adopt ." >&2
  cat "${LOG_DIR}/target-status.txt" >&2
  exit 1
fi

find "${TARGET_REPO}" -maxdepth 3 -type d \( -name .amof -o -name ecosystems -o -name context \) > "${LOG_DIR}/source-noise.txt"
if [[ -s "${LOG_DIR}/source-noise.txt" ]]; then
  echo "FAIL: source pollution detected in target repo" >&2
  cat "${LOG_DIR}/source-noise.txt" >&2
  exit 1
fi

printf 'AMOF_NO_PIPX_SMOKE_PASS\n'
printf 'Smoke root: %s\n' "${SMOKE_ROOT}"

#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_STAMP="$(date -u +%Y%m%d-%H%M%S)"
RUN_DIR="${ROOT_DIR}/receipts/execution-e2e-smoke/${RUN_STAMP}"
LOG_DIR="${RUN_DIR}/logs"
AMOF_HOME_DIR="${RUN_DIR}/amof-home"
INTAKE_ID="amof-execution-e2e-safe-${RUN_STAMP}"

mkdir -p "${LOG_DIR}" "${AMOF_HOME_DIR}"

export AMOF_HOME="${AMOF_HOME_DIR}"
unset AMOF_REMOTE_IAL_BASE_URL || true
unset AMOF_REMOTE_IAL_API_KEY || true

require_json_field() {
  local file="$1"
  local key="$2"
  python3 - "$file" "$key" <<'PY'
import json
import sys

payload_path = sys.argv[1]
field = sys.argv[2]
with open(payload_path, "r", encoding="utf-8") as fh:
    payload = json.load(fh)
value = payload.get(field)
if value is None or str(value).strip() == "":
    raise SystemExit(1)
print(str(value))
PY
}

cat > "${RUN_DIR}/intake.yaml" <<EOF
id: ${INTAKE_ID}
version: "1.0.0"
kind: bounded_intake_task
ticket_id: AMOF-EXECUTION-E2E-SMOKE-SCRIPT-001
rough_intent: Prove smallest safe AMOF intake-to-execution evidence flow.
bounded_goal: Run one bounded read-only loop and collect reproducible evidence.
task_kind: other
repo_scope:
  - .
paths_to_inspect:
  - scripts/amof/commands/intake.py
  - scripts/amof/commands/runner.py
  - scripts/amof/commands/execution.py
  - scripts/amof/commands/loop.py
  - scripts/amof/commands/runs.py
profile_ref: local-planning-runner
mutations:
  allowed: []
  forbidden:
    - edit
    - deploy
    - promote
    - push
validation_gates:
  - name: read_only
    requirement: Planning-only execution with no mutation.
    failure_action: stop
cost_truth_policy:
  missing_cost_representation: unknown
mode: planning_only
mutation_mode: read_only
dispatch_allowed: false
remote_execution: false
stop_policy:
  terminal_on_ready: false
EOF

amof context use local > "${LOG_DIR}/context-use.txt" 2>&1
amof context current > "${LOG_DIR}/context-current.txt" 2>&1

amof runner template --kind local-planning > "${RUN_DIR}/runner.yaml"
amof runner register "${RUN_DIR}/runner.yaml" --json > "${LOG_DIR}/runner-register.json"
amof runner doctor --json > "${LOG_DIR}/runner-doctor.json"

amof intake validate "${RUN_DIR}/intake.yaml" --json > "${LOG_DIR}/intake-validate.json"
amof intake submit "${RUN_DIR}/intake.yaml" --json > "${LOG_DIR}/intake-submit.json"

amof runner match "${INTAKE_ID}" --json > "${LOG_DIR}/runner-match.json"

amof execution scan "${INTAKE_ID}" --json > "${LOG_DIR}/execution-scan.json"
SCAN_ID="$(require_json_field "${LOG_DIR}/execution-scan.json" "scan_id")"
amof execution report "${SCAN_ID}" --json > "${LOG_DIR}/execution-report.json"

amof loop run "${INTAKE_ID}" --max-loops 1 --json > "${LOG_DIR}/loop-run.json"
LOOP_RUN_ID="$(require_json_field "${LOG_DIR}/loop-run.json" "run_id")"
amof loop show "${LOOP_RUN_ID}" --json > "${LOG_DIR}/loop-show.json"
amof loop logs "${LOOP_RUN_ID}" --json > "${LOG_DIR}/loop-logs.json"

amof runs list --json > "${LOG_DIR}/runs-list.json"
amof runs show "${LOOP_RUN_ID}" --json > "${LOG_DIR}/runs-show.json"
amof runs logs "${LOOP_RUN_ID}" --json > "${LOG_DIR}/runs-logs.json"

REPORT_PATH="${RUN_DIR}/smoke-report.md"
{
  echo "# AMOF Execution E2E Smoke Report"
  echo
  echo "- run_stamp: ${RUN_STAMP}"
  echo "- run_dir: ${RUN_DIR}"
  echo "- amof_home: ${AMOF_HOME_DIR}"
  echo "- intake_id: ${INTAKE_ID}"
  echo "- scan_id: ${SCAN_ID}"
  echo "- loop_run_id: ${LOOP_RUN_ID}"
  echo
  echo "## Command Chain"
  echo
  echo "1. amof intake validate ${RUN_DIR}/intake.yaml --json"
  echo "2. amof intake submit ${RUN_DIR}/intake.yaml --json"
  echo "3. amof runner match ${INTAKE_ID} --json"
  echo "4. amof execution scan ${INTAKE_ID} --json"
  echo "5. amof execution report ${SCAN_ID} --json"
  echo "6. amof loop run ${INTAKE_ID} --max-loops 1 --json"
  echo "7. amof loop show ${LOOP_RUN_ID} --json"
  echo "8. amof loop logs ${LOOP_RUN_ID} --json"
  echo "9. amof runs list --json"
  echo "10. amof runs show ${LOOP_RUN_ID} --json"
  echo "11. amof runs logs ${LOOP_RUN_ID} --json"
  echo
  echo "## Evidence Artifacts"
  echo
  echo "- ${LOG_DIR}/intake-submit.json"
  echo "- ${LOG_DIR}/runner-match.json"
  echo "- ${LOG_DIR}/execution-scan.json"
  echo "- ${LOG_DIR}/execution-report.json"
  echo "- ${LOG_DIR}/loop-run.json"
  echo "- ${LOG_DIR}/loop-show.json"
  echo "- ${LOG_DIR}/loop-logs.json"
  echo "- ${LOG_DIR}/runs-list.json"
  echo "- ${LOG_DIR}/runs-show.json"
  echo "- ${LOG_DIR}/runs-logs.json"
} > "${REPORT_PATH}"

echo "AMOF_EXECUTION_E2E_SMOKE_PASS"
echo "report_path=${REPORT_PATH}"

#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d)"
FAKE_KUBECTL="$TMP_DIR/kubectl"
PORT_FILE="$TMP_DIR/port"

cleanup() {
  if [ -n "${SERVER_PID:-}" ]; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" >/dev/null 2>&1 || true
  fi
  rm -rf "$TMP_DIR"
}

trap cleanup EXIT

cat > "$FAKE_KUBECTL" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
cmd="${*: -1}"
sh -c "$cmd"
EOF
chmod +x "$FAKE_KUBECTL"

start_mock_server() {
  local mode="$1"
  local request_file="$2"
  : > "$PORT_FILE"
  MOCK_MODE="$mode" REQUEST_FILE="$request_file" python3 - "$PORT_FILE" <<'PY' &
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

port_file = sys.argv[1]
mode = os.environ["MOCK_MODE"]
request_file = os.environ["REQUEST_FILE"]
state = {"run_calls": 0}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in {"/health", "/ready"}:
            self._send(200, {"ok": True})
            return
        if self.path == "/openapi.json":
            paths = {
                "/api/v1/control/startTask": {"post": {}},
                "/api/v1/runs/{run_id}": {"get": {}},
            }
            if mode != "fallback":
                paths["/api/v1/runs/{run_id}/status"] = {"get": {}}
            self._send(200, {"paths": paths})
            return
        if self.path == "/api/v1/runs/run-123/status":
            state["run_calls"] += 1
            if mode == "success" and state["run_calls"] == 1:
                self._send(200, {
                    "run_id": "run-123",
                    "ecosystem": "test-ecosystem",
                    "action": "agent",
                    "status": "running",
                    "created_at": "2026-03-18T11:59:00Z",
                    "terminal": False,
                    "last_message": "Run started",
                })
            elif mode == "success":
                self._send(200, {
                    "run_id": "run-123",
                    "ecosystem": "test-ecosystem",
                    "action": "agent",
                    "status": "success",
                    "created_at": "2026-03-18T11:59:00Z",
                    "finished_at": "2026-03-18T12:00:00Z",
                    "exit_code": 0,
                    "terminal": True,
                    "last_message": "Run finished cleanly",
                })
            elif mode == "unknown":
                self._send(404, {"detail": "Run not found"})
            else:
                self._send(500, {"detail": f"Unsupported mock mode: {mode}"})
            return
        if self.path == "/api/v1/runs/run-123":
            state["run_calls"] += 1
            if mode == "fallback" and state["run_calls"] == 1:
                self._send(200, {
                    "run_id": "run-123",
                    "status": "running",
                    "events": [{"message": "Run started"}],
                })
            elif mode == "fallback":
                self._send(200, {
                    "run_id": "run-123",
                    "status": "success",
                    "finished_at": "2026-03-18T12:00:00Z",
                    "exit_code": 0,
                    "events": [
                        {"message": "Run started"},
                        {"message": "Run finished cleanly"},
                    ],
                })
            elif mode == "unknown":
                self._send(404, {"detail": "Run not found"})
            else:
                self._send(500, {"detail": f"Unsupported mock mode: {mode}"})
            return
        self._send(404, {"detail": "not found"})

    def do_POST(self):
        if self.path == "/api/v1/control/startTask":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            with open(request_file, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            self._send(200, {"run_id": "run-123", "queue_status": "queued", "run_status": "queued"})
            return
        self._send(404, {"detail": "not found"})

server = HTTPServer(("127.0.0.1", 0), Handler)
with open(port_file, "w", encoding="utf-8") as fh:
    fh.write(str(server.server_port))
server.serve_forever()
PY
  SERVER_PID="$!"
}

wait_for_port() {
  for _ in $(seq 1 50); do
    if [ -s "$PORT_FILE" ]; then
      return 0
    fi
    sleep 0.1
  done
  return 1
}

stop_mock_server() {
  if [ -n "${SERVER_PID:-}" ]; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" >/dev/null 2>&1 || true
    SERVER_PID=""
  fi
}

run_case() {
  local mode="$1"
  local expected_exit="$2"
  local run_log="$TMP_DIR/amof-orch-run-${mode}.log"
  local request_file="$TMP_DIR/request-${mode}.json"

  start_mock_server "$mode" "$request_file"
  if ! wait_for_port; then
    echo "mock server did not start" >&2
    exit 1
  fi

  PORT="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").strip())' "$PORT_FILE")"

  set +e
  AMOF_ORCH_RUN_API="http://127.0.0.1:${PORT}" \
  KUBECTL_BIN="$FAKE_KUBECTL" \
  AMOF_ORCH_MAX_POLLS=3 \
  AMOF_ORCH_POLL_INTERVAL_SEC=0 \
  AMOF_ORCH_RUN_LOG="$run_log" \
  AMOF_ORCH_RUN_ECOSYSTEM="test-ecosystem" \
  AMOF_ORCH_RUN_PROMPT="Inspect runner's shell quoting and finish cleanly." \
  AMOF_ORCH_RUN_MODE="plan-execute" \
  AMOF_ORCH_RUN_RUNTIME_PROFILE="prod_dev" \
  AMOF_ORCH_RUN_AGENT_ID="master" \
  AMOF_ORCH_RUN_TRIGGER_KIND="manual" \
  bash "$ROOT/scripts/amof-orch-run" >/dev/null
  rc="$?"
  set -e

  stop_mock_server

  if [ "$rc" -ne "$expected_exit" ]; then
    echo "unexpected exit code for mode=$mode: got $rc expected $expected_exit" >&2
    exit 1
  fi

  python3 - "$mode" "$run_log" "$request_file" <<'PY'
import json
import pathlib
import sys

mode = sys.argv[1]
log = pathlib.Path(sys.argv[2]).read_text(encoding="utf-8")
request = json.loads(pathlib.Path(sys.argv[3]).read_text(encoding="utf-8"))

assert "# run_id=run-123" in log, log
assert "# run_id=" not in log.replace("# run_id=run-123", ""), log
assert "# queue_status=queued" in log, log
assert "# start_run_status=queued" in log, log
assert '"prompt":"Inspect runner\'s shell quoting and finish cleanly."' in log, log
assert request == {
    "ecosystem": "test-ecosystem",
    "prompt": "Inspect runner's shell quoting and finish cleanly.",
    "mode": "plan-execute",
    "runtime_profile": "prod_dev",
    "agent_id": "master",
    "trigger_kind": "manual",
}, request

if mode == "success":
    assert "# run_status_path=/api/v1/runs/{run_id}/status" in log, log
    assert "# final run_id=run-123 status=success finished_at=2026-03-18T12:00:00Z exit_code=0" in log, log
    assert "# final_message=Run finished cleanly" in log, log
    poll_count = sum(1 for line in log.splitlines() if line.startswith("# poll="))
    assert poll_count == 2, log
    assert "# poll=1 run_id=run-123 lookup=status status=running" in log, log
elif mode == "fallback":
    assert "# run_details_path=/api/v1/runs/{run_id}" in log, log
    assert "# final run_id=run-123 status=success finished_at=2026-03-18T12:00:00Z exit_code=0" in log, log
    assert "# final_message=Run finished cleanly" in log, log
    assert "# poll=1 run_id=run-123 lookup=details status=running" in log, log
    poll_count = sum(1 for line in log.splitlines() if line.startswith("# poll="))
    assert poll_count == 2, log
elif mode == "unknown":
    assert "# run_status_path=/api/v1/runs/{run_id}/status" in log, log
    assert "# poll=1 run_id=run-123 lookup=status status=unknown reason=run lookup failed" in log, log
    assert "# final run_id=run-123 status=unknown reason=run did not reach a terminal state within 3 polls last_known_status=unknown" in log, log
    poll_count = sum(1 for line in log.splitlines() if line.startswith("# poll="))
    assert poll_count == 3, log
else:
    raise AssertionError(f"Unhandled mode {mode}")
PY
}

run_case success 0
run_case fallback 0
run_case unknown 2
echo "amof-orch-run verification passed"

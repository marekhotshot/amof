#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d)"
PORT_FILE="$TMP_DIR/port"
REQUEST_LOG="$TMP_DIR/request-log.jsonl"

cleanup() {
  if [[ -n "${SERVER_PID:-}" ]]; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" >/dev/null 2>&1 || true
  fi
  rm -rf "$TMP_DIR"
}

trap cleanup EXIT

start_mock_server() {
  : > "$PORT_FILE"
  python3 - "$PORT_FILE" "$REQUEST_LOG" <<'PY' &
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

port_file = sys.argv[1]
request_log = sys.argv[2]

state = {
    "sandbox_id": "sbx-1",
    "running_polls": 0,
}


def append_log(entry):
    with open(request_log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, status, payload):
        body = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return ""
        return self.rfile.read(length).decode("utf-8")

    def _log_request(self, body=""):
        append_log(
            {
                "method": self.command,
                "path": self.path,
                "headers": {k: v for k, v in self.headers.items()},
                "body": body,
            }
        )

    def do_GET(self):
        self._log_request()
        parsed = urlparse(self.path)

        if parsed.path == "/ping":
            self._json(200, {"ok": True})
            return

        if parsed.path == f"/v1/sandboxes/{state['sandbox_id']}":
            state["running_polls"] += 1
            current_state = "Pending" if state["running_polls"] == 1 else "Running"
            self._json(200, {"id": state["sandbox_id"], "status": {"state": current_state}})
            return

        if parsed.path == f"/v1/sandboxes/{state['sandbox_id']}/endpoints/44772":
            query = parse_qs(parsed.query)
            self._json(200, {"endpoint": "server-proxy", "use_server_proxy": query.get("use_server_proxy") == ["true"]})
            return

        if parsed.path == f"/v1/sandboxes/{state['sandbox_id']}/proxy/44772/ping":
            self._json(200, {"ok": True})
            return

        if parsed.path == f"/v1/sandboxes/{state['sandbox_id']}/proxy/44772/command/status/cmd-1":
            self._json(200, {"running": False, "exitCode": 0})
            return

        if parsed.path == f"/v1/sandboxes/{state['sandbox_id']}/proxy/44772/command/cmd-1/logs":
            self._text(200, "amof-opensandbox-ok\n")
            return

        self._json(404, {"detail": "not found"})

    def do_POST(self):
        body = self._read_body()
        self._log_request(body)
        parsed = urlparse(self.path)

        if parsed.path == "/v1/sandboxes":
            self._json(202, {"id": state["sandbox_id"]})
            return

        if parsed.path == f"/v1/sandboxes/{state['sandbox_id']}/proxy/44772/command":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"type":"init","text":"cmd-1"}\n\n')
            return

        self._json(404, {"detail": "not found"})

    def do_DELETE(self):
        self._log_request()
        parsed = urlparse(self.path)
        if parsed.path == f"/v1/sandboxes/{state['sandbox_id']}":
            self._json(204, {})
            return
        self._json(404, {"detail": "not found"})


server = HTTPServer(("127.0.0.1", 0), Handler)
with open(port_file, "w", encoding="utf-8") as fh:
    fh.write(str(server.server_port))
server.serve_forever()
PY
  SERVER_PID="$!"
}

wait_for_port() {
  for _ in $(seq 1 50); do
    if [[ -s "$PORT_FILE" ]]; then
      return 0
    fi
    sleep 0.1
  done
  return 1
}

start_mock_server
wait_for_port
PORT="$(cat "$PORT_FILE")"
BASE_URL="http://127.0.0.1:${PORT}"

python3 "$ROOT/scripts/opensandbox-smoke.py" \
  --base-url "$BASE_URL" \
  --api-key "api-key-1" \
  --execd-token "execd-token-1" \
  --image-uri "python:3.11-slim" \
  --timeout-seconds 60 \
  --poll-timeout-seconds 5 \
  > "$TMP_DIR/full-run.txt"

python3 "$ROOT/scripts/opensandbox-smoke.py" \
  --base-url "$BASE_URL" \
  --api-key "api-key-1" \
  --skip-execd \
  --timeout-seconds 60 \
  --poll-timeout-seconds 5 \
  > "$TMP_DIR/skip-run.txt"

python3 - "$REQUEST_LOG" "$TMP_DIR/full-run.txt" "$TMP_DIR/skip-run.txt" <<'PY'
import json
import pathlib
import sys

request_log = pathlib.Path(sys.argv[1])
full_output = pathlib.Path(sys.argv[2]).read_text(encoding="utf-8")
skip_output = pathlib.Path(sys.argv[3]).read_text(encoding="utf-8")
entries = [json.loads(line) for line in request_log.read_text(encoding="utf-8").splitlines() if line.strip()]

assert "[opensandbox-smoke] PASS" in full_output, full_output
assert "[opensandbox-smoke] execd checks skipped by request" in skip_output, skip_output

create_calls = [entry for entry in entries if entry["method"] == "POST" and entry["path"] == "/v1/sandboxes"]
assert len(create_calls) == 2, create_calls
assert all(entry["headers"].get("OPEN-SANDBOX-API-KEY") == "api-key-1" for entry in create_calls), create_calls

command_calls = [entry for entry in entries if entry["path"] == "/v1/sandboxes/sbx-1/proxy/44772/command"]
assert len(command_calls) == 1, command_calls
assert command_calls[0]["headers"].get("X-EXECD-ACCESS-TOKEN") == "execd-token-1", command_calls

delete_calls = [entry for entry in entries if entry["method"] == "DELETE" and entry["path"] == "/v1/sandboxes/sbx-1"]
assert len(delete_calls) == 2, delete_calls
PY

set +e
missing_output="$(
  python3 "$ROOT/scripts/opensandbox-smoke.py" 2>&1
)"
missing_rc="$?"
set -e
if [[ "$missing_rc" -eq 0 ]]; then
  echo "expected missing base URL to fail" >&2
  exit 1
fi
if ! grep -q "Missing OpenSandbox base URL" <<<"$missing_output"; then
  echo "expected missing base URL error message, got: $missing_output" >&2
  exit 1
fi

echo "opensandbox-smoke verification passed"

#!/usr/bin/env bash
# Deterministic local demo for Kubernetes capability authority v0.
# Uses the in-process fixture executor. No cluster required.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export AMOF_HOME="${AMOF_HOME:-$(mktemp -d /tmp/amof-k8s-capability-XXXX)}"
export PYTHONPATH="${ROOT}/scripts${PYTHONPATH:+:$PYTHONPATH}"
AMOF=(python3 -m amof)

echo "AMOF_HOME=${AMOF_HOME}"

propose="$("${AMOF[@]}" scope propose \
  --capability kubernetes.mutate \
  --cluster local-fixture \
  --namespace demo \
  --verb get --verb list --verb patch \
  --resource deployments \
  --from-run AMOF-CAPABILITY-AUTHORITY-V0-K8S-001 \
  --requested-by worker:demo \
  --reason "patch demo/web replicas" \
  --json)"
proposal_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["proposal_id"])' <<<"$propose")"
echo "proposal_id=${proposal_id}"

approve="$("${AMOF[@]}" scope approve "$proposal_id" --ttl 30m --approved-by operator:demo --json)"
approval_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["approval_id"])' <<<"$approve")"
echo "approval_id=${approval_id}"

set +e
oos="$("${AMOF[@]}" scope execute \
  --approval "$approval_id" \
  --cluster local-fixture \
  --namespace kube-system \
  --verb patch \
  --resource deployments \
  --name web \
  --patch-replicas 3 \
  --run-id exec-oos \
  --mission-id AMOF-CAPABILITY-AUTHORITY-V0-K8S-001 \
  --requested-by worker:demo \
  --json)"
oos_rc=$?
set -e
echo "$oos"
if [[ "$oos_rc" -eq 0 ]]; then
  echo "ERROR: out-of-scope execute unexpectedly succeeded" >&2
  exit 1
fi

ok="$("${AMOF[@]}" scope execute \
  --approval "$approval_id" \
  --cluster local-fixture \
  --namespace demo \
  --verb patch \
  --resource deployments \
  --name web \
  --patch-replicas 3 \
  --run-id exec-ok \
  --mission-id AMOF-CAPABILITY-AUTHORITY-V0-K8S-001 \
  --requested-by worker:demo \
  --json)"
echo "$ok"
printf '%s\n' "$ok" | python3 -c '
import json, sys
payload = json.load(sys.stdin)
receipt = payload["receipt"]
assert payload["ok"] is True
assert receipt["acceptance_state"] == "PASS"
assert receipt["within_scope"] is True
assert receipt["verification"]["verified"] is True
print("receipt_id=" + receipt["receipt_id"])
print("acceptance_state=" + receipt["acceptance_state"])
'

echo "DEMO PASS"

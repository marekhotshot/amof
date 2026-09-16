"""Runtime Authority demo orchestrator.

Wraps canonical authority APIs. Does not execute without proposal → approval
→ binding → enforcement. Vertical stories are fixtures, not products.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .capability import CAPABILITY_KUBERNETES_MUTATE, CAPABILITY_REFERENCE_ACTION
from .kubernetes_capability import (
    ACCEPTANCE_PASS,
    ALLOWED_ANNOTATION_KEY,
    FixtureKubernetesExecutor,
    KubernetesCapabilityStore,
    KubernetesOperation,
    approve_proposal as approve_k8s,
    execute_kubernetes_capability,
    propose_kubernetes_capability,
)
from .kubernetes_live import LiveKubernetesExecutor, save_target_registry
from .reference_capability import (
    RecordingReferenceTransport,
    ReferenceCapabilityStore,
    ReferenceExecutor,
    ReferenceOperation,
    approve_proposal as approve_ref,
    execute_reference_capability,
    propose_reference_capability,
)
from .write_scope_approvals import approve_proposal as approve_write_scope
from .write_scope_bindings import create_binding as bind_write_scope, git_rev_parse_head
from .write_scope_enforcement import (
    COMPLIANCE_WITHIN_SCOPE,
    enforce_write_scope_mutations,
    guardrail_write_allowed,
    load_scope_roots_for_binding,
)
from .write_scope_proposals import build_proposal_record, save_proposal

MISSION = "AMOF-15MIN-RUNTIME-AUTHORITY-DEMO-001"
WORKER = "worker:demo"
OPERATOR = "operator:demo"

MODE_REAL = "REAL"
MODE_LOCAL_REFERENCE = "LOCAL REFERENCE SYSTEM"
MODE_K8S_FIXTURE = "LOCAL REFERENCE SYSTEM"
MODE_K8S_LIVE = "REAL"


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""
    invoked_transport: bool | None = None


@dataclass
class ScenarioResult:
    scenario: str
    title: str
    mode: str
    ok: bool
    steps: list[Step] = field(default_factory=list)
    receipt: dict[str, Any] | None = None
    receipt_path: str | None = None
    evidence_path: str | None = None
    inspect_command: str = ""
    non_claims: list[str] = field(default_factory=list)


def _store_home(home: str | None) -> str:
    if home:
        return home
    return tempfile.mkdtemp(prefix="amof-demo-")


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _run_reference(
    *,
    home: str,
    title: str,
    scenario: str,
    body: dict[str, Any],
    initial_state: dict[str, dict[str, Any]],
    allowed: ReferenceOperation,
    blocked: list[ReferenceOperation],
    ttl: str,
    non_claims: list[str],
) -> ScenarioResult:
    os.environ["AMOF_HOME"] = home
    store = ReferenceCapabilityStore.from_env()
    transport = RecordingReferenceTransport(initial_state)
    executor = ReferenceExecutor(transport)
    proposal = propose_reference_capability(
        run_id=f"{scenario}-propose",
        body=body,
        requested_by=WORKER,
        store=store,
    )
    approval = approve_ref(
        proposal["proposal_id"],
        ttl=ttl,
        approved_by=OPERATOR,
        store=store,
    )
    steps = [
        Step("REQUEST", True, proposal["proposal_id"]),
        Step("APPROVAL", True, f"{approval['approval_id']} ttl={ttl}"),
    ]
    blocked_ok = True
    for index, operation in enumerate(blocked, start=1):
        before_calls = len(transport.calls)
        outcome = execute_reference_capability(
            operation=operation,
            approval_id=approval["approval_id"],
            executor=executor,
            store=store,
        )
        invoked = len(transport.calls) > before_calls
        blocked_ok = blocked_ok and (not outcome.ok) and (not invoked)
        steps.append(
            Step(
                "BLOCKED ACTION" if index == 1 else f"BLOCKED ACTION {index}",
                not outcome.ok and not invoked,
                outcome.code,
                invoked_transport=invoked,
            )
        )
    allowed_outcome = execute_reference_capability(
        operation=allowed,
        approval_id=approval["approval_id"],
        executor=executor,
        store=store,
    )
    receipt = allowed_outcome.receipt or {}
    verified = bool(receipt.get("verification", {}).get("verified")) and receipt.get("acceptance_state") == ACCEPTANCE_PASS
    steps.append(Step("BINDING", True, (allowed_outcome.binding or {}).get("binding_id") or ""))
    steps.append(
        Step(
            "ALLOWED ACTION",
            bool(allowed_outcome.ok),
            allowed.action,
            invoked_transport=bool(transport.calls),
        )
    )
    steps.append(Step("VERIFICATION", verified, receipt.get("acceptance_state") or ""))
    receipt_path = None
    if receipt.get("receipt_id"):
        receipt_path = str(store.receipts_dir / f"{receipt['receipt_id']}.json")
    evidence = _write_json(
        Path(home) / "demo" / f"{scenario}-evidence.json",
        {
            "scenario": scenario,
            "mode": MODE_LOCAL_REFERENCE,
            "proposal_id": proposal["proposal_id"],
            "approval_id": approval["approval_id"],
            "receipt": receipt,
            "transport_calls": transport.calls,
        },
    )
    return ScenarioResult(
        scenario=scenario,
        title=title,
        mode=MODE_LOCAL_REFERENCE,
        ok=blocked_ok and bool(allowed_outcome.ok) and verified,
        steps=steps,
        receipt=receipt,
        receipt_path=receipt_path,
        evidence_path=str(evidence),
        inspect_command=f"amof demo {scenario} --show-receipt" if receipt_path else "",
        non_claims=non_claims,
    )


def run_migration(home: str) -> ScenarioResult:
    batches = [f"customer-batch-{index:03d}" for index in range(1, 101)]
    body = {
        "capability": CAPABILITY_REFERENCE_ACTION,
        "system": "local-migration",
        "objects": batches,
        "actions": ["migrate"],
        "constraints": {"target": "cloud-native-target-b"},
        "denied_actions": ["delete"],
        "reason": "migrate approved customer batch to approved target",
    }
    state = {item: {"location": "legacy-system-a", "generation": 1} for item in batches}
    state["customer-batch-101"] = {"location": "legacy-system-a", "generation": 1}
    return _run_reference(
        home=home,
        title="Migration",
        scenario="migration",
        body=body,
        initial_state=state,
        ttl="30m",
        allowed=ReferenceOperation(
            system="local-migration",
            object="customer-batch-001",
            action="migrate",
            params={"target": "cloud-native-target-b"},
            mission_id=MISSION,
            requested_by=WORKER,
            run_id="migration-allowed",
        ),
        blocked=[
            ReferenceOperation(
                system="local-migration",
                object="customer-batch-101",
                action="migrate",
                params={"target": "cloud-native-target-b"},
                mission_id=MISSION,
                requested_by=WORKER,
                run_id="migration-blocked-object",
            ),
            ReferenceOperation(
                system="local-migration",
                object="customer-batch-001",
                action="migrate",
                params={"target": "other-cloud"},
                mission_id=MISSION,
                requested_by=WORKER,
                run_id="migration-blocked-target",
            ),
            ReferenceOperation(
                system="local-migration",
                object="customer-batch-001",
                action="delete",
                params={},
                mission_id=MISSION,
                requested_by=WORKER,
                run_id="migration-blocked-delete",
            ),
        ],
        non_claims=["AMOF does not perform or automate real workload migration."],
    )


def run_security(home: str) -> ScenarioResult:
    return _run_reference(
        home=home,
        title="Security / IAM",
        scenario="security",
        body={
            "capability": CAPABILITY_REFERENCE_ACTION,
            "system": "local-iam",
            "objects": ["user-x"],
            "actions": ["disable"],
            "denied_actions": ["delete", "modify_policy"],
            "reason": "disable one named reference user",
        },
        initial_state={"user-x": {"enabled": True, "generation": 1}, "user-y": {"enabled": True, "generation": 1}},
        ttl="10m",
        allowed=ReferenceOperation(
            system="local-iam",
            object="user-x",
            action="disable",
            params={},
            mission_id=MISSION,
            requested_by=WORKER,
            run_id="iam-allowed",
        ),
        blocked=[
            ReferenceOperation(
                system="local-iam",
                object="user-x",
                action="delete",
                params={},
                mission_id=MISSION,
                requested_by=WORKER,
                run_id="iam-blocked-delete",
            ),
            ReferenceOperation(
                system="local-iam",
                object="user-x",
                action="modify_policy",
                params={},
                mission_id=MISSION,
                requested_by=WORKER,
                run_id="iam-blocked-policy",
            ),
            ReferenceOperation(
                system="local-iam",
                object="user-y",
                action="disable",
                params={},
                mission_id=MISSION,
                requested_by=WORKER,
                run_id="iam-blocked-other-user",
            ),
        ],
        non_claims=["AMOF does not integrate with a real IAM provider."],
    )


def run_insurance(home: str) -> ScenarioResult:
    return _run_reference(
        home=home,
        title="Insurance",
        scenario="insurance",
        body={
            "capability": CAPABILITY_REFERENCE_ACTION,
            "system": "local-insurance",
            "objects": ["CLM-10293"],
            "actions": ["set_status"],
            "constraints": {"status": "APPROVED"},
            "denied_actions": ["set_payout"],
            "reason": "advance one reference claim REVIEW → APPROVED",
        },
        initial_state={
            "CLM-10293": {"status": "REVIEW", "payout": 0, "generation": 1},
            "CLM-99999": {"status": "REVIEW", "payout": 0, "generation": 1},
        },
        ttl="30m",
        allowed=ReferenceOperation(
            system="local-insurance",
            object="CLM-10293",
            action="set_status",
            params={"status": "APPROVED"},
            mission_id=MISSION,
            requested_by=WORKER,
            run_id="insurance-allowed",
        ),
        blocked=[
            ReferenceOperation(
                system="local-insurance",
                object="CLM-10293",
                action="set_payout",
                params={"amount": 5000},
                mission_id=MISSION,
                requested_by=WORKER,
                run_id="insurance-blocked-payout",
            ),
            ReferenceOperation(
                system="local-insurance",
                object="CLM-99999",
                action="set_status",
                params={"status": "APPROVED"},
                mission_id=MISSION,
                requested_by=WORKER,
                run_id="insurance-blocked-claim",
            ),
        ],
        non_claims=["AMOF does not integrate with a real insurer or decide claims."],
    )


def run_banking(home: str) -> ScenarioResult:
    return _run_reference(
        home=home,
        title="Banking",
        scenario="banking",
        body={
            "capability": CAPABILITY_REFERENCE_ACTION,
            "system": "local-banking",
            "objects": ["acct-1001"],
            "actions": ["adjust"],
            "constraints": {"max_amount": 100},
            "reason": "bounded adjustment on one reference account",
        },
        initial_state={
            "acct-1001": {"value": 0, "generation": 1},
            "acct-2002": {"value": 0, "generation": 1},
        },
        ttl="30m",
        allowed=ReferenceOperation(
            system="local-banking",
            object="acct-1001",
            action="adjust",
            params={"amount": 40},
            mission_id=MISSION,
            requested_by=WORKER,
            run_id="banking-allowed",
        ),
        blocked=[
            ReferenceOperation(
                system="local-banking",
                object="acct-2002",
                action="adjust",
                params={"amount": 40},
                mission_id=MISSION,
                requested_by=WORKER,
                run_id="banking-blocked-account",
            ),
            ReferenceOperation(
                system="local-banking",
                object="acct-1001",
                action="adjust",
                params={"amount": 500},
                mission_id=MISSION,
                requested_by=WORKER,
                run_id="banking-blocked-amount",
            ),
        ],
        non_claims=[
            "AMOF does not perform regulated credit or financial decisions.",
            "AMOF does not integrate with a real bank.",
        ],
    )


def run_healthcare(home: str) -> ScenarioResult:
    return _run_reference(
        home=home,
        title="Healthcare",
        scenario="healthcare",
        body={
            "capability": CAPABILITY_REFERENCE_ACTION,
            "system": "local-healthcare",
            "objects": ["patient-ref-01"],
            "actions": ["append_note"],
            "denied_actions": ["set_diagnosis", "set_medication"],
            "reason": "append one operational note on a reference record",
        },
        initial_state={"patient-ref-01": {"notes": [], "diagnosis": None, "medication": None, "generation": 1}},
        ttl="30m",
        allowed=ReferenceOperation(
            system="local-healthcare",
            object="patient-ref-01",
            action="append_note",
            params={"note": "operational follow-up scheduled"},
            mission_id=MISSION,
            requested_by=WORKER,
            run_id="healthcare-allowed",
        ),
        blocked=[
            ReferenceOperation(
                system="local-healthcare",
                object="patient-ref-01",
                action="set_diagnosis",
                params={"diagnosis": "example"},
                mission_id=MISSION,
                requested_by=WORKER,
                run_id="healthcare-blocked-diagnosis",
            ),
            ReferenceOperation(
                system="local-healthcare",
                object="patient-ref-01",
                action="set_medication",
                params={"medication": "example"},
                mission_id=MISSION,
                requested_by=WORKER,
                run_id="healthcare-blocked-medication",
            ),
        ],
        non_claims=["AMOF does not provide clinical decision authority or a real EHR integration."],
    )


def _run_kubernetes_live(home: str) -> ScenarioResult:
    if not live_cluster_tools_available():
        raise RuntimeError("live Kubernetes requested but k3d+kubectl were not found")
    cluster_name = "amof-demo"
    kubeconfig = Path(home) / "amof-demo.kubeconfig"
    subprocess.run(["k3d", "cluster", "delete", cluster_name], check=False, capture_output=True)
    created = subprocess.run(
        ["k3d", "cluster", "create", cluster_name, "--wait", "--timeout", "180s"],
        check=False,
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        raise RuntimeError(f"disposable k3d cluster create failed: {created.stderr}")
    try:
        written = subprocess.run(
            ["k3d", "kubeconfig", "write", cluster_name, "--output", str(kubeconfig)],
            check=False,
            capture_output=True,
            text=True,
        )
        if written.returncode != 0 or not kubeconfig.is_file():
            raise RuntimeError("failed to write disposable kubeconfig")
        context = f"k3d-{cluster_name}"
        env = os.environ.copy()
        env["KUBECONFIG"] = str(kubeconfig)
        for argv in (
            ["kubectl", "--context", context, "create", "namespace", "amof-cap-test"],
            [
                "kubectl",
                "--context",
                context,
                "-n",
                "amof-cap-test",
                "create",
                "deployment",
                "demo",
                "--image=nginx:1.27-alpine",
            ],
        ):
            completed = subprocess.run(argv, check=False, capture_output=True, text=True, env=env)
            if completed.returncode != 0:
                raise RuntimeError(f"disposable cluster fixture failed: {completed.stderr}")
        os.environ["AMOF_HOME"] = home
        os.environ["AMOF_K8S_TARGETS_FILE"] = str(Path(home) / "targets.json")
        save_target_registry(
            {"local-kind": {"kubeconfig": str(kubeconfig), "context": context}},
            path=Path(home) / "targets.json",
        )
        store = KubernetesCapabilityStore.from_env()
        executor = LiveKubernetesExecutor()
        proposal = propose_kubernetes_capability(
            run_id="k8s-live-propose",
            body={
                "capability": CAPABILITY_KUBERNETES_MUTATE,
                "cluster": "local-kind",
                "namespace": "amof-cap-test",
                "verbs": ["get", "patch"],
                "resources": ["deployments"],
                "reason": "live demo annotation probe",
            },
            requested_by=WORKER,
            store=store,
        )
        approval = approve_k8s(proposal["proposal_id"], ttl="30m", approved_by=OPERATOR, store=store)
        blocked = execute_kubernetes_capability(
            operation=KubernetesOperation(
                cluster="local-kind",
                namespace="other-ns",
                verb="patch",
                resource="deployments",
                name="demo",
                patch={"annotation": {"key": ALLOWED_ANNOTATION_KEY, "value": "amof-demo-live"}},
                mission_id=MISSION,
                requested_by=WORKER,
                run_id="k8s-live-blocked",
            ),
            approval_id=approval["approval_id"],
            executor=executor,
            store=store,
        )
        allowed = execute_kubernetes_capability(
            operation=KubernetesOperation(
                cluster="local-kind",
                namespace="amof-cap-test",
                verb="patch",
                resource="deployments",
                name="demo",
                patch={"annotation": {"key": ALLOWED_ANNOTATION_KEY, "value": "amof-demo-live"}},
                mission_id=MISSION,
                requested_by=WORKER,
                run_id="k8s-live-allowed",
            ),
            approval_id=approval["approval_id"],
            executor=executor,
            store=store,
        )
        receipt = allowed.receipt or {}
        verified = receipt.get("acceptance_state") == ACCEPTANCE_PASS and bool(
            receipt.get("verification", {}).get("verified")
        )
        receipt_path = None
        if receipt.get("receipt_id"):
            receipt_path = str(store.receipts_dir / f"{receipt['receipt_id']}.json")
        evidence = _write_json(
            Path(home) / "demo" / "kubernetes-live-evidence.json",
            {
                "scenario": "kubernetes",
                "mode": MODE_K8S_LIVE,
                "proposal_id": proposal["proposal_id"],
                "approval_id": approval["approval_id"],
                "blocked_code": blocked.code,
                "receipt": receipt,
            },
        )
        return ScenarioResult(
            scenario="kubernetes",
            title="Kubernetes / Infrastructure",
            mode=MODE_K8S_LIVE,
            ok=(not blocked.ok) and bool(allowed.ok) and verified,
            steps=[
                Step("REQUEST", True, proposal["proposal_id"]),
                Step("APPROVAL", True, approval["approval_id"]),
                Step("BINDING", True, (allowed.binding or {}).get("binding_id") or ""),
                Step("ALLOWED ACTION", bool(allowed.ok), "live annotation patch", invoked_transport=True),
                Step("BLOCKED ACTION", not blocked.ok, blocked.code, invoked_transport=False),
                Step("VERIFICATION", verified, receipt.get("acceptance_state") or ""),
            ],
            receipt=receipt,
            receipt_path=receipt_path,
            evidence_path=str(evidence),
            inspect_command="amof demo kubernetes --show-receipt",
            non_claims=["Live mode uses a disposable local cluster only. It is not cloud-dev."],
        )
    finally:
        subprocess.run(["k3d", "cluster", "delete", cluster_name], check=False, capture_output=True)


def run_kubernetes(home: str, *, live: bool = False) -> ScenarioResult:
    os.environ["AMOF_HOME"] = home
    if live:
        return _run_kubernetes_live(home)
    store = KubernetesCapabilityStore.from_env()
    executor = FixtureKubernetesExecutor()
    proposal = propose_kubernetes_capability(
        run_id="k8s-propose",
        body={
            "capability": CAPABILITY_KUBERNETES_MUTATE,
            "cluster": "local-fixture",
            "namespace": "demo",
            "verbs": ["get", "patch"],
            "resources": ["deployments"],
            "reason": "patch demo/web replicas",
        },
        requested_by=WORKER,
        store=store,
    )
    approval = approve_k8s(proposal["proposal_id"], ttl="30m", approved_by=OPERATOR, store=store)
    blocked = execute_kubernetes_capability(
        operation=KubernetesOperation(
            cluster="local-fixture",
            namespace="kube-system",
            verb="patch",
            resource="deployments",
            name="web",
            patch={"replicas": 3},
            mission_id=MISSION,
            requested_by=WORKER,
            run_id="k8s-blocked",
        ),
        approval_id=approval["approval_id"],
        executor=executor,
        store=store,
    )
    allowed = execute_kubernetes_capability(
        operation=KubernetesOperation(
            cluster="local-fixture",
            namespace="demo",
            verb="patch",
            resource="deployments",
            name="web",
            patch={"replicas": 3},
            mission_id=MISSION,
            requested_by=WORKER,
            run_id="k8s-allowed",
        ),
        approval_id=approval["approval_id"],
        executor=executor,
        store=store,
    )
    receipt = allowed.receipt or {}
    verified = receipt.get("acceptance_state") == ACCEPTANCE_PASS and bool(
        receipt.get("verification", {}).get("verified")
    )
    receipt_path = None
    if receipt.get("receipt_id"):
        receipt_path = str(store.receipts_dir / f"{receipt['receipt_id']}.json")
    evidence = _write_json(
        Path(home) / "demo" / "kubernetes-evidence.json",
        {
            "scenario": "kubernetes",
            "mode": MODE_K8S_FIXTURE,
            "proposal_id": proposal["proposal_id"],
            "approval_id": approval["approval_id"],
            "blocked_code": blocked.code,
            "receipt": receipt,
        },
    )
    return ScenarioResult(
        scenario="kubernetes",
        title="Kubernetes / Infrastructure",
        mode=MODE_K8S_FIXTURE,
        ok=(not blocked.ok) and bool(allowed.ok) and verified,
        steps=[
            Step("REQUEST", True, proposal["proposal_id"]),
            Step("APPROVAL", True, approval["approval_id"]),
            Step("BINDING", True, (allowed.binding or {}).get("binding_id") or ""),
            Step("ALLOWED ACTION", bool(allowed.ok), "patch demo/web", invoked_transport=True),
            Step("BLOCKED ACTION", not blocked.ok, blocked.code, invoked_transport=False),
            Step("VERIFICATION", verified, receipt.get("acceptance_state") or ""),
        ],
        receipt=receipt,
        receipt_path=receipt_path,
        evidence_path=str(evidence),
        inspect_command="amof demo kubernetes --show-receipt",
        non_claims=["Fixture mode is not a live cluster. Live mode is opt-in and disposable only."],
    )


def _init_demo_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "demo@amof.local"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "AMOF Demo"], cwd=path, check=True, capture_output=True)
    (path / "docs").mkdir(parents=True, exist_ok=True)
    (path / "docs" / "note.md").write_text("draft\n", encoding="utf-8")
    (path / "src").mkdir(parents=True, exist_ok=True)
    (path / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    return git_rev_parse_head(path)


def run_git(home: str) -> ScenarioResult:
    os.environ["AMOF_HOME"] = home
    repo = Path(home) / "demo-repo"
    base_sha = _init_demo_repo(repo)
    proposals = Path(home) / "share" / "write-scopes" / "proposals"
    approvals = Path(home) / "share" / "write-scopes" / "approvals"
    bindings = Path(home) / "share" / "write-scopes" / "bindings"
    revocations = Path(home) / "share" / "write-scopes" / "revocations"
    receipts = Path(home) / "share" / "write-scopes" / "receipts"
    events = Path(home) / "share" / "write-scopes" / "events"
    body = {
        "target_id": f"local:amof-demo-git:{base_sha}",
        "base_sha": base_sha,
        "allowed_roots": ["docs/note.md"],
        "denied_roots": ["src/"],
        "reason": "update the demo note only",
        "expected_checks": ["git diff --check"],
        "docs_only": True,
        "source_mutation": False,
    }
    proposal = save_proposal(build_proposal_record(run_id="git-propose", body=body), base_dir=proposals)
    approval = approve_write_scope(
        proposal["proposal_id"],
        ttl="30m",
        approved_by=OPERATOR,
        proposals_dir=proposals,
        approvals_dir=approvals,
        events_dir=events,
    )
    gate = bind_write_scope(
        approval["approval_id"],
        run_id="git-exec",
        workspace_root=repo,
        requested_capabilities=["bounded_write"],
        approvals_dir=approvals,
        events_dir=events,
        bindings_dir=bindings,
    )
    scope = load_scope_roots_for_binding(gate.binding, approvals_dir=approvals, events_dir=events)
    blocked_path = "src/app.py"
    original = (repo / blocked_path).read_text(encoding="utf-8")
    denial = guardrail_write_allowed(
        blocked_path,
        writable_roots=scope.allowed_roots,
        denied_roots=scope.denied_roots,
        workspace_root=repo,
    )
    blocked_ok = denial is not None and (repo / blocked_path).read_text(encoding="utf-8") == original
    (repo / "docs" / "note.md").write_text("updated by bounded grant\n", encoding="utf-8")
    outcome = enforce_write_scope_mutations(
        gate.binding["binding_id"],
        changed_paths=["docs/note.md"],
        workspace_root=repo,
        approvals_dir=approvals,
        bindings_dir=bindings,
        revocations_dir=revocations,
        events_dir=events,
        receipts_dir=receipts,
    )
    receipt = outcome.receipt
    verified = receipt.get("compliance") == COMPLIANCE_WITHIN_SCOPE and "docs/note.md" in receipt.get(
        "in_scope_paths", []
    )
    receipt_path = str(receipts / f"{receipt['receipt_id']}.json") if receipt.get("receipt_id") else None
    evidence = _write_json(
        Path(home) / "demo" / "git-evidence.json",
        {
            "scenario": "git",
            "mode": MODE_REAL,
            "proposal_id": proposal["proposal_id"],
            "approval_id": approval["approval_id"],
            "blocked_guardrail": denial,
            "receipt": receipt,
        },
    )
    return ScenarioResult(
        scenario="git",
        title="Software / Git",
        mode=MODE_REAL,
        ok=blocked_ok and verified and not outcome.run_failed,
        steps=[
            Step("REQUEST", True, proposal["proposal_id"]),
            Step("APPROVAL", True, approval["approval_id"]),
            Step("BINDING", True, gate.binding["binding_id"]),
            Step("ALLOWED ACTION", verified, "docs/note.md", invoked_transport=True),
            Step("BLOCKED ACTION", blocked_ok, denial or "missing-guardrail", invoked_transport=False),
            Step("VERIFICATION", verified, receipt.get("compliance") or ""),
        ],
        receipt=receipt,
        receipt_path=receipt_path,
        evidence_path=str(evidence),
        inspect_command="amof demo git --show-receipt",
        non_claims=["This uses a disposable local Git repo, not a remote GitHub integration."],
    )


SCENARIOS: dict[str, tuple[str, Callable[..., ScenarioResult]]] = {
    "migration": ("Migration", run_migration),
    "kubernetes": ("Kubernetes / Infrastructure", run_kubernetes),
    "security": ("Security / IAM", run_security),
    "insurance": ("Insurance", run_insurance),
    "banking": ("Banking", run_banking),
    "healthcare": ("Healthcare", run_healthcare),
    "git": ("Software / Git", run_git),
}

MENU_ORDER = (
    "migration",
    "kubernetes",
    "security",
    "insurance",
    "banking",
    "healthcare",
    "git",
)


def live_cluster_tools_available() -> bool:
    return shutil.which("k3d") is not None and shutil.which("kubectl") is not None


def run_scenario(name: str, *, home: str | None = None, live: bool = False) -> ScenarioResult:
    key = str(name or "").strip().lower()
    if key not in SCENARIOS:
        raise RuntimeError(f"unknown scenario {name!r}")
    resolved_home = _store_home(home)
    runner = SCENARIOS[key][1]
    if key == "kubernetes":
        return runner(resolved_home, live=live)
    return runner(resolved_home)


def format_report(result: ScenarioResult) -> str:
    lines = [
        "AMOF RUNTIME AUTHORITY PROOF",
        "",
        f"Scenario: {result.title}",
        f"Mode: {result.mode}",
        "",
    ]
    marks = {
        "REQUEST": "Authority requested",
        "APPROVAL": "Bounded grant approved",
        "BINDING": "Grant bound to one execution",
        "ALLOWED ACTION": "Allowed operation executed",
        "BLOCKED ACTION": "Out-of-scope operation blocked before transport",
        "VERIFICATION": "Result verified",
    }
    order = ["REQUEST", "APPROVAL", "BINDING", "ALLOWED ACTION", "BLOCKED ACTION", "VERIFICATION"]

    def _rank(step: Step) -> tuple[int, str]:
        key = "BLOCKED ACTION" if step.name.startswith("BLOCKED ACTION") else step.name
        return (order.index(key) if key in order else 99, step.name)

    for step in sorted(result.steps, key=_rank):
        if step.name.startswith("BLOCKED ACTION"):
            label = marks["BLOCKED ACTION"]
            mark = "✕" if step.ok else "!"
        else:
            label = marks.get(step.name, step.name)
            mark = "✓" if step.ok else "!"
        extra = f" ({step.detail})" if step.detail else ""
        lines.append(f"{mark} {label}{extra}")
    receipt = result.receipt or {}
    acceptance = receipt.get("acceptance_state")
    if not acceptance and receipt.get("compliance") == COMPLIANCE_WITHIN_SCOPE:
        acceptance = "PASS"
    acceptance = acceptance or "FAIL"
    lines.extend(
        [
            "",
            f"Acceptance: {acceptance if result.ok else 'FAIL'}",
            f"Receipt: {result.receipt_path or '<none>'}",
            f"Evidence: {result.evidence_path or '<none>'}",
            "",
            "The worker proposed the action.",
            "AMOF owned the authority.",
            "",
        ]
    )
    if result.receipt_path:
        lines.extend(["Inspect receipt:", f"  cat {result.receipt_path}", ""])
    lines.extend(["Try another scenario:", "  amof demo", ""])
    for claim in result.non_claims:
        lines.append(f"Non-claim: {claim}")
    return "\n".join(lines).rstrip() + "\n"


def receipt_required_fields(receipt: dict[str, Any]) -> list[str]:
    kind = str(receipt.get("kind") or "")
    if kind == "mutation_receipt":
        return [
            "kind",
            "receipt_id",
            "approval_id",
            "binding_id",
            "compliance",
            "in_scope_paths",
            "out_of_scope_paths",
        ]
    return [
        "kind",
        "receipt_id",
        "approval_id",
        "binding_id",
        "capability",
        "within_scope",
        "verification",
        "acceptance_state",
        "secrets_omitted",
    ]


def validate_demo_receipt(receipt: dict[str, Any]) -> None:
    missing = [field for field in receipt_required_fields(receipt) if field not in receipt]
    _require(not missing, f"receipt missing fields: {missing}")
    if receipt.get("kind") == "mutation_receipt":
        _require(receipt.get("compliance") == COMPLIANCE_WITHIN_SCOPE, "git receipt is not within_scope")
        return
    verification = receipt.get("verification") or {}
    _require(receipt.get("acceptance_state") == ACCEPTANCE_PASS, "PASS receipt required")
    _require(bool(verification.get("verified")), "PASS requires verification")
    _require(receipt.get("secrets_omitted") is True, "receipt must omit secrets")

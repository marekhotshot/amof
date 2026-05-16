"""Governed workstation bootstrap evidence emitters."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from ..app_paths import ensure_parent_dir, evidence_dir
from .doctor import topology_report


RESULT_KIND = "amof_governed_workstation_bootstrap_contract"
CONTRACT_VERSION = "2026-05-15"
DEFAULT_FILENAME = "governed-workstation-bootstrap-contract.json"
SCHEMA_FILENAME = "governed-workstation-bootstrap-contract.schema.json"
DOCTOR_FILENAME = "doctor.json"
SOURCE_CHECKOUT_RECEIPT_FILENAME = "bootstrap-source-checkout-receipt.json"
TOOLCHAIN_RECEIPT_FILENAME = "bootstrap-toolchain-receipt.json"
PROVIDER_CONFIGURATION_RECEIPT_FILENAME = "bootstrap-provider-configuration-receipt.json"
FAILURE_RECEIPT_FILENAME = "bootstrap-failure-receipt.json"
SUMMARY_FILENAME = "up10-bootstrap-summary.json"
SHA256_MANIFEST_FILENAME = "bootstrap-sha256-manifest.json"
SOURCE_CHECKOUT_RECEIPT_SCHEMA_FILENAME = "bootstrap-source-checkout-receipt.schema.json"
TOOLCHAIN_RECEIPT_SCHEMA_FILENAME = "bootstrap-toolchain-receipt.schema.json"
PROVIDER_CONFIGURATION_RECEIPT_SCHEMA_FILENAME = "bootstrap-provider-configuration-receipt.schema.json"
FAILURE_RECEIPT_SCHEMA_FILENAME = "bootstrap-failure-receipt.schema.json"
SUMMARY_SCHEMA_FILENAME = "up10-bootstrap-summary.schema.json"
SHA256_MANIFEST_SCHEMA_FILENAME = "bootstrap-sha256-manifest.schema.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _bundle_label() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _git_remote_url(path: str | None) -> str | None:
    normalized = str(path or "").strip()
    if not normalized:
        return None
    repo_path = Path(normalized)
    if not repo_path.exists():
        return None
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=repo_path,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        return None
    remote = (result.stdout or "").strip()
    return remote or None


def _default_output_path(context_name: str) -> Path:
    normalized = str(context_name or "local").strip() or "local"
    return evidence_dir() / "bootstrap" / normalized / DEFAULT_FILENAME


def _default_bundle_dir(context_name: str) -> Path:
    normalized = str(context_name or "local").strip() or "local"
    return evidence_dir() / "bootstrap" / normalized / _bundle_label()


def _bootstrap_status(doctor_verdict: str) -> str:
    normalized = str(doctor_verdict or "").strip().upper()
    if normalized == "PASS":
        return "PASS"
    if normalized == "WARN":
        return "WARN"
    return "BLOCKED"


def _gate_status(*, passed: bool, warn_on_false: bool = False) -> str:
    if passed:
        return "PASS"
    return "WARN" if warn_on_false else "BLOCKED"


def _tool_probe_status(probe: dict[str, Any]) -> str:
    if bool(probe.get("available", False)) and not probe.get("error"):
        return "PASS"
    if bool(probe.get("required", False)):
        return "BLOCKED"
    return "WARN"


def _schema_path(report: dict[str, Any], filename: str) -> Path:
    return Path(str(report.get("canonical_amof_code_path") or Path.cwd())) / "contracts" / filename


def _source_repo_entries(report: dict[str, Any]) -> list[dict[str, Any]]:
    layout_mode = str(report.get("layout_mode") or "")
    role_map = {
        "root": "wrapper_config_audit_shell" if layout_mode == "split_workspace" else "canonical_active",
        "canonical_amof": "canonical_active",
        "canonical_ui": "canonical_active",
        "gmd_app": "active_supporting",
    }
    required_map = {
        "root": True,
        "canonical_amof": True,
        "canonical_ui": False,
        "gmd_app": False,
    }
    entries: list[dict[str, Any]] = []
    for name in ("root", "canonical_amof", "canonical_ui", "gmd_app"):
        surface = report.get("surfaces", {}).get(name, {})
        path = str(surface.get("path") or "")
        entries.append(
            {
                "name": name,
                "role": role_map[name],
                "required": required_map[name],
                "path": path,
                "exists": bool(surface.get("exists", False)),
                "git_remote_url": _git_remote_url(path),
                "current_branch": surface.get("branch"),
                "current_head": surface.get("head"),
                "dirty_count": int(surface.get("dirty_count", 0) or 0),
            }
        )
    return entries


def _runtime_roots(report: dict[str, Any]) -> dict[str, Any]:
    purposes = {
        "config_root": "AMOF config, contexts, and registries",
        "data_root": "AMOF durable app-data payloads",
        "cache_root": "AMOF cache and temporary downloads",
        "state_root": "AMOF logs, queue state, and locks",
        "evidence_dir": "Bootstrap and run evidence outputs",
        "runs_dir": "Run-level durable data",
        "workspaces_dir": "Per-run isolated workspace base directory",
        "materialized_runs_dir": "Exact-SHA materialized workspace roots",
        "receipts_dir": "Receipts and supporting bootstrap artifacts",
        "logs_dir": "Operational logs",
        "locks_dir": "Process coordination locks",
        "queue_dir": "Queued work state",
        "tmp_dir": "Temporary scratch directory",
        "provider_profiles_dir": "Provider profile references",
    }
    payload: dict[str, Any] = {}
    for name in (
        "config_root",
        "data_root",
        "cache_root",
        "state_root",
        "evidence_dir",
        "runs_dir",
        "workspaces_dir",
        "materialized_runs_dir",
        "receipts_dir",
        "logs_dir",
        "locks_dir",
        "queue_dir",
        "tmp_dir",
        "provider_profiles_dir",
    ):
        status = report.get("app_data", {}).get("roots", {}).get(name, {})
        payload[name] = {
            "path": status.get("path"),
            "purpose": purposes[name],
            "exists": bool(status.get("exists", False)),
            "writable": bool(status.get("writable", False)),
            "outside_source_workspaces": not bool(status.get("inside_source_workspace", False)),
        }
    return payload


def _doctor_gates(report: dict[str, Any]) -> list[dict[str, Any]]:
    surfaces = report.get("surfaces", {})
    required_tools = report.get("toolchain", {})
    contracts = report.get("contracts", {})
    context_summary = report.get("contexts", {}).get("current", {})
    root_entries = report.get("app_data", {}).get("roots", {})
    runtime_roots_ok = all(
        bool(item.get("exists", False))
        and bool(item.get("writable", False))
        and not bool(item.get("inside_source_workspace", False))
        for item in root_entries.values()
    )
    required_contracts_ok = all(
        bool(item.get("exists", False)) or bool(item.get("available_via_runtime", False))
        for item in contracts.values()
    )
    contract_support_mode = str(report.get("contract_support_mode") or "source_tree")
    required_toolchain_ok = all(
        bool(item.get("available", False)) and not bool(item.get("error"))
        for item in required_tools.values()
        if bool(item.get("required", False))
    )
    optional_tools_ok = all(
        bool(item.get("available", False)) and not bool(item.get("error"))
        for item in required_tools.values()
        if not bool(item.get("required", False))
    )
    dirty_summary = ", ".join(
        f"{name}={int(surface.get('dirty_count', 0) or 0)}"
        for name, surface in surfaces.items()
        if int(surface.get("dirty_count", 0) or 0) > 0
    )
    provider_ref_count = int(context_summary.get("provider_profile_ref_count", 0) or 0)
    secret_finding_count = int(report.get("secret_exposure", {}).get("finding_count", 0) or 0)
    return [
        {
            "name": "canonical_import_resolution",
            "status": _gate_status(passed=bool(report.get("runtime_import_is_canonical", False))),
            "summary": (
                "Runtime import must resolve under the canonical AMOF runtime path."
                if contract_support_mode == "packaged_runtime"
                else "Runtime import must resolve under the canonical AMOF scripts path."
            ),
            "evidence": str(report.get("runtime_import_source") or ""),
        },
        {
            "name": "git_dirty_classification",
            "status": _gate_status(passed=not dirty_summary, warn_on_false=True),
            "summary": "Dirty state is preserved as evidence and must not be silently normalized.",
            "evidence": dirty_summary or "all tracked surfaces clean",
        },
        {
            "name": "runtime_roots_outside_source_workspaces",
            "status": _gate_status(passed=runtime_roots_ok),
            "summary": "All runtime roots must be writable and remain outside mutable source workspaces.",
            "evidence": str(report.get("app_data", {}).get("roots", {}).get("workspaces_dir", {}).get("path") or ""),
        },
        {
            "name": "required_contracts_available",
            "status": _gate_status(passed=required_contracts_ok),
            "summary": (
                "Bootstrap and Director contract definitions must be available from the packaged runtime or canonical repo."
                if contract_support_mode == "packaged_runtime"
                else "Bootstrap and Director contract schemas must be present in the canonical repo."
            ),
            "evidence": ", ".join(sorted(contracts.keys())),
        },
        {
            "name": "required_toolchain_present",
            "status": _gate_status(passed=required_toolchain_ok),
            "summary": "Required bootstrap toolchain commands must be installed and runnable.",
            "evidence": ", ".join(
                f"{name}={tool.get('version') or tool.get('error') or 'missing'}"
                for name, tool in required_tools.items()
                if bool(tool.get("required", False))
            ),
        },
        {
            "name": "optional_local_runtime_tools",
            "status": _gate_status(passed=optional_tools_ok, warn_on_false=True),
            "summary": "Optional local-runtime tools may remain WARN when local runtime proof is deferred.",
            "evidence": ", ".join(
                f"{name}={'available' if bool(tool.get('available', False)) and not tool.get('error') else 'missing'}"
                for name, tool in required_tools.items()
                if not bool(tool.get("required", False))
            ),
        },
        {
            "name": "provider_authority_by_reference",
            "status": _gate_status(passed=provider_ref_count > 0, warn_on_false=True),
            "summary": "Provider authority must be described by references and redacted metadata, not raw secrets.",
            "evidence": f"provider_profile_ref_count={provider_ref_count}; health={context_summary.get('provider_health_status')}",
        },
        {
            "name": "secret_exposure_sanity_check",
            "status": _gate_status(passed=secret_finding_count == 0),
            "summary": "Obvious secret-bearing files inside the canonical source tree block bootstrap readiness.",
            "evidence": f"finding_count={secret_finding_count}",
        },
    ]


def build_bootstrap_contract(
    report: dict[str, Any],
    *,
    output_path: Path,
    artifact_paths: dict[str, str | None] | None = None,
    bundle_directory: str | None = None,
) -> dict[str, Any]:
    current_context = report.get("contexts", {}).get("current", {})
    bootstrap_status = _bootstrap_status(str(report.get("verdict") or ""))
    runtime_roots = _runtime_roots(report)
    source_repos = _source_repo_entries(report)
    normalized_artifact_paths = {
        "contract_artifact_path": str(output_path),
        "doctor_artifact_path": None,
        "source_checkout_receipt_path": None,
        "toolchain_receipt_path": None,
        "provider_configuration_receipt_path": None,
        "failure_receipt_path": None,
        "summary_artifact_path": None,
        "sha256_manifest_path": None,
    }
    if artifact_paths:
        normalized_artifact_paths.update(artifact_paths)
    forbidden_roots = []
    for path in (
        report.get("workspace_root"),
        report.get("canonical_amof_code_path"),
        report.get("canonical_ui_path"),
        report.get("surfaces", {}).get("gmd_app", {}).get("path"),
    ):
        normalized = str(path or "").strip()
        if normalized and normalized not in forbidden_roots:
            forbidden_roots.append(normalized)
    required_contracts = sorted(report.get("contracts", {}).keys())
    all_runtime_roots_ok = all(
        bool(item.get("exists", False))
        and bool(item.get("writable", False))
        and bool(item.get("outside_source_workspaces", False))
        for item in runtime_roots.values()
    )
    payload = {
        "result_kind": RESULT_KIND,
        "contract_version": CONTRACT_VERSION,
        "bootstrap_status": bootstrap_status,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "target_workstation": {
            "os": sys.platform,
            "architecture": subprocess.run(
                ["uname", "-m"],
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            ).stdout.strip()
            or "unknown",
            "shell": str(os.environ.get("SHELL") or "unknown"),
            "layout_mode": report.get("layout_mode"),
            "required_local_tools": ["git", "python3"],
            "optional_local_tools": ["docker", "k3d", "kubectl", "helm"],
        },
        "source_repos": source_repos,
        "runtime_roots": runtime_roots,
        "forbidden_state_roots": forbidden_roots,
        "provider_authority": {
            "current_context": current_context.get("current_context"),
            "controlplane_mode": current_context.get("controlplane_mode"),
            "execution_backend": current_context.get("execution_backend"),
            "workspace_backend": current_context.get("workspace_backend"),
            "evidence_backend": current_context.get("evidence_backend"),
            "provider_profile_refs": current_context.get("provider_profile_refs", []),
            "provider_profile_ref_count": current_context.get("provider_profile_ref_count"),
            "provider_health_status": current_context.get("provider_health_status"),
            "kubeconfig_ref": current_context.get("kubeconfig_ref"),
            "kubeconfig_ref_exists": current_context.get("kubeconfig_ref_exists"),
            "redaction_rules": [
                "do not record raw API keys or bearer tokens",
                "do not record kubeconfig contents or private SSH keys",
                "record only provider profile refs, secret names, boolean presence, and health classifications",
            ],
        },
        "director_prerequisites": {
            "cli_import_is_canonical": bool(report.get("runtime_import_is_canonical", False)),
            "required_contracts_available": all(
                bool(item.get("exists", False)) or bool(item.get("available_via_runtime", False))
                for item in report.get("contracts", {}).values()
            ),
            "isolated_workspace_base_dir": runtime_roots["workspaces_dir"]["path"],
            "materialized_runs_dir": runtime_roots["materialized_runs_dir"]["path"],
            "artifact_directory": runtime_roots["evidence_dir"]["path"],
            "runtime_roots_outside_source_workspaces": all_runtime_roots_ok,
            "provider_resolution_status": current_context.get("provider_health_status"),
            "doctor_surface": "amof doctor --json",
            "bootstrap_contract_surface": "amof bootstrap contract --json",
        },
        "doctor_gates": _doctor_gates(report),
        "evidence_outputs": {
            "contract_artifact_path": str(output_path),
            "bootstrap_command": "amof bootstrap bundle --json",
            "bundle_directory": bundle_directory,
            "doctor_command": "amof doctor --json",
            "expected_artifacts": [
                "governed-workstation-bootstrap-contract.json",
                DOCTOR_FILENAME,
                SOURCE_CHECKOUT_RECEIPT_FILENAME,
                TOOLCHAIN_RECEIPT_FILENAME,
                PROVIDER_CONFIGURATION_RECEIPT_FILENAME,
                SUMMARY_FILENAME,
                SHA256_MANIFEST_FILENAME,
            ],
            "artifact_paths": normalized_artifact_paths,
            "sha256_manifest_path": normalized_artifact_paths["sha256_manifest_path"],
            "hash_policy": "sha256",
            "replay_notes": [
                "Git history remains the canonical evidence substrate for source changes.",
                "Bootstrap WARN or BLOCKED states must be preserved as evidence rather than normalized into PASS.",
            ],
        },
        "mutation_policy": {
            "allowed_setup_mutations": [
                "create or refresh AMOF app-data roots outside source workspaces",
                "write bootstrap evidence artifacts under the declared evidence root",
                "initialize the default local AMOF context when absent",
            ],
            "forbidden_runtime_mutations": [
                "deploy or mutate Kubernetes runtime state",
                "patch provider credentials into runtime surfaces",
                "perform cloud or production changes",
            ],
            "forbidden_source_mutations": [
                "write operational state into source checkouts",
                "rewrite unrelated repositories or branches",
                "hide dirty-state or secret-exposure findings",
            ],
        },
        "rollback_policy": {
            "cleanup_expectations": [
                "remove only evidence artifacts or temporary bootstrap outputs created by this slice",
                "leave app-data and source history intact unless the operator explicitly requests cleanup",
            ],
            "blocked_bootstrap_rules": [
                "stop when a required doctor gate is BLOCKED",
                "preserve the blocked contract artifact and matching doctor evidence",
            ],
            "quarantine_rules": [
                "quarantine and review any artifact that appears to contain secret material before reuse or publication",
            ],
        },
    }
    return payload


def build_source_checkout_receipt(report: dict[str, Any]) -> dict[str, Any]:
    source_repos = _source_repo_entries(report)
    dirty_repo_names = [entry["name"] for entry in source_repos if int(entry["dirty_count"]) > 0]
    return {
        "result_kind": "amof_bootstrap_source_checkout_receipt",
        "contract_version": CONTRACT_VERSION,
        "checked_at": _now_iso(),
        "layout_mode": report.get("layout_mode"),
        "canonical_amof_code_path": report.get("canonical_amof_code_path"),
        "source_repos": source_repos,
        "dirty_repo_count": len(dirty_repo_names),
        "dirty_repo_names": dirty_repo_names,
        "forbidden_state_roots": [
            str(path)
            for path in (
                report.get("workspace_root"),
                report.get("canonical_amof_code_path"),
                report.get("canonical_ui_path"),
                report.get("surfaces", {}).get("gmd_app", {}).get("path"),
            )
            if str(path or "").strip()
        ],
        "status": "WARN" if dirty_repo_names else "PASS",
    }


def build_toolchain_receipt(report: dict[str, Any]) -> dict[str, Any]:
    required_tools: list[dict[str, Any]] = []
    optional_tools: list[dict[str, Any]] = []
    for name, probe in report.get("toolchain", {}).items():
        entry = {
            "name": name,
            "required": bool(probe.get("required", False)),
            "available": bool(probe.get("available", False)),
            "resolved_path": probe.get("resolved_path"),
            "version": probe.get("version"),
            "error": probe.get("error"),
            "status": _tool_probe_status(probe),
        }
        if entry["required"]:
            required_tools.append(entry)
        else:
            optional_tools.append(entry)
    required_ok = all(entry["status"] == "PASS" for entry in required_tools)
    optional_ok = all(entry["status"] == "PASS" for entry in optional_tools)
    status = "PASS" if required_ok and optional_ok else ("WARN" if required_ok else "BLOCKED")
    return {
        "result_kind": "amof_bootstrap_toolchain_receipt",
        "contract_version": CONTRACT_VERSION,
        "checked_at": _now_iso(),
        "status": status,
        "required_tools": required_tools,
        "optional_tools": optional_tools,
    }


def build_provider_configuration_receipt(report: dict[str, Any]) -> dict[str, Any]:
    current_context = report.get("contexts", {}).get("current", {})
    profile_ref_count = int(current_context.get("provider_profile_ref_count", 0) or 0)
    status = "PASS" if profile_ref_count > 0 else "WARN"
    return {
        "result_kind": "amof_bootstrap_provider_configuration_receipt",
        "contract_version": CONTRACT_VERSION,
        "checked_at": _now_iso(),
        "status": status,
        "current_context": current_context.get("current_context"),
        "controlplane_mode": current_context.get("controlplane_mode"),
        "execution_backend": current_context.get("execution_backend"),
        "workspace_backend": current_context.get("workspace_backend"),
        "evidence_backend": current_context.get("evidence_backend"),
        "provider_profile_refs": current_context.get("provider_profile_refs", []),
        "provider_profile_ref_count": profile_ref_count,
        "provider_health_status": current_context.get("provider_health_status"),
        "kubeconfig_ref": current_context.get("kubeconfig_ref"),
        "kubeconfig_ref_exists": current_context.get("kubeconfig_ref_exists"),
        "redaction_rules": [
            "do not record raw API keys or bearer tokens",
            "do not record kubeconfig contents or private SSH keys",
            "record only provider profile refs, secret names, boolean presence, and health classifications",
        ],
    }


def build_failure_receipt(report: dict[str, Any]) -> dict[str, Any]:
    doctor_gates = _doctor_gates(report)
    blocked_gates = [gate for gate in doctor_gates if gate["status"] == "BLOCKED"]
    return {
        "result_kind": "amof_bootstrap_failure_receipt",
        "contract_version": CONTRACT_VERSION,
        "generated_at": _now_iso(),
        "bootstrap_status": "BLOCKED",
        "blocked_gate_names": [gate["name"] for gate in blocked_gates],
        "blocked_gates": blocked_gates,
        "doctor_warnings": list(report.get("warnings", [])),
        "doctor_failures": list(report.get("failures", [])),
    }


def build_summary(
    report: dict[str, Any],
    *,
    bootstrap_status: str,
    bundle_directory: Path,
    artifact_paths: dict[str, str | None],
) -> dict[str, Any]:
    doctor_gates = _doctor_gates(report)
    blocked_gate_count = len([gate for gate in doctor_gates if gate["status"] == "BLOCKED"])
    warn_gate_count = len([gate for gate in doctor_gates if gate["status"] == "WARN"])
    key_reasons: list[str] = []
    key_reasons.extend(list(report.get("failures", [])))
    if bootstrap_status != "BLOCKED":
        key_reasons.extend(list(report.get("warnings", [])))
    return {
        "result_kind": "amof_up10_bootstrap_summary",
        "contract_version": CONTRACT_VERSION,
        "generated_at": _now_iso(),
        "bootstrap_status": bootstrap_status,
        "ready_for_up11": bootstrap_status == "PASS",
        "current_context": report.get("contexts", {}).get("current", {}).get("current_context"),
        "bundle_directory": str(bundle_directory),
        "artifact_paths": artifact_paths,
        "doctor_warning_count": len(list(report.get("warnings", []))),
        "doctor_failure_count": len(list(report.get("failures", []))),
        "warn_gate_count": warn_gate_count,
        "blocked_gate_count": blocked_gate_count,
        "key_reasons": key_reasons,
    }


def build_sha256_manifest(
    *,
    bundle_directory: Path,
    artifact_paths: dict[str, str | None],
    excluded_labels: list[str] | None = None,
) -> dict[str, Any]:
    excluded = set(excluded_labels or [])
    artifacts: list[dict[str, Any]] = []
    for label, path in sorted(artifact_paths.items()):
        normalized = str(path or "").strip()
        if not normalized or label in excluded:
            continue
        file_path = Path(normalized)
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        artifacts.append(
            {
                "label": label,
                "path": str(file_path),
                "sha256": digest,
                "bytes": file_path.stat().st_size,
            }
        )
    return {
        "result_kind": "amof_bootstrap_sha256_manifest",
        "contract_version": CONTRACT_VERSION,
        "generated_at": _now_iso(),
        "hash_algorithm": "sha256",
        "bundle_directory": str(bundle_directory),
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
        "excluded_artifacts": sorted(excluded),
    }


def _validate_schema_if_available(payload: dict[str, Any], schema_path: Path) -> None:
    if importlib.util.find_spec("jsonschema") is None or not schema_path.exists():
        return
    import jsonschema

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.validate(instance=payload, schema=schema)


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    target = ensure_parent_dir(path)
    target.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return target


def cmd_bootstrap(args: Any) -> int:
    bootstrap_cmd = str(getattr(args, "bootstrap_cmd", "") or "").strip()
    if bootstrap_cmd not in {"contract", "bundle"}:
        sys.stderr.write("Usage: amof bootstrap <contract|bundle> [options]\n")
        return 1
    report = topology_report()
    context_name = str(report.get("contexts", {}).get("current", {}).get("current_context") or "local")
    if bootstrap_cmd == "contract":
        output_arg = str(getattr(args, "output", "") or "").strip()
        output_path = Path(output_arg).expanduser().resolve(strict=False) if output_arg else _default_output_path(context_name)
        payload = build_bootstrap_contract(report, output_path=output_path)
        _validate_schema_if_available(payload, _schema_path(report, SCHEMA_FILENAME))
        written = _write_json(output_path, payload)
        if bool(getattr(args, "json", False)):
            print(json.dumps(payload, indent=2, sort_keys=False))
        else:
            print(str(written))
        return 2 if payload["bootstrap_status"] == "BLOCKED" else 0

    output_dir_arg = str(getattr(args, "output_dir", "") or "").strip()
    bundle_directory = (
        Path(output_dir_arg).expanduser().resolve(strict=False)
        if output_dir_arg
        else _default_bundle_dir(context_name)
    )
    contract_path = bundle_directory / DEFAULT_FILENAME
    doctor_path = bundle_directory / DOCTOR_FILENAME
    source_checkout_path = bundle_directory / SOURCE_CHECKOUT_RECEIPT_FILENAME
    toolchain_path = bundle_directory / TOOLCHAIN_RECEIPT_FILENAME
    provider_configuration_path = bundle_directory / PROVIDER_CONFIGURATION_RECEIPT_FILENAME
    summary_path = bundle_directory / SUMMARY_FILENAME
    sha256_manifest_path = bundle_directory / SHA256_MANIFEST_FILENAME
    bootstrap_status = _bootstrap_status(str(report.get("verdict") or ""))
    failure_path = bundle_directory / FAILURE_RECEIPT_FILENAME if bootstrap_status == "BLOCKED" else None

    artifact_paths: dict[str, str | None] = {
        "contract_artifact_path": str(contract_path),
        "doctor_artifact_path": str(doctor_path),
        "source_checkout_receipt_path": str(source_checkout_path),
        "toolchain_receipt_path": str(toolchain_path),
        "provider_configuration_receipt_path": str(provider_configuration_path),
        "failure_receipt_path": str(failure_path) if failure_path is not None else None,
        "summary_artifact_path": str(summary_path),
        "sha256_manifest_path": str(sha256_manifest_path),
    }

    doctor_payload = report
    source_checkout_payload = build_source_checkout_receipt(report)
    toolchain_payload = build_toolchain_receipt(report)
    provider_configuration_payload = build_provider_configuration_receipt(report)
    failure_payload = build_failure_receipt(report) if failure_path is not None else None
    contract_payload = build_bootstrap_contract(
        report,
        output_path=contract_path,
        artifact_paths=artifact_paths,
        bundle_directory=str(bundle_directory),
    )
    summary_payload = build_summary(
        report,
        bootstrap_status=bootstrap_status,
        bundle_directory=bundle_directory,
        artifact_paths=artifact_paths,
    )

    _validate_schema_if_available(contract_payload, _schema_path(report, SCHEMA_FILENAME))
    _validate_schema_if_available(
        source_checkout_payload,
        _schema_path(report, SOURCE_CHECKOUT_RECEIPT_SCHEMA_FILENAME),
    )
    _validate_schema_if_available(
        toolchain_payload,
        _schema_path(report, TOOLCHAIN_RECEIPT_SCHEMA_FILENAME),
    )
    _validate_schema_if_available(
        provider_configuration_payload,
        _schema_path(report, PROVIDER_CONFIGURATION_RECEIPT_SCHEMA_FILENAME),
    )
    if failure_payload is not None:
        _validate_schema_if_available(
            failure_payload,
            _schema_path(report, FAILURE_RECEIPT_SCHEMA_FILENAME),
        )
    _validate_schema_if_available(summary_payload, _schema_path(report, SUMMARY_SCHEMA_FILENAME))

    _write_json(doctor_path, doctor_payload)
    _write_json(source_checkout_path, source_checkout_payload)
    _write_json(toolchain_path, toolchain_payload)
    _write_json(provider_configuration_path, provider_configuration_payload)
    if failure_payload is not None and failure_path is not None:
        _write_json(failure_path, failure_payload)
    _write_json(contract_path, contract_payload)
    _write_json(summary_path, summary_payload)
    sha256_manifest_payload = build_sha256_manifest(
        bundle_directory=bundle_directory,
        artifact_paths=artifact_paths,
        excluded_labels=["sha256_manifest_path"],
    )
    _validate_schema_if_available(
        sha256_manifest_payload,
        _schema_path(report, SHA256_MANIFEST_SCHEMA_FILENAME),
    )
    _write_json(sha256_manifest_path, sha256_manifest_payload)

    if bool(getattr(args, "json", False)):
        print(json.dumps(summary_payload, indent=2, sort_keys=False))
    else:
        print(str(summary_path))
    return 2 if bootstrap_status == "BLOCKED" else 0


__all__ = [
    "RESULT_KIND",
    "CONTRACT_VERSION",
    "build_bootstrap_contract",
    "build_source_checkout_receipt",
    "build_toolchain_receipt",
    "build_provider_configuration_receipt",
    "build_failure_receipt",
    "build_summary",
    "build_sha256_manifest",
    "cmd_bootstrap",
]

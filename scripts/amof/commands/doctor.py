"""Bootstrap doctor for AMOF topology, app-data, and install guardrails."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional

from ..app_config import ensure_default_context_config, get_context, get_current_context_name, load_contexts
from ..app_paths import (
    config_file,
    contexts_file,
    ensure_app_roots,
    evidence_dir,
    get_app_paths,
    locks_dir,
    logs_dir,
    materialized_runs_dir,
    provider_profiles_dir,
    queue_dir,
    receipts_dir,
    runs_dir,
    tmp_dir,
    workspace_state_file,
    workspaces_dir,
    workspaces_registry_file,
)


RESULT_KIND = "amof_doctor_result"
CONTRACT_VERSION = "2026-05-15"
REQUIRED_CONTRACT_FILES = (
    "contracts/director-intake-client-contract.md",
    "contracts/director-intake-execution-contract.schema.json",
    "contracts/director-plan-result.schema.json",
    "contracts/workspace-receipt.schema.json",
    "contracts/execution-handoff-result.schema.json",
    "contracts/governed-workstation-bootstrap-contract.schema.json",
    "contracts/bootstrap-source-checkout-receipt.schema.json",
    "contracts/bootstrap-toolchain-receipt.schema.json",
    "contracts/bootstrap-provider-configuration-receipt.schema.json",
    "contracts/bootstrap-failure-receipt.schema.json",
    "contracts/up10-bootstrap-summary.schema.json",
    "contracts/bootstrap-sha256-manifest.schema.json",
)
REQUIRED_TOOLCHAIN = {
    "git": ["git", "--version"],
    "python": [sys.executable, "--version"],
}
OPTIONAL_TOOLCHAIN = {
    "docker": ["docker", "--version"],
    "k3d": ["k3d", "version"],
    "kubectl": ["kubectl", "version", "--client", "--output=json"],
    "helm": ["helm", "version", "--short"],
}
SECRET_EXPOSURE_GLOBS = (
    ".env",
    ".env.*",
    "*.kubeconfig",
    "*.pem",
    "id_rsa",
    "id_ed25519",
)


def _run(args: List[str], cwd: Optional[Path] = None) -> str:
    try:
        result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=10)
    except Exception as exc:
        return f"<error: {exc}>"
    if result.returncode != 0:
        return (result.stderr or result.stdout or "").strip() or f"<exit {result.returncode}>"
    return result.stdout.strip()


def _command_probe(label: str, argv: list[str], *, required: bool) -> dict[str, Any]:
    resolved = shutil.which(argv[0]) if argv else None
    result = {
        "name": label,
        "required": required,
        "available": resolved is not None,
        "command": argv,
        "resolved_path": resolved,
        "version": None,
        "error": None,
    }
    if resolved is None:
        result["error"] = "command not found"
        return result
    output = _run(argv)
    if output.startswith("<error:") or output.startswith("<exit "):
        result["error"] = output
        return result
    result["version"] = output
    return result


def _git_summary(path: Path) -> Dict[str, Any]:
    exists = path.exists()
    out: Dict[str, Any] = {"path": str(path), "exists": exists}
    if not exists:
        return out
    top = _run(["git", "rev-parse", "--show-toplevel"], cwd=path)
    if top.startswith("<"):
        out.update({"git": False, "error": top})
        return out
    status_lines = [line for line in _run(["git", "status", "--short"], cwd=path).splitlines() if line.strip()]
    out.update(
        {
            "git": True,
            "top": top,
            "branch": _run(["git", "branch", "--show-current"], cwd=path) or "detached",
            "head": _run(["git", "rev-parse", "--short", "HEAD"], cwd=path),
            "dirty_count": len(status_lines),
            "dirty_sample": status_lines[:20],
        }
    )
    return out


def _git_toplevel(path: Path) -> Path | None:
    top = _run(["git", "rev-parse", "--show-toplevel"], cwd=path)
    if top.startswith("<") or not top:
        return None
    return Path(top).resolve()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def _path_status(path: Path) -> dict[str, Any]:
    normalized = path.resolve(strict=False)
    return {
        "path": str(normalized),
        "exists": normalized.exists(),
        "is_dir": normalized.is_dir(),
        "writable": os.access(normalized, os.W_OK) if normalized.exists() else False,
    }


def _directory_status(path: Path) -> dict[str, Any]:
    normalized = path.resolve(strict=False)
    normalized.mkdir(parents=True, exist_ok=True)
    return _path_status(normalized)


def _context_provider_summary(context_name: str, context_payload: dict[str, Any]) -> dict[str, Any]:
    credentials = context_payload.get("credentials", {}) if isinstance(context_payload.get("credentials"), dict) else {}
    refs = credentials.get("provider_profile_refs")
    provider_profile_refs = refs if isinstance(refs, list) else []
    kubeconfig_ref = credentials.get("kubeconfig_ref")
    kubeconfig_ref_str = str(kubeconfig_ref).strip() if kubeconfig_ref is not None else None
    kubeconfig_exists = None
    if kubeconfig_ref_str:
        candidate = Path(kubeconfig_ref_str).expanduser()
        kubeconfig_exists = candidate.exists() if candidate.is_absolute() else None
    return {
        "current_context": context_name,
        "controlplane_mode": context_payload.get("controlplane", {}).get("mode"),
        "execution_backend": context_payload.get("execution", {}).get("backend"),
        "workspace_backend": context_payload.get("workspace", {}).get("backend"),
        "evidence_backend": context_payload.get("evidence", {}).get("backend"),
        "provider_profile_refs": [str(item) for item in provider_profile_refs],
        "provider_profile_ref_count": len(provider_profile_refs),
        "provider_health_status": "unknown" if provider_profile_refs else "unconfigured",
        "kubeconfig_ref": kubeconfig_ref_str,
        "kubeconfig_ref_exists": kubeconfig_exists,
    }


def _toolchain_report() -> dict[str, dict[str, Any]]:
    report: dict[str, dict[str, Any]] = {}
    for label, argv in REQUIRED_TOOLCHAIN.items():
        report[label] = _command_probe(label, argv, required=True)
    for label, argv in OPTIONAL_TOOLCHAIN.items():
        report[label] = _command_probe(label, argv, required=False)
    return report


def _contracts_report(canonical_amof: Path) -> dict[str, Any]:
    entries: dict[str, Any] = {}
    for relative_path in REQUIRED_CONTRACT_FILES:
        target = canonical_amof / relative_path
        entries[relative_path] = {
            "path": str(target),
            "exists": target.exists(),
        }
    return entries


def _secret_exposure_report(*, canonical_amof: Path) -> dict[str, Any]:
    search_roots = {
        "canonical_amof": canonical_amof,
    }
    findings: list[dict[str, str]] = []
    seen_paths: set[Path] = set()
    for scope, root in search_roots.items():
        if not root.exists():
            continue
        for pattern in SECRET_EXPOSURE_GLOBS:
            for match in root.glob(pattern):
                resolved = match.resolve(strict=False)
                if resolved in seen_paths or not match.is_file():
                    continue
                seen_paths.add(resolved)
                findings.append(
                    {
                        "scope": scope,
                        "path": str(resolved),
                        "pattern": pattern,
                    }
                )
    return {
        "findings": findings,
        "finding_count": len(findings),
    }


def _app_data_report(*, workspace: Path, canonical_amof: Path) -> dict[str, Any]:
    ensure_app_roots()
    ensure_default_context_config()
    roots = get_app_paths()
    runtime_dirs = {
        "config_root": _directory_status(roots.config_root),
        "data_root": _directory_status(roots.data_root),
        "cache_root": _directory_status(roots.cache_root),
        "state_root": _directory_status(roots.state_root),
        "evidence_dir": _directory_status(evidence_dir()),
        "runs_dir": _directory_status(runs_dir()),
        "workspaces_dir": _directory_status(workspaces_dir()),
        "materialized_runs_dir": _directory_status(materialized_runs_dir()),
        "receipts_dir": _directory_status(receipts_dir()),
        "logs_dir": _directory_status(logs_dir()),
        "locks_dir": _directory_status(locks_dir()),
        "queue_dir": _directory_status(queue_dir()),
        "tmp_dir": _directory_status(tmp_dir()),
        "provider_profiles_dir": _directory_status(provider_profiles_dir()),
    }
    forbidden_roots = [workspace.resolve(strict=False), canonical_amof.resolve(strict=False)]
    for entry in runtime_dirs.values():
        entry_path = Path(entry["path"])
        entry["inside_source_workspace"] = any(_is_relative_to(entry_path, parent) for parent in forbidden_roots)
    config_files = {
        "config_file": _path_status(config_file()),
        "contexts_file": _path_status(contexts_file()),
        "workspace_state_file": _path_status(workspace_state_file()),
        "workspaces_registry_file": _path_status(workspaces_registry_file()),
    }
    return {
        "roots": runtime_dirs,
        "files": config_files,
    }


def _is_split_workspace_root(path: Path) -> bool:
    return (path / "repos" / "amof" / "scripts" / "amof").is_dir()


def _is_standalone_repo_root(path: Path) -> bool:
    return (path / "scripts" / "amof").is_dir()


def _detect_layout(start_path: Path | None = None) -> tuple[str, Path]:
    here = (start_path or Path.cwd()).resolve()
    seen: set[Path] = set()
    standalone_root: Path | None = None

    for candidate in (here, *here.parents):
        git_toplevel = _git_toplevel(candidate)
        if git_toplevel is None or git_toplevel in seen:
            continue
        seen.add(git_toplevel)
        if _is_split_workspace_root(git_toplevel):
            return "split_workspace", git_toplevel
        if standalone_root is None and _is_standalone_repo_root(git_toplevel):
            standalone_root = git_toplevel

    if standalone_root is not None:
        return "standalone_repo", standalone_root

    script_repo = _git_toplevel(Path(__file__).resolve().parent)
    if script_repo is not None:
        if _is_split_workspace_root(script_repo):
            return "split_workspace", script_repo
        if _is_standalone_repo_root(script_repo):
            return "standalone_repo", script_repo
    return "standalone_repo", here


def _import_origin() -> str:
    spec = importlib.util.find_spec("amof")
    return str(spec.origin) if spec and spec.origin else "<unresolved>"


def _runtime_path_contains_root_before_canonical(
    workspace: Path,
    canonical_scripts: Path,
    *,
    path_entries: list[str] | None = None,
) -> bool:
    root_scripts = str(workspace / "scripts")
    canonical = str(canonical_scripts)
    source_paths = path_entries if path_entries is not None else sys.path
    paths = [str(Path(p).resolve()) for p in source_paths if p]
    try:
        return paths.index(str(Path(root_scripts).resolve())) < paths.index(str(Path(canonical).resolve()))
    except ValueError:
        return False


def topology_report(
    *,
    start_path: Path | None = None,
    import_origin: str | None = None,
    path_entries: list[str] | None = None,
) -> Dict[str, Any]:
    layout_mode, workspace = _detect_layout(start_path)
    if layout_mode == "split_workspace":
        canonical_amof = workspace / "repos" / "amof"
        canonical_ui = canonical_amof / "apps" / "amof-ui"
        gmd_app = workspace / "repos" / "gmd-app"
        root_workspace_role = "wrapper/config/audit surface only; not canonical AMOF implementation"
    else:
        canonical_amof = workspace
        canonical_ui = workspace / "apps" / "amof-ui"
        gmd_app = workspace.parent / "gmd-app"
        root_workspace_role = "standalone repo root is the canonical AMOF implementation"
    canonical_scripts = canonical_amof / "scripts"
    resolved_import_origin = import_origin or _import_origin()
    import_is_canonical = resolved_import_origin.startswith(str(canonical_scripts))
    root_before_canonical = (
        layout_mode == "split_workspace"
        and _runtime_path_contains_root_before_canonical(
            workspace,
            canonical_scripts,
            path_entries=path_entries,
        )
    )

    surfaces = {
        "root": _git_summary(workspace),
        "canonical_amof": _git_summary(canonical_amof),
        "canonical_ui": _git_summary(canonical_ui),
        "gmd_app": _git_summary(gmd_app),
    }
    app_data = _app_data_report(workspace=workspace, canonical_amof=canonical_amof)
    contracts = _contracts_report(canonical_amof)
    toolchain = _toolchain_report()
    contexts = load_contexts()
    current_context = get_current_context_name()
    current_context_payload = get_context(current_context)
    context_summary = _context_provider_summary(current_context, current_context_payload)
    secret_exposure = _secret_exposure_report(canonical_amof=canonical_amof)

    warnings: List[str] = []
    failures: List[str] = []

    if not import_is_canonical:
        failures.append(f"amof import resolves outside canonical {canonical_scripts}: {resolved_import_origin}")
    if root_before_canonical:
        failures.append("root scripts/ appears before repos/amof/scripts on sys.path")
    seen_warning_paths: set[str] = set()
    for label, surface in surfaces.items():
        dirty_count = int(surface.get("dirty_count", 0) or 0)
        surface_path = str(surface.get("path") or "")
        if dirty_count and surface_path not in seen_warning_paths:
            seen_warning_paths.add(surface_path)
            warnings.append(f"{label} dirty_count={dirty_count}")
    for label, root_status in app_data["roots"].items():
        if not root_status["exists"] or not root_status["is_dir"]:
            failures.append(f"{label} is missing: {root_status['path']}")
            continue
        if not root_status["writable"]:
            failures.append(f"{label} is not writable: {root_status['path']}")
        if root_status["inside_source_workspace"]:
            failures.append(f"{label} resolves inside a source workspace: {root_status['path']}")
    contexts_file_status = app_data["files"]["contexts_file"]
    config_file_status = app_data["files"]["config_file"]
    if not contexts_file_status["exists"]:
        failures.append(f"contexts file is missing: {contexts_file_status['path']}")
    if not config_file_status["exists"]:
        failures.append(f"global config file is missing: {config_file_status['path']}")
    for relative_path, contract_status in contracts.items():
        if not contract_status["exists"]:
            failures.append(f"required contract missing: {relative_path}")
    for label, probe in toolchain.items():
        if probe["required"] and not probe["available"]:
            failures.append(f"required tool missing: {label}")
        elif probe["required"] and probe["error"]:
            failures.append(f"required tool unusable: {label}: {probe['error']}")
        elif not probe["required"] and not probe["available"]:
            warnings.append(f"optional tool missing: {label}")
        elif not probe["required"] and probe["error"]:
            warnings.append(f"optional tool probe failed: {label}: {probe['error']}")
    if context_summary["provider_profile_ref_count"] == 0:
        warnings.append("current context has no provider profile references configured")
    if context_summary["kubeconfig_ref"] and context_summary["kubeconfig_ref_exists"] is False:
        warnings.append(f"kubeconfig ref path does not exist: {context_summary['kubeconfig_ref']}")
    if secret_exposure["finding_count"]:
        failures.append(f"secret-exposure check found {secret_exposure['finding_count']} obvious source file(s)")

    verdict = "FAIL" if failures else ("WARN" if warnings else "PASS")
    return {
        "result_kind": RESULT_KIND,
        "contract_version": CONTRACT_VERSION,
        "verdict": verdict,
        "layout_mode": layout_mode,
        "workspace_root": str(workspace),
        "canonical_amof_code_path": str(canonical_amof),
        "canonical_ui_path": str(canonical_ui),
        "root_workspace_role": root_workspace_role,
        "runtime_import_source": resolved_import_origin,
        "runtime_import_is_canonical": import_is_canonical,
        "root_scripts_before_canonical": root_before_canonical,
        "surfaces": surfaces,
        "app_data": app_data,
        "toolchain": toolchain,
        "contracts": contracts,
        "contexts": {
            "available_contexts": sorted(contexts.get("contexts", {}).keys()),
            "current": context_summary,
        },
        "secret_exposure": secret_exposure,
        "warnings": warnings,
        "failures": failures,
    }


def cmd_doctor(args: Any = None) -> int:
    report = topology_report()
    as_json = bool(getattr(args, "json", False))
    if as_json:
        print(json.dumps(report, indent=2, sort_keys=False))
    else:
        print(f"AMOF doctor: {report['verdict']}")
        print(f"  layout: {report['layout_mode']}")
        print(f"  workspace: {report['workspace_root']}")
        print(f"  canonical AMOF: {report['canonical_amof_code_path']}")
        print(f"  canonical UI:   {report['canonical_ui_path']}")
        print(f"  import source:  {report['runtime_import_source']}")
        current_context = report["contexts"]["current"]
        print(f"  current context: {current_context['current_context']}")
        print(f"  app config:      {report['app_data']['files']['config_file']['path']}")
        print(f"  evidence dir:    {report['app_data']['roots']['evidence_dir']['path']}")
        print(f"  workspaces dir:  {report['app_data']['roots']['workspaces_dir']['path']}")
        for key, surface in report["surfaces"].items():
            print(
                f"  {key}: branch={surface.get('branch')} head={surface.get('head')} "
                f"dirty={surface.get('dirty_count')} path={surface.get('path')}"
            )
        required_tools = ", ".join(
            f"{name}={probe.get('version') or 'missing'}"
            for name, probe in report["toolchain"].items()
            if probe["required"]
        )
        print(f"  required tools: {required_tools}")
        optional_missing = [
            name for name, probe in report["toolchain"].items() if not probe["required"] and not probe["available"]
        ]
        if optional_missing:
            print(f"  optional tools missing: {', '.join(optional_missing)}")
        for warning in report["warnings"]:
            print(f"  WARN: {warning}")
        for failure in report["failures"]:
            print(f"  FAIL: {failure}")
    return 2 if report["verdict"] == "FAIL" else 0

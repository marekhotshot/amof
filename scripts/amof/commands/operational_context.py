"""Kubectl-like AMOF operational context commands."""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import yaml

from ..app_config import (
    add_named_context,
    get_context,
    get_current_context_name,
    load_contexts,
    resolve_active_context_name,
    set_current_context_name,
)

REMOTE_CONTEXT_REQUIRED_ENV = {
    "cloud-dev": ("AMOF_REMOTE_IAL_BASE_URL", "AMOF_REMOTE_IAL_API_KEY"),
    "msg-aws-dev": ("AMOF_REMOTE_IAL_BASE_URL", "AMOF_REMOTE_IAL_API_KEY"),
}

CONTEXT_DESCRIPTIONS = {
    "local": "Run planning and evidence via local AMOF app-data.",
    "cloud-dev": "Use cloud-dev remote controlplane/runtime contract.",
    "msg-aws-dev": "Use msg-aws-dev remote controlplane/runtime contract.",
}


def _format_prompt_context(name: str, *, plain: bool = False) -> str:
    normalized = str(name or "").strip() or "local"
    if normalized == "local":
        return ""
    return normalized if plain else f"({normalized})"


def _context_banner_payload(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    controlplane = payload.get("controlplane", {}) if isinstance(payload.get("controlplane"), dict) else {}
    execution = payload.get("execution", {}) if isinstance(payload.get("execution"), dict) else {}
    workspace = payload.get("workspace", {}) if isinstance(payload.get("workspace"), dict) else {}
    evidence = payload.get("evidence", {}) if isinstance(payload.get("evidence"), dict) else {}
    browser = payload.get("browser", {}) if isinstance(payload.get("browser"), dict) else {}
    kubernetes = payload.get("kubernetes", {}) if isinstance(payload.get("kubernetes"), dict) else {}
    safety = payload.get("safety", {}) if isinstance(payload.get("safety"), dict) else {}
    credentials = payload.get("credentials", {}) if isinstance(payload.get("credentials"), dict) else {}
    banner = {
        "name": name,
        "controlplane_mode": controlplane.get("mode"),
        "controlplane_url": controlplane.get("url"),
        "execution_backend": execution.get("backend"),
        "workspace_backend": workspace.get("backend"),
        "evidence_backend": evidence.get("backend"),
        "kubernetes_namespace": kubernetes.get("namespace"),
        "protected": bool(safety.get("protected", False)),
        "kubeconfig_ref": credentials.get("kubeconfig_ref"),
    }
    browser_backend = browser.get("backend")
    if browser_backend:
        banner["browser_backend"] = browser_backend
    return banner


def _render_context_banner(payload: dict[str, Any]) -> str:
    control_plane = str(payload.get("controlplane_mode") or "unknown")
    controlplane_url = str(payload.get("controlplane_url") or "").strip()
    if controlplane_url:
        control_plane = f"{control_plane} {controlplane_url}"

    lines = [
        f"AMOF context: {payload.get('name') or 'local'}",
        f"Control plane: {control_plane}",
        f"Execution backend: {payload.get('execution_backend') or 'unknown'}",
        f"Workspace backend: {payload.get('workspace_backend') or 'unknown'}",
        f"Evidence backend: {payload.get('evidence_backend') or 'unknown'}",
    ]

    namespace = str(payload.get("kubernetes_namespace") or "").strip()
    if namespace:
        lines.append(f"Kubernetes namespace: {namespace}")
    browser_backend = str(payload.get("browser_backend") or "").strip()
    if browser_backend:
        lines.append(f"Browser backend: {browser_backend}")
    lines.append(f"Protected: {str(bool(payload.get('protected', False))).lower()}")
    return "\n".join(lines)


def _context_kind(name: str) -> str:
    normalized = str(name or "").strip().lower()
    if normalized == "local":
        return "local"
    if normalized in {"cloud-dev", "msg-aws-dev"}:
        return "cloud"
    return "future"


def _context_availability(name: str) -> tuple[str, str]:
    normalized = str(name or "").strip()
    if _context_kind(normalized) == "local":
        return "available", "local context is always available"
    required_env = REMOTE_CONTEXT_REQUIRED_ENV.get(normalized, ())
    missing = [key for key in required_env if not str(os.environ.get(key) or "").strip()]
    if missing:
        return "unavailable", f"missing required env vars: {', '.join(missing)}"
    return "available", "required remote context env vars are present"


def _public_context_summary(name: str, payload: dict[str, Any], *, active: bool) -> dict[str, Any]:
    controlplane = payload.get("controlplane", {}) if isinstance(payload.get("controlplane"), dict) else {}
    execution = payload.get("execution", {}) if isinstance(payload.get("execution"), dict) else {}
    workspace = payload.get("workspace", {}) if isinstance(payload.get("workspace"), dict) else {}
    evidence = payload.get("evidence", {}) if isinstance(payload.get("evidence"), dict) else {}
    safety = payload.get("safety", {}) if isinstance(payload.get("safety"), dict) else {}
    availability, reason = _context_availability(name)
    return {
        "name": name,
        "kind": _context_kind(name),
        "active": bool(active),
        "availability": availability,
        "availability_reason": reason,
        "description": CONTEXT_DESCRIPTIONS.get(name, "Future/customer context placeholder."),
        "controlplane_mode": controlplane.get("mode"),
        "execution_backend": execution.get("backend"),
        "workspace_backend": workspace.get("backend"),
        "evidence_backend": evidence.get("backend"),
        "protected": bool(safety.get("protected", False)),
    }


def cmd_operational_context(args: Any) -> int:
    action = str(getattr(args, "service", "") or "").strip()
    target = str(getattr(args, "context_target", "") or "").strip() or None
    emit_json = bool(getattr(args, "json", False))

    try:
        if action == "current":
            current, source = resolve_active_context_name()
            if emit_json:
                print(json.dumps({"current_context": current, "source_of_resolution": source}, indent=2))
            else:
                print(current)
            return 0

        if action == "list":
            contexts = load_contexts()["contexts"]
            current, _source = resolve_active_context_name()
            summaries = [
                _public_context_summary(name, contexts[name], active=(name == current)) for name in sorted(contexts)
            ]
            if emit_json:
                print(json.dumps({"resolved_context": current, "contexts": summaries}, indent=2))
                return 0
            for item in summaries:
                marker = "*" if item["active"] else " "
                print(
                    f"{marker} {item['name']}\t{item['kind']}\t{item['availability']}\t{item['description']}"
                )
            return 0

        if action == "show":
            resolved_context, resolution_source = resolve_active_context_name()
            context_name = target or resolved_context
            source = "explicit_argument" if target else resolution_source
            payload = get_context(context_name)
            summary = _public_context_summary(context_name, payload, active=(context_name == resolved_context))
            if emit_json:
                print(
                    json.dumps(
                        {
                            "resolved_context": context_name,
                            "source_of_resolution": source,
                            "context": summary,
                        },
                        indent=2,
                    )
                )
            else:
                print(
                    yaml.safe_dump(
                        {
                            "resolved_context": context_name,
                            "source_of_resolution": source,
                            "context": summary,
                        },
                        sort_keys=False,
                    ).rstrip()
                )
            return 0

        if action == "banner":
            current = get_current_context_name()
            payload = _context_banner_payload(current, get_context(current))
            if emit_json:
                print(json.dumps(payload, indent=2))
            else:
                print(_render_context_banner(payload))
            return 0

        if action == "use":
            if target is None:
                raise ValueError("amof context use <name> requires a context name")
            set_current_context_name(target)
            print(f"active context set to {target}")
            return 0

        if action == "prompt":
            current = get_current_context_name()
            print(_format_prompt_context(current, plain=bool(getattr(args, "plain", False))))
            return 0

        if action == "doctor":
            resolved_context, source = resolve_active_context_name()
            payload = get_context(resolved_context)
            summary = _public_context_summary(resolved_context, payload, active=True)
            ok = summary["availability"] == "available"
            doctor_payload = {
                "resolved_context": resolved_context,
                "source_of_resolution": source,
                "context": summary,
                "status": "ok" if ok else "fail_closed",
            }
            if emit_json:
                print(json.dumps(doctor_payload, indent=2))
            else:
                print(yaml.safe_dump(doctor_payload, sort_keys=False).rstrip())
            if not ok:
                sys.stderr.write(
                    f"[context] FAIL_CLOSED: selected context '{resolved_context}' is unavailable ({summary['availability_reason']}). No fallback to local.\n"
                )
                return 1
            return 0

        if action == "add":
            if target is None:
                raise ValueError("amof context add <name> requires a supported context name")
            payload = add_named_context(
                target,
                {
                    "controlplane_mode": getattr(args, "controlplane_mode", None),
                    "controlplane_url": getattr(args, "controlplane_url", None),
                    "execution_backend": getattr(args, "execution_backend", None),
                    "workspace_backend": getattr(args, "workspace_backend", None),
                    "evidence_backend": getattr(args, "evidence_backend", None),
                    "browser_backend": getattr(args, "browser_backend", None),
                    "browser_recordings": getattr(args, "browser_recordings", None),
                    "browser_human_in_loop": getattr(args, "browser_human_in_loop", None),
                    "browser_allowed_hosts": getattr(args, "browser_allowed_hosts", None),
                    "kubeconfig_ref": getattr(args, "kubeconfig_ref", None),
                    "namespace": getattr(args, "namespace", None),
                },
            )
            if emit_json:
                print(json.dumps({"name": target, "context": payload}, indent=2))
            else:
                print(f"added {target}")
            return 0
    except (KeyError, ValueError) as exc:
        sys.stderr.write(f"[context] {exc}\n")
        return 1

    sys.stderr.write("Usage: amof context <current|list|show|use|doctor|add|prompt|banner> [name]\n")
    return 1


__all__ = ["cmd_operational_context"]

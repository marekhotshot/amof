"""Kubectl-like AMOF operational context commands."""

from __future__ import annotations

import json
import sys
from typing import Any

import yaml

from ..app_config import (
    add_named_context,
    ensure_default_context_config,
    get_context,
    get_current_context_name,
    load_contexts,
    set_current_context_name,
)


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


def cmd_operational_context(args: Any) -> int:
    ensure_default_context_config()
    action = str(getattr(args, "service", "") or "").strip()
    target = str(getattr(args, "context_target", "") or "").strip() or None
    emit_json = bool(getattr(args, "json", False))

    try:
        if action == "current":
            current = get_current_context_name()
            if emit_json:
                print(json.dumps({"current_context": current}, indent=2))
            else:
                print(current)
            return 0

        if action == "list":
            contexts = load_contexts()["contexts"]
            current = get_current_context_name()
            if emit_json:
                print(json.dumps({"current_context": current, "contexts": contexts}, indent=2))
                return 0
            for name in sorted(contexts):
                marker = "*" if name == current else " "
                mode = contexts[name].get("controlplane", {}).get("mode", "unknown")
                print(f"{marker} {name}\t{mode}")
            return 0

        if action == "show":
            if target is None:
                raise ValueError("amof context show <name> requires a context name")
            payload = get_context(target)
            if emit_json:
                print(json.dumps({"name": target, "context": payload}, indent=2))
            else:
                print(yaml.safe_dump({"name": target, "context": payload}, sort_keys=False).rstrip())
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
            print(target)
            return 0

        if action == "prompt":
            current = get_current_context_name()
            print(_format_prompt_context(current, plain=bool(getattr(args, "plain", False))))
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

    sys.stderr.write("Usage: amof context <current|list|show|use|add|prompt|banner> [name]\n")
    return 1


__all__ = ["cmd_operational_context"]

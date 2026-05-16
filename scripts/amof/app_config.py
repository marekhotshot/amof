"""Read and write AMOF app-data configuration files."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .app_paths import (
    config_file,
    contexts_file,
    ensure_app_roots,
    ensure_parent_dir,
    get_app_paths,
    provider_profiles_dir,
    workspaces_registry_file,
)


DEFAULT_LOCAL_CONTEXT = {
    "controlplane": {
        "mode": "local-cli",
        "url": None,
        "deployment_variant": None,
    },
    "execution": {
        "backend": "local",
    },
    "workspace": {
        "backend": "local-appdata",
        "default_registry_entry": None,
    },
    "evidence": {
        "backend": "local-appdata",
    },
    "credentials": {
        "provider_profile_refs": [],
        "kubeconfig_ref": None,
    },
    "safety": {
        "protected": False,
        "require_confirmation": False,
        "no_push_default": True,
        "dry_run_default": True,
    },
    "promotion": {
        "default_policy": "evidence-gated-dry-run",
    },
}

ALLOWED_CONTROLPLANE_MODES = {"local-cli", "remote-api"}
ALLOWED_EXECUTION_BACKENDS = {"local", "remote-worker", "kubernetes-worker"}
ALLOWED_WORKSPACE_BACKENDS = {"local-appdata", "remote-worker-pvc", "object-store"}
ALLOWED_EVIDENCE_BACKENDS = {"local-appdata", "remote-controlplane", "mirrored"}
ALLOWED_BROWSER_BACKENDS = {"local-http", "local-playwright", "cloudflare-browser-run"}


def _load_yaml(path: Path, *, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        return deepcopy(default) if default is not None else {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return data
    return deepcopy(default) if default is not None else {}


def _write_yaml(path: Path, payload: dict[str, Any]) -> Path:
    ensure_app_roots()
    target = ensure_parent_dir(path)
    target.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return target


def load_global_config() -> dict[str, Any]:
    payload = _load_yaml(config_file(), default={"current_context": "local"})
    if not isinstance(payload.get("current_context"), str) or not str(payload["current_context"]).strip():
        payload["current_context"] = "local"
    return payload


def current_context_prompt_cache_file() -> Path:
    return get_app_paths().state_root / "current-context"


def save_global_config(payload: dict[str, Any]) -> Path:
    normalized = dict(payload)
    if not isinstance(normalized.get("current_context"), str) or not str(normalized["current_context"]).strip():
        normalized["current_context"] = "local"
    return _write_yaml(config_file(), normalized)


def load_contexts() -> dict[str, Any]:
    payload = _load_yaml(contexts_file(), default={"contexts": {"local": deepcopy(DEFAULT_LOCAL_CONTEXT)}})
    contexts = payload.get("contexts")
    if not isinstance(contexts, dict):
        contexts = {}
    if "local" not in contexts:
        contexts["local"] = deepcopy(DEFAULT_LOCAL_CONTEXT)
    return {"contexts": contexts}


def save_contexts(payload: dict[str, Any]) -> Path:
    contexts = payload.get("contexts")
    if not isinstance(contexts, dict):
        contexts = {}
    if "local" not in contexts:
        contexts["local"] = deepcopy(DEFAULT_LOCAL_CONTEXT)
    return _write_yaml(contexts_file(), {"contexts": contexts})


def ensure_default_context_config() -> None:
    save_contexts(load_contexts())
    save_global_config(load_global_config())


def get_current_context_name() -> str:
    ensure_default_context_config()
    return str(load_global_config().get("current_context") or "local")


def set_current_context_name(name: str) -> None:
    normalized = str(name or "").strip()
    if not normalized:
        raise ValueError("context name is required")
    contexts = load_contexts()["contexts"]
    if normalized not in contexts:
        raise KeyError(f"unknown AMOF context: {normalized}")
    config = load_global_config()
    config["current_context"] = normalized
    save_global_config(config)
    write_current_context_prompt_cache(normalized)


def write_current_context_prompt_cache(name: str) -> Path:
    normalized = str(name or "").strip() or "local"
    target = ensure_parent_dir(current_context_prompt_cache_file())
    target.write_text(normalized + "\n", encoding="utf-8")
    return target


def get_context(name: str) -> dict[str, Any]:
    normalized = str(name or "").strip()
    if not normalized:
        raise KeyError("context name is required")
    contexts = load_contexts()["contexts"]
    if normalized not in contexts:
        raise KeyError(f"unknown AMOF context: {normalized}")
    return deepcopy(contexts[normalized])


def upsert_context(name: str, payload: dict[str, Any]) -> None:
    normalized = str(name or "").strip()
    if not normalized:
        raise ValueError("context name is required")
    contexts_payload = load_contexts()
    contexts_payload["contexts"][normalized] = deepcopy(payload)
    save_contexts(contexts_payload)


def activate_provider_profile_ref(profile_name: str, *, context_name: str | None = None) -> dict[str, Any]:
    normalized_profile = str(profile_name or "").strip()
    if not normalized_profile:
        raise ValueError("provider profile name is required")
    target_context = str(context_name or get_current_context_name() or "local").strip() or "local"
    context_payload = get_context(target_context)
    credentials = context_payload.setdefault("credentials", {})
    if not isinstance(credentials, dict):
        credentials = {}
        context_payload["credentials"] = credentials
    refs = credentials.get("provider_profile_refs")
    provider_refs = [str(item) for item in refs] if isinstance(refs, list) else []
    if normalized_profile not in provider_refs:
        provider_refs.append(normalized_profile)
    credentials["provider_profile_refs"] = provider_refs
    upsert_context(target_context, context_payload)
    return deepcopy(context_payload)


def get_provider_profile_refs(*, context_name: str | None = None) -> list[str]:
    target_context = str(context_name or get_current_context_name() or "local").strip() or "local"
    context_payload = get_context(target_context)
    credentials = context_payload.get("credentials")
    if not isinstance(credentials, dict):
        return []
    refs = credentials.get("provider_profile_refs")
    if not isinstance(refs, list):
        return []
    return [str(item).strip() for item in refs if str(item).strip()]


def load_provider_profile(profile_name: str) -> dict[str, Any]:
    normalized = str(profile_name or "").strip()
    if not normalized:
        raise ValueError("provider profile name is required")
    path = provider_profiles_dir() / f"{normalized}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"provider profile not found: {path}")
    payload = _load_yaml(path, default={})
    if not payload:
        raise ValueError(f"provider profile is empty or invalid: {normalized}")
    payload.setdefault("name", normalized)
    return deepcopy(payload)


def _normalize_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _validate_choice(value: str | None, *, field_name: str, allowed: set[str]) -> str | None:
    normalized = _normalize_optional_string(value)
    if normalized is None:
        return None
    if normalized not in allowed:
        options = ", ".join(sorted(allowed))
        raise ValueError(f"{field_name} must be one of: {options}")
    return normalized


def _normalize_optional_bool(value: Any) -> bool | None:
    normalized = _normalize_optional_string(value)
    if normalized is None:
        return None
    lowered = normalized.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    raise ValueError("boolean fields must be one of: true, false")


def _normalize_optional_host_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    items = value if isinstance(value, list) else [value]
    normalized: list[str] = []
    for item in items:
        host = _normalize_optional_string(item)
        if host is not None:
            normalized.append(host)
    return normalized or None


def _apply_context_overrides(base_payload: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = deepcopy(base_payload)
    normalized_overrides = overrides or {}

    controlplane_mode = _validate_choice(
        normalized_overrides.get("controlplane_mode"),
        field_name="controlplane mode",
        allowed=ALLOWED_CONTROLPLANE_MODES,
    )
    controlplane_url = _normalize_optional_string(normalized_overrides.get("controlplane_url"))
    execution_backend = _validate_choice(
        normalized_overrides.get("execution_backend"),
        field_name="execution backend",
        allowed=ALLOWED_EXECUTION_BACKENDS,
    )
    workspace_backend = _validate_choice(
        normalized_overrides.get("workspace_backend"),
        field_name="workspace backend",
        allowed=ALLOWED_WORKSPACE_BACKENDS,
    )
    evidence_backend = _validate_choice(
        normalized_overrides.get("evidence_backend"),
        field_name="evidence backend",
        allowed=ALLOWED_EVIDENCE_BACKENDS,
    )
    browser_backend = _validate_choice(
        normalized_overrides.get("browser_backend"),
        field_name="browser backend",
        allowed=ALLOWED_BROWSER_BACKENDS,
    )
    browser_recordings = _normalize_optional_bool(normalized_overrides.get("browser_recordings"))
    browser_human_in_loop = _normalize_optional_bool(normalized_overrides.get("browser_human_in_loop"))
    browser_allowed_hosts = _normalize_optional_host_list(normalized_overrides.get("browser_allowed_hosts"))
    kubeconfig_ref = _normalize_optional_string(normalized_overrides.get("kubeconfig_ref"))
    namespace = _normalize_optional_string(normalized_overrides.get("namespace"))

    if controlplane_mode is not None:
        payload.setdefault("controlplane", {})["mode"] = controlplane_mode
    if controlplane_url is not None:
        payload.setdefault("controlplane", {})["url"] = controlplane_url
    if execution_backend is not None:
        payload.setdefault("execution", {})["backend"] = execution_backend
    if workspace_backend is not None:
        payload.setdefault("workspace", {})["backend"] = workspace_backend
    if evidence_backend is not None:
        payload.setdefault("evidence", {})["backend"] = evidence_backend
    if browser_backend is not None:
        payload.setdefault("browser", {})["backend"] = browser_backend
    if browser_recordings is not None:
        payload.setdefault("browser", {})["recordings"] = browser_recordings
    if browser_human_in_loop is not None:
        payload.setdefault("browser", {})["human_in_loop"] = browser_human_in_loop
    if browser_allowed_hosts is not None:
        payload.setdefault("browser", {})["allowed_hosts"] = browser_allowed_hosts
    if kubeconfig_ref is not None:
        payload.setdefault("credentials", {})["kubeconfig_ref"] = kubeconfig_ref
    if namespace is not None:
        payload.setdefault("kubernetes", {})["namespace"] = namespace

    return payload


def add_named_context(name: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized = str(name or "").strip()
    if normalized == "local":
        payload = deepcopy(DEFAULT_LOCAL_CONTEXT)
    elif normalized == "cloud-dev":
        payload = deepcopy(DEFAULT_LOCAL_CONTEXT)
        payload["controlplane"] = {
            "mode": "remote-api",
            "url": None,
            "deployment_variant": "cloud-dev",
        }
        payload["evidence"] = {"backend": "mirrored"}
        payload["safety"]["require_confirmation"] = True
    else:
        raise ValueError("supported context names are: local, cloud-dev")
    payload = _apply_context_overrides(payload, overrides)
    upsert_context(normalized, payload)
    return payload


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalized_registry_payload(payload: dict[str, Any]) -> dict[str, Any]:
    workspaces = payload.get("workspaces")
    if not isinstance(workspaces, dict):
        workspaces = {}
    repo_bindings = payload.get("repo_bindings")
    if not isinstance(repo_bindings, dict):
        repo_bindings = {}
    adopted_ecosystems = payload.get("adopted_ecosystems")
    if not isinstance(adopted_ecosystems, dict):
        adopted_ecosystems = {}
    return {
        "workspaces": workspaces,
        "repo_bindings": repo_bindings,
        "adopted_ecosystems": adopted_ecosystems,
    }


def load_workspace_registry() -> dict[str, Any]:
    payload = _load_yaml(workspaces_registry_file(), default={"workspaces": {}})
    return _normalized_registry_payload(payload)


def save_workspace_registry(payload: dict[str, Any]) -> Path:
    return _write_yaml(workspaces_registry_file(), _normalized_registry_payload(payload))


def register_workspace(name: str, path: str, *, default_ref: str | None = None) -> dict[str, Any]:
    normalized_name = str(name or "").strip()
    normalized_path = str(path or "").strip()
    normalized_ref = str(default_ref or "main").strip() or "main"
    if not normalized_name:
        raise ValueError("workspace name is required")
    if not normalized_path:
        raise ValueError("workspace path is required")
    registry = load_workspace_registry()
    entry = {
        "name": normalized_name,
        "path": str(Path(normalized_path).expanduser().resolve(strict=False)),
        "default_ref": normalized_ref,
    }
    registry["workspaces"][normalized_name] = entry
    save_workspace_registry(registry)
    return entry


def build_adopted_ecosystem_manifest(
    *,
    ecosystem: str,
    repo_name: str,
    git_root: str | Path,
    default_ref: str | None = None,
) -> dict[str, Any]:
    normalized_ref = str(default_ref or "main").strip() or "main"
    root = str(Path(git_root).expanduser().resolve(strict=False))
    return {
        "ecosystem": ecosystem,
        "name": ecosystem,
        "description": f"App-data adopted repository: {repo_name}",
        "manifest_source": "appdata",
        "repos": [
            {
                "name": repo_name,
                "url": f"local://{root}",
                "path": root,
                "branch": normalized_ref,
                "readonly": False,
            }
        ],
    }


def adopt_repo_binding(
    *,
    git_root: str | Path,
    ecosystem: str,
    repo_name: str,
    default_ref: str | None = None,
) -> dict[str, Any]:
    normalized_ecosystem = str(ecosystem or "").strip()
    normalized_repo_name = str(repo_name or "").strip()
    normalized_ref = str(default_ref or "main").strip() or "main"
    root = str(Path(git_root).expanduser().resolve(strict=False))
    if not normalized_ecosystem:
        raise ValueError("ecosystem name is required")
    if not normalized_repo_name:
        raise ValueError("repo name is required")
    registry = load_workspace_registry()
    now = _utc_timestamp()
    previous = registry["repo_bindings"].get(root)
    created_at = previous.get("created_at") if isinstance(previous, dict) else None
    entry = {
        "git_root": root,
        "ecosystem": normalized_ecosystem,
        "repo_name": normalized_repo_name,
        "default_ref": normalized_ref,
        "manifest_source": "appdata",
        "created_at": created_at or now,
        "updated_at": now,
    }
    registry["repo_bindings"][root] = entry
    registry["adopted_ecosystems"][normalized_ecosystem] = build_adopted_ecosystem_manifest(
        ecosystem=normalized_ecosystem,
        repo_name=normalized_repo_name,
        git_root=root,
        default_ref=normalized_ref,
    )
    save_workspace_registry(registry)
    return entry


def get_repo_binding_for_git_root(git_root: str | Path) -> dict[str, Any] | None:
    root = str(Path(git_root).expanduser().resolve(strict=False))
    entry = load_workspace_registry()["repo_bindings"].get(root)
    return deepcopy(entry) if isinstance(entry, dict) else None


def get_adopted_ecosystem_manifest(ecosystem: str) -> dict[str, Any] | None:
    normalized = str(ecosystem or "").strip()
    manifest = load_workspace_registry()["adopted_ecosystems"].get(normalized)
    return deepcopy(manifest) if isinstance(manifest, dict) else None


def list_adopted_ecosystems() -> list[str]:
    return sorted(str(name) for name in load_workspace_registry()["adopted_ecosystems"])


def get_registered_workspace(name: str) -> dict[str, Any]:
    normalized = str(name or "").strip()
    registry = load_workspace_registry()["workspaces"]
    if normalized not in registry:
        raise KeyError(f"unknown registered workspace: {normalized}")
    return deepcopy(registry[normalized])


__all__ = [
    "DEFAULT_LOCAL_CONTEXT",
    "ALLOWED_CONTROLPLANE_MODES",
    "ALLOWED_BROWSER_BACKENDS",
    "ALLOWED_EVIDENCE_BACKENDS",
    "ALLOWED_EXECUTION_BACKENDS",
    "ALLOWED_WORKSPACE_BACKENDS",
    "add_named_context",
    "activate_provider_profile_ref",
    "current_context_prompt_cache_file",
    "ensure_default_context_config",
    "get_context",
    "get_current_context_name",
    "get_adopted_ecosystem_manifest",
    "get_provider_profile_refs",
    "get_registered_workspace",
    "get_repo_binding_for_git_root",
    "load_provider_profile",
    "list_adopted_ecosystems",
    "load_contexts",
    "load_global_config",
    "load_workspace_registry",
    "adopt_repo_binding",
    "build_adopted_ecosystem_manifest",
    "register_workspace",
    "save_contexts",
    "save_global_config",
    "save_workspace_registry",
    "set_current_context_name",
    "upsert_context",
    "write_current_context_prompt_cache",
]

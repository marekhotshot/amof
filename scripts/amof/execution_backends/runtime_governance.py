"""Execution adapter authority checks and bound write-scope enforcement."""

from __future__ import annotations

from pathlib import Path
from typing import Any

FUTURE_ISOLATION_MODELS = ("session_execution_environment", "run_execution_environment")
SUPPORTED_CAPABILITIES = ("read", "bounded_write", "shell_limited", "focused_tests")
DANGEROUS_CAPABILITIES = {
    "kubernetes_mutation", "deployment", "deploy", "secrets", "secret_access",
    "network_unrestricted", "unrestricted_network", "push", "promotion",
    "promote", "tags", "releases",
}


def assert_no_dangerous_caps(
    capabilities: list[str], *, backend_name: str, error_type: type[Exception] = RuntimeError,
) -> None:
    dangerous = sorted({cap for cap in capabilities if cap in DANGEROUS_CAPABILITIES})
    if dangerous:
        raise error_type(
            f"dangerous capabilities are not available for {backend_name} backend: "
            + ", ".join(dangerous)
        )


def apply_write_scope_enforcement_if_bound(
    result: dict[str, Any], *, selection: Any, run_id: str, workspace: Path,
) -> dict[str, Any]:
    """Enforce an active binding and attach its MutationReceipt; never grant one."""
    from ..write_scope_bindings import list_bindings, load_binding
    from ..write_scope_enforcement import apply_enforcement_to_result

    binding = None
    binding_id = getattr(selection, "write_scope_binding_id", None)
    if binding_id:
        try:
            binding = load_binding(str(binding_id))
        except Exception:
            binding = None
    if binding is None:
        active = list_bindings(run_id=run_id, status="active")
        binding = active[0] if active else None
    if binding is None:
        return result
    return apply_enforcement_to_result(result, binding=binding, workspace_root=workspace)

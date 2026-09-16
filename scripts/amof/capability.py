"""Thin capability-kind authority (v0).

Conceptual tree — not a second permission platform:

    Authority
     ├── git.write            existing Write-Scope Authority (unchanged)
     ├── kubernetes.read      Kubernetes sibling
     │   kubernetes.mutate
     └── reference.action     local reference-system sibling (demo fixtures)

    Write-Scope remains the Git filesystem implementation. Kubernetes remains
    one Deployment get/patch lane. reference.action is one object/action/state
    primitive for local reference systems — not enterprise integrations or RBAC.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

CAPABILITY_GIT_WRITE = "git.write"
CAPABILITY_KUBERNETES_READ = "kubernetes.read"
CAPABILITY_KUBERNETES_MUTATE = "kubernetes.mutate"
CAPABILITY_REFERENCE_ACTION = "reference.action"

KUBERNETES_CAPABILITIES = frozenset(
    {CAPABILITY_KUBERNETES_READ, CAPABILITY_KUBERNETES_MUTATE}
)
REFERENCE_CAPABILITIES = frozenset({CAPABILITY_REFERENCE_ACTION})

READ_VERBS = frozenset({"get", "list"})
MUTATE_VERBS = frozenset({"patch"})
SUPPORTED_VERBS = READ_VERBS | MUTATE_VERBS

# v0 advertised surface. Enforcement still fail-closes on any verb/resource
# outside the frozen grant, including values not in this set.
V0_SUPPORTED_RESOURCES = frozenset({"deployments"})

_CLUSTER_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._:-]{0,61}[A-Za-z0-9])?$")
_DNS_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_RESOURCE_RE = re.compile(r"^[a-z][a-z0-9-]{0,61}$")

KUBERNETES_BODY_FIELDS = (
    "capability",
    "cluster",
    "namespaces",
    "verbs",
    "resources",
    "denied_namespaces",
    "denied_verbs",
    "denied_resources",
    "reason",
)
KUBERNETES_BODY_HASH_FIELDS = (
    "capability",
    "cluster",
    "namespaces",
    "verbs",
    "resources",
    "denied_namespaces",
    "denied_verbs",
    "denied_resources",
)


class CapabilityBodyError(ValueError):
    """Raised when a capability body cannot be normalized truthfully."""


def _unique_preserve(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in values:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _string_list(value: Any, *, name: str, required: bool = False) -> list[str]:
    if value is None:
        if required:
            raise CapabilityBodyError(f"{name} is required")
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        if any(not isinstance(item, str) for item in value):
            raise CapabilityBodyError(f"{name} entries must be strings")
        items = list(value)
    else:
        raise CapabilityBodyError(f"{name} must be a string or list of strings")
    cleaned = [item.strip() for item in items]
    if any(not item for item in cleaned):
        raise CapabilityBodyError(f"{name} rejects empty entries")
    return _unique_preserve(cleaned)


def normalize_kubernetes_capability_body(value: Any) -> dict[str, Any]:
    """Normalize and freeze a v0 Kubernetes capability body.

    Accepts ``namespace`` (singular, mission shape) or ``namespaces``.
    TTL lives on the Approval, not the body — same as Write-Scope.
    """
    if not isinstance(value, dict):
        raise CapabilityBodyError("capability body must be an object")
    capability = str(value.get("capability") or "").strip()
    if capability not in KUBERNETES_CAPABILITIES:
        raise CapabilityBodyError(
            f"unsupported capability {capability!r}; "
            f"v0 supports {sorted(KUBERNETES_CAPABILITIES)}"
        )
    cluster = str(value.get("cluster") or "").strip()
    if not cluster or _CLUSTER_RE.fullmatch(cluster) is None:
        raise CapabilityBodyError("cluster must be a logical target id")

    raw_namespaces = value.get("namespaces")
    if raw_namespaces is None and value.get("namespace") is not None:
        raw_namespaces = value.get("namespace")
    namespaces = _string_list(raw_namespaces, name="namespaces", required=True)
    if not namespaces:
        raise CapabilityBodyError("at least one namespace is required")
    bad_ns = [item for item in namespaces if _DNS_LABEL_RE.fullmatch(item) is None]
    if bad_ns:
        raise CapabilityBodyError(f"invalid namespace(s): {bad_ns}")

    verbs = [item.lower() for item in _string_list(value.get("verbs"), name="verbs", required=True)]
    if not verbs:
        raise CapabilityBodyError("at least one verb is required")
    unknown_verbs = [item for item in verbs if item not in SUPPORTED_VERBS]
    if unknown_verbs:
        raise CapabilityBodyError(
            f"unsupported verb(s) {unknown_verbs}; v0 supports {sorted(SUPPORTED_VERBS)}"
        )
    if capability == CAPABILITY_KUBERNETES_READ and any(
        item in MUTATE_VERBS for item in verbs
    ):
        raise CapabilityBodyError(
            "kubernetes.read cannot include mutate verbs; use kubernetes.mutate"
        )

    resources = [
        item.lower()
        for item in _string_list(value.get("resources"), name="resources", required=True)
    ]
    if not resources:
        raise CapabilityBodyError("at least one resource is required")
    bad_res = [item for item in resources if _RESOURCE_RE.fullmatch(item) is None]
    if bad_res:
        raise CapabilityBodyError(f"invalid resource(s): {bad_res}")

    denied_namespaces = _string_list(value.get("denied_namespaces"), name="denied_namespaces")
    bad_denied_ns = [
        item for item in denied_namespaces if _DNS_LABEL_RE.fullmatch(item) is None
    ]
    if bad_denied_ns:
        raise CapabilityBodyError(f"invalid denied_namespace(s): {bad_denied_ns}")
    denied_verbs = [
        item.lower()
        for item in _string_list(value.get("denied_verbs"), name="denied_verbs")
    ]
    unknown_denied = [item for item in denied_verbs if item not in SUPPORTED_VERBS]
    if unknown_denied:
        raise CapabilityBodyError(f"unsupported denied verb(s): {unknown_denied}")
    denied_resources = [
        item.lower()
        for item in _string_list(value.get("denied_resources"), name="denied_resources")
    ]
    bad_denied_res = [
        item for item in denied_resources if _RESOURCE_RE.fullmatch(item) is None
    ]
    if bad_denied_res:
        raise CapabilityBodyError(f"invalid denied_resource(s): {bad_denied_res}")

    reason = str(value.get("reason") or "").strip()
    if not reason:
        raise CapabilityBodyError("reason is required")

    return {
        "capability": capability,
        "cluster": cluster,
        "namespaces": namespaces,
        "verbs": verbs,
        "resources": resources,
        "denied_namespaces": denied_namespaces,
        "denied_verbs": denied_verbs,
        "denied_resources": denied_resources,
        "reason": reason,
    }


def compute_kubernetes_body_hash(body: dict[str, Any]) -> str:
    payload = {field: body[field] for field in KUBERNETES_BODY_HASH_FIELDS}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def verb_requires_mutate(verb: str) -> bool:
    return str(verb or "").strip().lower() in MUTATE_VERBS


__all__ = [
    "CAPABILITY_GIT_WRITE",
    "CAPABILITY_KUBERNETES_MUTATE",
    "CAPABILITY_KUBERNETES_READ",
    "CAPABILITY_REFERENCE_ACTION",
    "CapabilityBodyError",
    "REFERENCE_CAPABILITIES",
    "KUBERNETES_BODY_FIELDS",
    "KUBERNETES_BODY_HASH_FIELDS",
    "KUBERNETES_CAPABILITIES",
    "MUTATE_VERBS",
    "READ_VERBS",
    "SUPPORTED_VERBS",
    "V0_SUPPORTED_RESOURCES",
    "compute_kubernetes_body_hash",
    "normalize_kubernetes_capability_body",
    "verb_requires_mutate",
]

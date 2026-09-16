"""Live Kubernetes adapter for one governed Deployment get/patch path.

The adapter receives an already validated/bound KubernetesOperation. It does
not invent or widen authority. Cluster credentials stay on disk as a path
reference; kubeconfig contents never enter receipts.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from .app_paths import ensure_parent_dir, get_app_paths
from .capability import _DNS_LABEL_RE
from .kubernetes_capability import (
    ALLOWED_ANNOTATION_KEY,
    KubernetesCapabilityError,
    KubernetesOperation,
    _sha256_canonical,
    normalize_patch,
)

LIVE_GET = "get"
LIVE_PATCH = "patch"
LIVE_RESOURCES = frozenset({"deployments"})
LIVE_VERBS = frozenset({LIVE_GET, LIVE_PATCH})
TARGET_REGISTRY_SCHEMA = 1
SAFE_CONTEXT_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._:@-]{0,251}[A-Za-z0-9])?$")
ANNOTATION_VALUE_RE = re.compile(r"^[A-Za-z0-9._:/=@-]{1,128}$")
KUBECTL_TIMEOUT_SECONDS = 30


class KubernetesTransport(Protocol):
    def get_deployment(
        self,
        *,
        kubeconfig: str,
        context: str,
        namespace: str,
        name: str,
    ) -> dict[str, Any]:
        """Return a Deployment object from the API."""

    def patch_deployment_annotation(
        self,
        *,
        kubeconfig: str,
        context: str,
        namespace: str,
        name: str,
        key: str,
        value: str,
    ) -> dict[str, Any]:
        """Apply the AMOF-owned annotation merge patch."""


@dataclass(frozen=True)
class ResolvedClusterTarget:
    cluster: str
    kubeconfig: Path
    context: str


def targets_registry_path() -> Path:
    override = str(os.environ.get("AMOF_K8S_TARGETS_FILE") or "").strip()
    if override:
        return Path(override).expanduser()
    return get_app_paths().data_root / "capabilities" / "kubernetes" / "targets.json"


def _assert_dns_label(value: str, *, field: str) -> str:
    text = str(value or "").strip()
    if _DNS_LABEL_RE.fullmatch(text) is None:
        raise KubernetesCapabilityError(f"invalid {field}: {value!r}", code="wrong_resource")
    return text


def _assert_context(value: str) -> str:
    text = str(value or "").strip()
    if SAFE_CONTEXT_RE.fullmatch(text) is None:
        raise KubernetesCapabilityError(
            "cluster context must be a logical kube context name",
            code="wrong_cluster",
        )
    return text


def normalize_target_record(cluster: str, value: Any) -> ResolvedClusterTarget:
    if not isinstance(value, dict):
        raise KubernetesCapabilityError(
            f"cluster target {cluster!r} must be an object",
            code="wrong_cluster",
        )
    extra = set(value) - {"kubeconfig", "context"}
    if extra:
        raise KubernetesCapabilityError(
            f"cluster target {cluster!r} has unsupported fields {sorted(extra)}",
            code="wrong_cluster",
        )
    kubeconfig = Path(str(value.get("kubeconfig") or "")).expanduser()
    if not kubeconfig.is_absolute():
        raise KubernetesCapabilityError(
            f"cluster target {cluster!r} kubeconfig must be an absolute path",
            code="wrong_cluster",
        )
    if not kubeconfig.is_file():
        raise KubernetesCapabilityError(
            f"cluster target {cluster!r} kubeconfig path is not a file",
            code="wrong_cluster",
        )
    return ResolvedClusterTarget(
        cluster=str(cluster),
        kubeconfig=kubeconfig,
        context=_assert_context(str(value.get("context") or "")),
    )


def load_target_registry(path: Path | None = None) -> dict[str, ResolvedClusterTarget]:
    registry_path = path or targets_registry_path()
    if not registry_path.is_file():
        return {}
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KubernetesCapabilityError(
            "cluster target registry is unreadable",
            code="wrong_cluster",
        ) from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != TARGET_REGISTRY_SCHEMA:
        raise KubernetesCapabilityError(
            "unsupported cluster target registry schema",
            code="wrong_cluster",
        )
    raw_targets = payload.get("targets")
    if not isinstance(raw_targets, dict):
        raise KubernetesCapabilityError(
            "cluster target registry targets must be an object",
            code="wrong_cluster",
        )
    return {
        str(cluster): normalize_target_record(str(cluster), record)
        for cluster, record in raw_targets.items()
    }


def save_target_registry(
    targets: dict[str, dict[str, str]],
    *,
    path: Path | None = None,
) -> Path:
    registry_path = path or targets_registry_path()
    payload = {"schema_version": TARGET_REGISTRY_SCHEMA, "targets": targets}
    ensure_parent_dir(registry_path)
    registry_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(registry_path, 0o600)
    return registry_path


def resolve_cluster_target(
    cluster: str,
    *,
    registry: dict[str, ResolvedClusterTarget] | None = None,
    registry_path: Path | None = None,
) -> ResolvedClusterTarget:
    name = str(cluster or "").strip()
    if not name:
        raise KubernetesCapabilityError("cluster is required", code="wrong_cluster")
    mapping = registry if registry is not None else load_target_registry(registry_path)
    target = mapping.get(name)
    if target is None:
        raise KubernetesCapabilityError(
            f"no registered cluster target: {name}",
            code="wrong_cluster",
        )
    return target


def safe_deployment_evidence(obj: Any) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise KubernetesCapabilityError("deployment payload is not an object", code="unverified")
    metadata = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
    annotations = metadata.get("annotations") if isinstance(metadata.get("annotations"), dict) else {}
    generation = metadata.get("generation")
    return {
        "apiVersion": obj.get("apiVersion"),
        "kind": obj.get("kind"),
        "name": metadata.get("name"),
        "namespace": metadata.get("namespace"),
        "uid": metadata.get("uid"),
        "resourceVersion": None if metadata.get("resourceVersion") is None else str(metadata.get("resourceVersion")),
        "generation": generation if isinstance(generation, int) and not isinstance(generation, bool) else None,
        "annotation": annotations.get(ALLOWED_ANNOTATION_KEY),
    }


def _resource_version_newer(before: str | None, after: str | None) -> bool:
    if not before or not after:
        return False
    if before.isdigit() and after.isdigit():
        return int(after) > int(before)
    return after != before


def verify_live_result(
    *,
    operation: KubernetesOperation,
    before: dict[str, Any] | None,
    after: dict[str, Any],
    expected_annotation: str | None,
) -> tuple[bool, str | None]:
    if after.get("kind") not in {None, "Deployment"}:
        return False, "read-back kind is not Deployment"
    if after.get("name") != operation.name:
        return False, "read-back name does not match requested target"
    if after.get("namespace") != operation.namespace:
        return False, "read-back namespace does not match requested target"
    if after.get("uid") in {None, ""}:
        return False, "read-back is missing object uid"
    if before is not None and before.get("uid") not in {None, ""} and before.get("uid") != after.get("uid"):
        return False, "read-back uid diverged from pre-image"
    if operation.verb == LIVE_PATCH:
        if expected_annotation is None or after.get("annotation") != expected_annotation:
            return False, "expected annotation mutation is not observable"
        if before is None or not _resource_version_newer(
            before.get("resourceVersion"),
            after.get("resourceVersion"),
        ):
            return False, "resourceVersion did not advance after mutation"
    return True, None


class RecordingTransport:
    """Test double that records argv-equivalent calls and never shells out."""

    def __init__(self, objects: dict[tuple[str, str], dict[str, Any]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.objects = objects if objects is not None else {}
        self.fail_readback = False

    def _key(self, namespace: str, name: str) -> tuple[str, str]:
        return (namespace, name)

    def get_deployment(
        self,
        *,
        kubeconfig: str,
        context: str,
        namespace: str,
        name: str,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "op": "get",
                "kubeconfig": kubeconfig,
                "context": context,
                "namespace": namespace,
                "name": name,
            }
        )
        obj = self.objects.get(self._key(namespace, name))
        if obj is None:
            raise KubernetesCapabilityError("deployment not found", code="wrong_resource")
        return json.loads(json.dumps(obj))

    def patch_deployment_annotation(
        self,
        *,
        kubeconfig: str,
        context: str,
        namespace: str,
        name: str,
        key: str,
        value: str,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "op": "patch",
                "kubeconfig": kubeconfig,
                "context": context,
                "namespace": namespace,
                "name": name,
                "key": key,
                "value": value,
            }
        )
        obj = self.objects.get(self._key(namespace, name))
        if obj is None:
            raise KubernetesCapabilityError("deployment not found", code="wrong_resource")
        updated = json.loads(json.dumps(obj))
        metadata = updated.setdefault("metadata", {})
        annotations = metadata.setdefault("annotations", {})
        annotations[key] = value
        if self.fail_readback:
            return updated
        current = str(metadata.get("resourceVersion") or "1")
        metadata["resourceVersion"] = str(int(current) + 1) if current.isdigit() else f"{current}-next"
        metadata["generation"] = int(metadata.get("generation") or 1) + 1
        self.objects[self._key(namespace, name)] = updated
        return json.loads(json.dumps(updated))


class KubectlTransport:
    """AMOF-constructed kubectl argv. No shell string, no worker flags."""

    def __init__(
        self,
        *,
        kubectl: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        timeout: int = KUBECTL_TIMEOUT_SECONDS,
    ) -> None:
        self.kubectl = kubectl or shutil.which("kubectl") or "kubectl"
        self.runner = runner or subprocess.run
        self.timeout = timeout
        self.calls: list[list[str]] = []

    def _run(self, argv: list[str]) -> dict[str, Any]:
        if any(not isinstance(item, str) or item == "" for item in argv):
            raise KubernetesCapabilityError("invalid kubectl argv", code="invalid_request")
        self.calls.append(list(argv))
        try:
            completed = self.runner(
                argv,
                check=False,
                capture_output=True,
                text=True,
                shell=False,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise KubernetesCapabilityError("kubernetes transport timed out", code="unverified") from exc
        except OSError as exc:
            raise KubernetesCapabilityError("kubernetes transport is unavailable", code="unverified") from exc
        if completed.returncode != 0:
            raise KubernetesCapabilityError(
                "kubernetes transport rejected the API call",
                code="execution_failed",
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise KubernetesCapabilityError(
                "kubernetes transport returned non-JSON output",
                code="unverified",
            ) from exc
        if not isinstance(payload, dict):
            raise KubernetesCapabilityError(
                "kubernetes transport returned a non-object payload",
                code="unverified",
            )
        return payload

    def _base_argv(self, *, kubeconfig: str, context: str, namespace: str) -> list[str]:
        return [
            self.kubectl,
            "--kubeconfig",
            kubeconfig,
            "--context",
            context,
            "--namespace",
            namespace,
        ]

    def get_deployment(
        self,
        *,
        kubeconfig: str,
        context: str,
        namespace: str,
        name: str,
    ) -> dict[str, Any]:
        return self._run(
            [
                *self._base_argv(kubeconfig=kubeconfig, context=context, namespace=namespace),
                "get",
                "deployment",
                name,
                "-o",
                "json",
            ]
        )

    def patch_deployment_annotation(
        self,
        *,
        kubeconfig: str,
        context: str,
        namespace: str,
        name: str,
        key: str,
        value: str,
    ) -> dict[str, Any]:
        if key != ALLOWED_ANNOTATION_KEY:
            raise KubernetesCapabilityError("unsupported annotation key", code="invalid_patch")
        if ANNOTATION_VALUE_RE.fullmatch(value) is None:
            raise KubernetesCapabilityError("unsupported annotation value", code="invalid_patch")
        patch = {"metadata": {"annotations": {key: value}}}
        return self._run(
            [
                *self._base_argv(kubeconfig=kubeconfig, context=context, namespace=namespace),
                "patch",
                "deployment",
                name,
                "--type",
                "merge",
                "-p",
                json.dumps(patch, separators=(",", ":"), sort_keys=True),
                "-o",
                "json",
            ]
        )


@dataclass
class LiveKubernetesExecutor:
    """One governed live lane: Deployment get + annotation patch + read-back."""

    transport: KubernetesTransport = field(default_factory=KubectlTransport)
    registry: dict[str, ResolvedClusterTarget] | None = None
    registry_path: Path | None = None

    def execute(self, operation: KubernetesOperation) -> dict[str, Any]:
        resource = str(operation.resource or "").strip().lower()
        verb = str(operation.verb or "").strip().lower()
        if resource not in LIVE_RESOURCES:
            raise KubernetesCapabilityError(
                "live executor supports deployments only",
                code="wrong_resource",
            )
        if verb not in LIVE_VERBS:
            raise KubernetesCapabilityError(
                "live executor supports get and patch only",
                code="wrong_verb",
            )
        name = _assert_dns_label(str(operation.name or ""), field="name")
        namespace = _assert_dns_label(operation.namespace, field="namespace")
        target = resolve_cluster_target(
            operation.cluster,
            registry=self.registry,
            registry_path=self.registry_path,
        )
        kubeconfig = str(target.kubeconfig)
        context = target.context
        before_obj = self.transport.get_deployment(
            kubeconfig=kubeconfig,
            context=context,
            namespace=namespace,
            name=name,
        )
        before = safe_deployment_evidence(before_obj)
        expected_annotation = None
        after_obj = before_obj
        changed = False
        if verb == LIVE_PATCH:
            patch = normalize_patch(operation.patch)
            if not patch or "annotation" not in patch:
                raise KubernetesCapabilityError(
                    "live mutate requires the bounded annotation patch",
                    code="invalid_patch",
                )
            expected_annotation = patch["annotation"]["value"]
            after_obj = self.transport.patch_deployment_annotation(
                kubeconfig=kubeconfig,
                context=context,
                namespace=namespace,
                name=name,
                key=patch["annotation"]["key"],
                value=expected_annotation,
            )
            # Always read back. Process/API success is not verification.
            after_obj = self.transport.get_deployment(
                kubeconfig=kubeconfig,
                context=context,
                namespace=namespace,
                name=name,
            )
        after = safe_deployment_evidence(after_obj)
        verified, reason = verify_live_result(
            operation=operation,
            before=before,
            after=after,
            expected_annotation=expected_annotation,
        )
        changed = verb == LIVE_PATCH and after.get("annotation") != before.get("annotation")
        result = {
            "executed": True,
            "verified": verified,
            "verification_method": "live_readback_compare",
            "verification_reason": reason,
            "result_digest": _sha256_canonical({"before": before, "after": after}),
            "before_digest": _sha256_canonical(before),
            "after_digest": _sha256_canonical(after),
            "before": before,
            "after": after,
            "changed": changed,
        }
        if not verified:
            result["executed"] = verb == LIVE_PATCH
            result["verified"] = False
        return result


__all__ = [
    "ALLOWED_ANNOTATION_KEY",
    "KubectlTransport",
    "LiveKubernetesExecutor",
    "RecordingTransport",
    "ResolvedClusterTarget",
    "load_target_registry",
    "resolve_cluster_target",
    "safe_deployment_evidence",
    "save_target_registry",
    "targets_registry_path",
    "verify_live_result",
]

"""Minimal Argo CD client for bounded AMOF deploy flows.

Universal Argo pattern note (clean-start slices 5/6/7)
-----------------------------------------------------

The per-ecosystem universal Argo layout is frozen in
``infrastructure/gitops/README.md`` and scaffolded under
``infrastructure/gitops/_template/``. The canonical example
ecosystem ``gmd`` lives at ``infrastructure/gitops/gmd/``
(clean-start slice 6).

The ``demo_microsaas_*`` symbols and settings fields in this module
(``ArgoCdSettings`` fields ``demo_microsaas_app_prefix``,
``demo_microsaas_chart_path``,
``demo_microsaas_image_pull_secret``; helpers
``demo_microsaas_app_name``, ``build_demo_microsaas_application``,
``extract_demo_microsaas_application_status``; and the
``AMOF_ARGOCD_DEMO_MICROSAAS_*`` env vars in
``load_argocd_settings``) are RETIRED from current truth by
clean-start slice 7. They are preserved here byte-stable as the
seed reference and as a legacy-recovery surface for existing
demo-microsaas operators. They MUST NOT be extended; new
ecosystems plug into the universal pattern via the
``infrastructure/gitops/<ecosystem>/`` layout instead.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

HELM_OWNERSHIP_LABEL_KEYS = {
    "app.kubernetes.io/component",
    "app.kubernetes.io/instance",
    "app.kubernetes.io/managed-by",
    "app.kubernetes.io/name",
    "helm.sh/chart",
}

HELM_OWNERSHIP_ANNOTATION_KEYS = {
    "meta.helm.sh/release-name",
    "meta.helm.sh/release-namespace",
}


def _env_text(name: str, default: str = "") -> str:
    return str(os.environ.get(name) or default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_text(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = _env_text(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ArgoCdSettings:
    server: str
    token: str
    verify_ssl: bool
    namespace: str
    project: str
    repo_url: str
    target_revision: str
    destination_server: str
    demo_microsaas_app_prefix: str
    demo_microsaas_chart_path: str
    demo_microsaas_image_pull_secret: str
    sync_timeout_seconds: float
    sync_poll_seconds: float


class ArgoCdClientError(RuntimeError):
    """Base error for bounded Argo CD control-plane flows."""


class ArgoCdNotConfigured(ArgoCdClientError):
    """Raised when the control plane is missing Argo CD configuration."""


class ArgoCdHttpError(ArgoCdClientError):
    """Raised when Argo CD returns an unexpected HTTP response."""

    def __init__(self, method: str, path: str, status_code: int, body: str):
        self.method = method
        self.path = path
        self.status_code = status_code
        self.body = body
        super().__init__(f"Argo CD {method} {path} failed with HTTP {status_code}: {body}")


def load_argocd_settings() -> Optional[ArgoCdSettings]:
    server = _env_text("AMOF_ARGOCD_SERVER")
    token = _env_text("AMOF_ARGOCD_AUTH_TOKEN")
    if not server or not token:
        return None
    return ArgoCdSettings(
        server=server.rstrip("/"),
        token=token,
        verify_ssl=_env_bool("AMOF_ARGOCD_VERIFY_SSL", True),
        namespace=_env_text("AMOF_ARGOCD_NAMESPACE", "argocd") or "argocd",
        project=_env_text("AMOF_ARGOCD_PROJECT", "default") or "default",
        repo_url=_env_text("AMOF_ARGOCD_REPO_URL", "https://github.com/marekhotshot/amof.git"),
        target_revision=_env_text("AMOF_ARGOCD_REPO_REVISION", "main") or "main",
        destination_server=_env_text("AMOF_ARGOCD_DESTINATION_SERVER", "https://kubernetes.default.svc"),
        demo_microsaas_app_prefix=_env_text("AMOF_ARGOCD_DEMO_MICROSAAS_APP_PREFIX", "demo-microsaas"),
        demo_microsaas_chart_path=_env_text(
            "AMOF_ARGOCD_DEMO_MICROSAAS_CHART_PATH",
            "infrastructure/gitops/demo-microsaas/chart",
        ),
        demo_microsaas_image_pull_secret=_env_text("AMOF_ARGOCD_DEMO_MICROSAAS_IMAGE_PULL_SECRET", "ghcr-auth"),
        sync_timeout_seconds=max(5.0, _env_float("AMOF_ARGOCD_SYNC_TIMEOUT_SECONDS", 300.0)),
        sync_poll_seconds=max(1.0, _env_float("AMOF_ARGOCD_SYNC_POLL_SECONDS", 5.0)),
    )


def demo_microsaas_app_name(environment_id: str, settings: Optional[ArgoCdSettings] = None) -> str:
    config = settings or load_argocd_settings()
    prefix = config.demo_microsaas_app_prefix if config else "demo-microsaas"
    normalized = str(environment_id or "dev").strip() or "dev"
    return f"{prefix}-{normalized}"


def _public_host(public_base_url: Optional[str], host: Optional[str]) -> Optional[str]:
    explicit_host = str(host or "").strip()
    if explicit_host:
        return explicit_host
    parsed = urlparse(str(public_base_url or "").strip())
    return parsed.netloc or None


def _helm_parameter(name: str, value: Optional[str], *, force_string: bool = True) -> Optional[Dict[str, Any]]:
    text = str(value or "").strip()
    if not text:
        return None
    return {"name": name, "value": text, "forceString": force_string}


def _merged_application_metadata(existing: Dict[str, Any], desired: Dict[str, Any]) -> Dict[str, Any]:
    existing_meta = dict(existing.get("metadata") or {})
    desired_meta = dict(desired.get("metadata") or {})
    merged_labels = dict(existing_meta.get("labels") or {})
    for key, value in dict(desired_meta.get("labels") or {}).items():
        if key in HELM_OWNERSHIP_LABEL_KEYS and key in merged_labels:
            continue
        merged_labels[key] = value
    merged_annotations = dict(existing_meta.get("annotations") or {})
    for key, value in dict(desired_meta.get("annotations") or {}).items():
        if key in HELM_OWNERSHIP_ANNOTATION_KEYS and key in merged_annotations:
            continue
        merged_annotations[key] = value
    merged_meta = {
        "name": desired_meta.get("name") or existing_meta.get("name"),
        "namespace": desired_meta.get("namespace") or existing_meta.get("namespace"),
        "labels": merged_labels,
        "annotations": merged_annotations,
    }
    return merged_meta


def build_argocd_application(
    settings: ArgoCdSettings,
    *,
    app_name: str,
    ecosystem: str,
    environment_id: str,
    namespace: str,
    release_name: str,
    chart_path: str,
    parameters: Optional[List[Dict[str, Any]]] = None,
    labels: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    metadata_labels = {
        "app.kubernetes.io/part-of": "amof",
        "amof.dev/ecosystem": str(ecosystem or "").strip(),
        "amof.dev/environment-id": str(environment_id or "dev").strip() or "dev",
    }
    for key, value in dict(labels or {}).items():
        if value is None:
            continue
        metadata_labels[str(key)] = str(value)
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {
            "name": app_name,
            "namespace": settings.namespace,
            "labels": metadata_labels,
        },
        "spec": {
            "project": settings.project,
            "source": {
                "repoURL": settings.repo_url,
                "targetRevision": settings.target_revision,
                "path": chart_path,
                "helm": {
                    "releaseName": release_name,
                    "parameters": [row for row in list(parameters or []) if row is not None],
                },
            },
            "destination": {
                "server": settings.destination_server,
                "namespace": namespace,
            },
            "syncPolicy": {
                "syncOptions": ["CreateNamespace=true"],
            },
        },
    }


def build_demo_microsaas_application(
    settings: ArgoCdSettings,
    *,
    environment_id: str,
    namespace: str,
    release_name: str,
    image_repository: str,
    image_tag: str,
    image_digest: Optional[str] = None,
    public_base_url: Optional[str] = None,
    host: Optional[str] = None,
) -> Dict[str, Any]:
    app_name = demo_microsaas_app_name(environment_id, settings)
    parameters = [
        _helm_parameter("fullnameOverride", "microsaas-backend"),
        _helm_parameter("image.repository", image_repository),
        _helm_parameter("image.tag", image_tag),
        _helm_parameter("image.digest", image_digest),
        _helm_parameter("image.pullPolicy", "IfNotPresent"),
        _helm_parameter("service.port", "8000"),
        _helm_parameter("ingress.enabled", "true"),
        _helm_parameter("ingress.host", _public_host(public_base_url, host)),
        _helm_parameter("publicBaseUrl", public_base_url),
        _helm_parameter("imagePullSecrets[0].name", settings.demo_microsaas_image_pull_secret),
    ]
    return build_argocd_application(
        settings,
        app_name=app_name,
        ecosystem="demo-microsaas",
        environment_id=environment_id,
        namespace=namespace,
        release_name=release_name,
        chart_path=settings.demo_microsaas_chart_path,
        parameters=parameters,
    )


def extract_demo_microsaas_application_status(app: Dict[str, Any]) -> Dict[str, Any]:
    metadata = dict(app.get("metadata") or {})
    spec = dict(app.get("spec") or {})
    source = dict(spec.get("source") or {})
    helm = dict(source.get("helm") or {})
    parameters = {
        str(row.get("name") or ""): str(row.get("value") or "")
        for row in list(helm.get("parameters") or [])
        if isinstance(row, dict) and row.get("name")
    }
    status = dict(app.get("status") or {})
    sync = dict(status.get("sync") or {})
    health = dict(status.get("health") or {})
    operation_state = dict(status.get("operationState") or {})
    summary = dict(status.get("summary") or {})
    external_urls = list(summary.get("externalURLs") or [])
    public_url = str(external_urls[0] or "").strip() if external_urls else ""
    if not public_url:
        public_url = str(parameters.get("publicBaseUrl") or "").strip()
    host = _public_host(public_url, parameters.get("ingress.host"))
    image_repository = str(parameters.get("image.repository") or "").strip() or None
    image_tag = str(parameters.get("image.tag") or "").strip() or None
    image_digest = str(parameters.get("image.digest") or "").strip() or None
    if image_repository and image_digest:
        image = f"{image_repository}@{image_digest}"
    elif image_repository and image_tag:
        image = f"{image_repository}:{image_tag}"
    else:
        image = None
    sync_status = str(sync.get("status") or "").strip() or "Unknown"
    health_status = str(health.get("status") or "").strip() or "Unknown"
    operation_phase = str(operation_state.get("phase") or "").strip() or None
    ready = sync_status == "Synced" and health_status == "Healthy"
    if ready:
        state = "observed_live"
    elif sync_status == "OutOfSync" or health_status in {"Degraded", "Missing"}:
        state = "drift"
    elif operation_phase in {"Running", "Terminating"} or health_status == "Progressing":
        state = "deploying"
    else:
        state = "registered_only"
    return {
        "exists": True,
        "app_name": str(metadata.get("name") or "").strip() or None,
        "image": image,
        "image_tag": image_tag,
        "image_digest": image_digest,
        "ready": ready,
        "host": host,
        "public_url": public_url or None,
        "sync_status": sync_status,
        "health_status": health_status,
        "operation_phase": operation_phase,
        "finished_at": operation_state.get("finishedAt") or status.get("reconciledAt"),
        "checked_at": status.get("reconciledAt") or operation_state.get("finishedAt") or _now_iso(),
        "state": state,
        "detail": (
            f"Observed via Argo CD app {metadata.get('name')}: "
            f"sync={sync_status}, health={health_status}"
        ),
    }


class ArgoCdClient:
    """Tiny REST client for the small slice of Argo CD AMOF needs."""

    def __init__(self, settings: Optional[ArgoCdSettings] = None):
        self.settings = settings or load_argocd_settings()
        if self.settings is None:
            raise ArgoCdNotConfigured(
                "Argo CD is not configured. Set AMOF_ARGOCD_SERVER and AMOF_ARGOCD_AUTH_TOKEN."
            )
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {self.settings.token}",
                "Content-Type": "application/json",
            }
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        expected_statuses: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        expected = expected_statuses or [200]
        response = self._session.request(
            method,
            f"{self.settings.server}{path}",
            params=params,
            json=json_body,
            verify=self.settings.verify_ssl,
            timeout=30,
        )
        body = response.text or ""
        if response.status_code not in expected:
            raise ArgoCdHttpError(method, path, response.status_code, body.strip())
        if not body.strip():
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    def get_application(self, name: str, refresh: Optional[str] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if refresh:
            params["refresh"] = refresh
        return self._request("GET", f"/api/v1/applications/{name}", params=params)

    def create_application(self, application: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/api/v1/applications", json_body=application, expected_statuses=[200, 201])

    def update_application(self, application: Dict[str, Any]) -> Dict[str, Any]:
        name = str(((application.get("metadata") or {}).get("name")) or "").strip()
        if not name:
            raise ArgoCdClientError("Argo CD application metadata.name is required for update.")
        return self._request(
            "PUT",
            f"/api/v1/applications/{name}",
            json_body=application,
            expected_statuses=[200],
        )

    def upsert_application(self, application: Dict[str, Any]) -> Dict[str, Any]:
        name = str(((application.get("metadata") or {}).get("name")) or "").strip()
        if not name:
            raise ArgoCdClientError("Argo CD application metadata.name is required.")
        try:
            existing = self.get_application(name)
        except ArgoCdHttpError as exc:
            if exc.status_code != 404:
                raise
            return self.create_application(application)
        application = dict(application)
        application["metadata"] = _merged_application_metadata(existing, application)
        return self.update_application(application)

    def sync_application(self, name: str, *, prune: bool = False) -> Dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/applications/{name}/sync",
            json_body={"prune": prune, "dryRun": False},
            expected_statuses=[200],
        )

    def wait_for_application(self, name: str) -> Dict[str, Any]:
        deadline = time.time() + self.settings.sync_timeout_seconds
        while time.time() < deadline:
            app = self.get_application(name, refresh="normal")
            status = extract_demo_microsaas_application_status(app)
            if status["ready"]:
                return app
            if status.get("operation_phase") in {"Error", "Failed"}:
                raise ArgoCdClientError(
                    f"Argo CD application {name} failed: {status.get('detail')}"
                )
            time.sleep(self.settings.sync_poll_seconds)
        raise ArgoCdClientError(
            f"Timed out waiting for Argo CD application {name} to become healthy/synced."
        )

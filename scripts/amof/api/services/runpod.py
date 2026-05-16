"""Bounded Runpod Pod lifecycle provider for AMOF.

Owns the orchestrator-side boundary to Runpod: settings, profile loading,
REST client, AMOF tenancy markers, status projection, audit trail, and
operator-callable TTL enforcement.

Explicitly NOT a daemon: ``enforce_ttl`` is a function the operator (or a
future scheduler) calls. Expired pods continue accruing Runpod cost until
``enforce_ttl(dry_run=False)`` runs.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shlex
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from amof.api.command_builder import get_workspace_root
from amof.manifest import simple_parse_yaml

logger = logging.getLogger(__name__)


CLOUD_TYPES = ("SECURE", "COMMUNITY", "SERVERLESS")
TTL_MIN_MINUTES = 1
TTL_MAX_MINUTES = 1440  # 24h hard cap day 1.
NAME_PREFIX = "amof-"

# T5: indicative RunPod on-demand hourly rates (USD) by gpu_class label.
# These are aligned with the pricing snapshot in
# .amof-tmp/runpod-heavy-ai-lanes-minimax-m25.md and the contract in
# contracts/runpod-heavy-lane-profile.md. They are NOT a live price
# feed; operators keep them in sync and may override per-profile via
# ``hourly_cost_usd_cap``. Missing gpu_class means "unknown rate";
# preflight then refuses only when the profile explicitly sets a cap.
GPU_HOURLY_RATES_USD: Dict[str, float] = {
    "RTX_4090_24GB": 0.34,
    "A40_48GB": 0.35,
    "RTX_A6000_48GB": 0.33,
    "L40S_48GB": 0.79,
    "A100_PCIE_80GB": 1.19,
    "H100_PCIE_80GB": 1.99,
    "H100_80GB": 1.99,
    "H100_NVL_94GB": 2.59,
    "RTX_PRO_6000_96GB": 1.69,
    "H200_141GB": 3.59,
    "B200_180GB": 5.98,
}

# Lane / routing additions (T2 profile catalog). Pod-creation fields above
# are unchanged; these are optional AMOF-side semantics layered on top of
# the same YAML so one file describes both the pod shape and how AMOF may
# route work to the pod.
PROFILE_KINDS = ("raw_workspace", "llm_openai_compatible", "media_worker", "serverless_openai_compatible")
TEARDOWN_POLICIES = ("stop_on_idle", "delete_on_idle", "manual")
ALLOWED_APPLICABILITIES = ("local", "cloud-dev", "both")


def _env_text(name: str, default: str = "") -> str:
    return str(os.environ.get(name) or default).strip()


def _env_float(name: str, default: float) -> float:
    raw = _env_text(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunpodSettings:
    api_key: str
    api_base: str
    http_timeout: float
    audit_path: Path
    profiles_dir: Path


def load_runpod_settings() -> Optional[RunpodSettings]:
    """Return Runpod settings, or ``None`` if ``RUNPOD_API_KEY`` is unset."""

    api_key = _env_text("RUNPOD_API_KEY")
    if not api_key:
        return None
    workspace_root = get_workspace_root()
    audit_default = workspace_root / ".amof" / "audit" / "runpod-pods.jsonl"
    profiles_default = workspace_root / ".amof" / "runpod-profiles"
    audit_path = Path(_env_text("AMOF_RUNPOD_AUDIT_PATH") or audit_default)
    profiles_dir = Path(_env_text("AMOF_RUNPOD_PROFILES_DIR") or profiles_default)
    return RunpodSettings(
        api_key=api_key,
        api_base=(_env_text("RUNPOD_API_BASE", "https://rest.runpod.io/v1")).rstrip("/"),
        http_timeout=max(1.0, _env_float("AMOF_RUNPOD_HTTP_TIMEOUT", 30.0)),
        audit_path=audit_path,
        profiles_dir=profiles_dir,
    )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RunpodClientError(RuntimeError):
    """Base error for the bounded Runpod provider."""


class RunpodNotConfigured(RunpodClientError):
    """Raised when ``RUNPOD_API_KEY`` is missing."""


class RunpodHttpError(RunpodClientError):
    """Raised when Runpod returns an unexpected HTTP response."""

    def __init__(self, method: str, path: str, status_code: int, body: str):
        self.method = method
        self.path = path
        self.status_code = status_code
        self.body = body
        super().__init__(f"Runpod {method} {path} failed with HTTP {status_code}: {body}")


class RunpodProfileError(RunpodClientError):
    """Raised when a profile YAML fails validation."""


class RunpodTtlExceeded(RunpodClientError):
    """Raised when a configured TTL exceeds the hard cap."""


class RunpodSiblingConflict(RunpodClientError):
    """Raised when an AMOF-managed pod of the same profile already exists.

    T3 guardrail: prevents accidental N-way billing when an operator
    clicks ``create`` twice or when the UI and CLI both submit.
    """


class RunpodBudgetCapExceeded(RunpodClientError):
    """Raised when a profile's projected hourly cost exceeds its cap.

    T5 guardrail: refuses pod creation before any billing starts.
    """


# ---------------------------------------------------------------------------
# Profile schema and loader
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunpodProfile:
    """Frozen, validated Runpod pod profile.

    MVP path: ``template_id`` is set; ``image_name`` / ``gpu_type_ids`` /
    ``ports`` are absent. The follow-up path (raw image + GPU strings) is
    accepted but emits a warning because the ``gpu_type_ids`` API-shape
    strings are not API-shape verified in MVP.
    """

    name: str
    template_id: Optional[str]
    gpu_count: int
    cloud_type: str
    container_disk_gb: int
    volume_gb: int
    network_volume_id: Optional[str]
    env: Dict[str, str]
    ttl_minutes: int
    owner: str
    purpose: str
    image_name: Optional[str] = None
    gpu_type_ids: Tuple[str, ...] = field(default_factory=tuple)
    ports: Tuple[str, ...] = field(default_factory=tuple)
    docker_args: Optional[str] = None
    # Lane / routing (T2). All optional; raw_workspace profiles leave
    # these empty. llm_openai_compatible profiles use them to describe
    # AMOF-side routing semantics.
    profile_kind: str = "raw_workspace"
    intended_roles: Tuple[str, ...] = field(default_factory=tuple)
    model: Optional[str] = None
    runtime: Optional[str] = None
    gpu_class: Optional[str] = None
    health_path: str = "/models"
    openai_base_url_template: Optional[str] = None
    max_context_tokens: Optional[int] = None
    idle_ttl_minutes: Optional[int] = None
    hard_timeout_seconds: Optional[int] = None
    max_cost_per_run_usd: Optional[float] = None
    hourly_cost_usd_cap: Optional[float] = None
    teardown_policy: str = "manual"
    applicability: Tuple[str, ...] = field(default_factory=lambda: ("local",))
    required_secrets: Tuple[str, ...] = field(default_factory=tuple)
    allow_master: bool = False
    allow_direct_git_write: bool = False


def _require(value: Any, field_name: str, profile_name: str) -> Any:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise RunpodProfileError(
            f"Profile '{profile_name}' is missing required field '{field_name}'."
        )
    return value


def _coerce_str_list(value: Any, field_name: str, profile_name: str) -> Tuple[str, ...]:
    if value is None:
        return tuple()
    if isinstance(value, str) and value.strip() in ("[]", ""):
        return tuple()
    if not isinstance(value, list):
        raise RunpodProfileError(
            f"Profile '{profile_name}' field '{field_name}' must be a list, got {type(value).__name__}."
        )
    items: List[str] = []
    for raw in value:
        text = str(raw or "").strip()
        if text:
            items.append(text)
    return tuple(items)


def _coerce_str_map(value: Any, field_name: str, profile_name: str) -> Dict[str, str]:
    if value is None:
        return {}
    if isinstance(value, str) and value.strip() in ("{}", ""):
        return {}
    if not isinstance(value, dict):
        raise RunpodProfileError(
            f"Profile '{profile_name}' field '{field_name}' must be a mapping, got {type(value).__name__}."
        )
    out: Dict[str, str] = {}
    for key, raw in value.items():
        out[str(key)] = str(raw if raw is not None else "")
    return out


def parse_profile(name: str, payload: Dict[str, Any]) -> RunpodProfile:
    """Validate a parsed profile mapping into a :class:`RunpodProfile`."""

    if not isinstance(payload, dict):
        raise RunpodProfileError(f"Profile '{name}' must be a YAML mapping at the top level.")

    # T11: read profile_kind early so we can relax pod-shape validation
    # for serverless profiles (which have no pod at all).
    probe_profile_kind = str(payload.get("profile_kind") or "raw_workspace").strip()
    is_serverless = probe_profile_kind == "serverless_openai_compatible"

    template_id_raw = payload.get("template_id")
    image_name_raw = payload.get("image_name")
    template_id = str(template_id_raw).strip() if template_id_raw not in (None, "") else None
    image_name = str(image_name_raw).strip() if image_name_raw not in (None, "") else None

    if template_id and image_name:
        raise RunpodProfileError(
            f"Profile '{name}' must set exactly one of 'template_id' / 'image_name', not both."
        )
    if not is_serverless and not template_id and not image_name:
        raise RunpodProfileError(
            f"Profile '{name}' must set exactly one of 'template_id' / 'image_name'."
        )
    if is_serverless and (template_id or image_name):
        raise RunpodProfileError(
            f"Profile '{name}' has profile_kind='serverless_openai_compatible'; "
            f"do not set 'template_id' or 'image_name' (serverless has no pod)."
        )

    gpu_type_ids = _coerce_str_list(payload.get("gpu_type_ids"), "gpu_type_ids", name)
    ports = _coerce_str_list(payload.get("ports"), "ports", name)

    if image_name and not gpu_type_ids:
        raise RunpodProfileError(
            f"Profile '{name}' uses raw 'image_name'; 'gpu_type_ids' must be a non-empty list."
        )
    if image_name and not ports:
        raise RunpodProfileError(
            f"Profile '{name}' uses raw 'image_name'; 'ports' must be a non-empty list."
        )
    if image_name:
        logger.warning(
            "runpod profile '%s' uses follow-up path (image_name + gpu_type_ids); "
            "API-shape strings are not API-shape verified in MVP.",
            name,
        )
    if template_id and (gpu_type_ids or ports):
        raise RunpodProfileError(
            f"Profile '{name}' uses 'template_id'; do not set 'gpu_type_ids' or 'ports' "
            f"(those live on the template upstream)."
        )

    docker_args_raw = payload.get("docker_args")
    docker_args: Optional[str] = None
    if docker_args_raw not in (None, ""):
        if not isinstance(docker_args_raw, str):
            raise RunpodProfileError(
                f"Profile '{name}' field 'docker_args' must be a string, "
                f"got {type(docker_args_raw).__name__}."
            )
        docker_args = docker_args_raw.strip() or None
    if template_id and docker_args:
        raise RunpodProfileError(
            f"Profile '{name}' uses 'template_id'; do not set 'docker_args' "
            f"(the template upstream owns the container command)."
        )

    gpu_count_raw = payload.get("gpu_count")
    if is_serverless:
        gpu_count = 0 if gpu_count_raw in (None, "") else gpu_count_raw
        if not isinstance(gpu_count, int) or gpu_count < 0:
            raise RunpodProfileError(
                f"Profile '{name}' field 'gpu_count' must be a non-negative integer."
            )
    else:
        gpu_count = _require(gpu_count_raw, "gpu_count", name)
        if not isinstance(gpu_count, int) or gpu_count < 1:
            raise RunpodProfileError(
                f"Profile '{name}' field 'gpu_count' must be an integer >= 1, got {gpu_count!r}."
            )

    cloud_type_raw = payload.get("cloud_type")
    if is_serverless:
        cloud_type = str(cloud_type_raw or "SERVERLESS").strip().upper() or "SERVERLESS"
    else:
        cloud_type = str(_require(cloud_type_raw, "cloud_type", name)).strip().upper()
    if cloud_type not in CLOUD_TYPES:
        raise RunpodProfileError(
            f"Profile '{name}' field 'cloud_type' must be one of {CLOUD_TYPES}, got {cloud_type!r}."
        )

    container_disk_gb_raw = payload.get("container_disk_gb")
    if is_serverless:
        container_disk_gb = 0 if container_disk_gb_raw in (None, "") else container_disk_gb_raw
        if not isinstance(container_disk_gb, int) or container_disk_gb < 0:
            raise RunpodProfileError(
                f"Profile '{name}' field 'container_disk_gb' must be a non-negative integer."
            )
    else:
        container_disk_gb = container_disk_gb_raw
        if not isinstance(container_disk_gb, int) or container_disk_gb < 1:
            raise RunpodProfileError(
                f"Profile '{name}' field 'container_disk_gb' must be an integer >= 1."
            )

    volume_gb = payload.get("volume_gb", 0)
    if not isinstance(volume_gb, int) or volume_gb < 0:
        raise RunpodProfileError(
            f"Profile '{name}' field 'volume_gb' must be an integer >= 0."
        )

    network_volume_id_raw = payload.get("network_volume_id")
    network_volume_id = (
        str(network_volume_id_raw).strip()
        if network_volume_id_raw not in (None, "")
        else None
    )

    ttl_minutes = payload.get("ttl_minutes")
    if not isinstance(ttl_minutes, int):
        raise RunpodProfileError(
            f"Profile '{name}' field 'ttl_minutes' must be an integer."
        )
    if ttl_minutes < TTL_MIN_MINUTES or ttl_minutes > TTL_MAX_MINUTES:
        raise RunpodProfileError(
            f"Profile '{name}' field 'ttl_minutes' must be in [{TTL_MIN_MINUTES}, {TTL_MAX_MINUTES}], "
            f"got {ttl_minutes}."
        )

    owner = str(_require(payload.get("owner"), "owner", name)).strip()
    purpose = str(_require(payload.get("purpose"), "purpose", name)).strip()

    profile_kind = str(payload.get("profile_kind") or "raw_workspace").strip()
    if profile_kind not in PROFILE_KINDS:
        raise RunpodProfileError(
            f"Profile '{name}' field 'profile_kind' must be one of {PROFILE_KINDS}, "
            f"got {profile_kind!r}."
        )
    teardown_policy = str(payload.get("teardown_policy") or "manual").strip()
    if teardown_policy not in TEARDOWN_POLICIES:
        raise RunpodProfileError(
            f"Profile '{name}' field 'teardown_policy' must be one of {TEARDOWN_POLICIES}, "
            f"got {teardown_policy!r}."
        )

    applicability_raw = payload.get("applicability")
    if applicability_raw in (None, ""):
        applicability: Tuple[str, ...] = ("local",)
    else:
        applicability = _coerce_str_list(applicability_raw, "applicability", name)
        for value in applicability:
            if value not in ALLOWED_APPLICABILITIES:
                raise RunpodProfileError(
                    f"Profile '{name}' field 'applicability' contains invalid value {value!r}; "
                    f"allowed: {ALLOWED_APPLICABILITIES}."
                )

    intended_roles = _coerce_str_list(payload.get("intended_roles"), "intended_roles", name)
    required_secrets = _coerce_str_list(payload.get("required_secrets"), "required_secrets", name)

    def _opt_str(field_name: str) -> Optional[str]:
        raw = payload.get(field_name)
        if raw in (None, ""):
            return None
        return str(raw).strip() or None

    def _opt_int(field_name: str) -> Optional[int]:
        raw = payload.get(field_name)
        if raw in (None, ""):
            return None
        if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:
            raise RunpodProfileError(
                f"Profile '{name}' field {field_name!r} must be a positive integer if set, got {raw!r}."
            )
        return int(raw)

    def _opt_float(field_name: str) -> Optional[float]:
        raw = payload.get(field_name)
        if raw in (None, ""):
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise RunpodProfileError(
                f"Profile '{name}' field {field_name!r} must be numeric if set, got {raw!r}."
            )
        if value < 0:
            raise RunpodProfileError(
                f"Profile '{name}' field {field_name!r} must be non-negative, got {value}."
            )
        return value

    def _opt_bool(field_name: str, default: bool = False) -> bool:
        raw = payload.get(field_name)
        if raw is None:
            return default
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("true", "1", "yes", "on"):
            return True
        if text in ("false", "0", "no", "off"):
            return False
        raise RunpodProfileError(
            f"Profile '{name}' field {field_name!r} must be boolean-like if set, got {raw!r}."
        )

    model = _opt_str("model")
    runtime = _opt_str("runtime")
    gpu_class = _opt_str("gpu_class")
    health_path = str(payload.get("health_path") or "/models").strip() or "/models"
    openai_base_url_template = _opt_str("openai_base_url_template")
    max_context_tokens = _opt_int("max_context_tokens")
    idle_ttl_minutes = _opt_int("idle_ttl_minutes")
    hard_timeout_seconds = _opt_int("hard_timeout_seconds")
    max_cost_per_run_usd = _opt_float("max_cost_per_run_usd")
    hourly_cost_usd_cap = _opt_float("hourly_cost_usd_cap")
    allow_master = _opt_bool("allow_master", default=False)
    allow_direct_git_write = _opt_bool("allow_direct_git_write", default=False)

    if profile_kind == "llm_openai_compatible" and not model:
        raise RunpodProfileError(
            f"Profile '{name}' with profile_kind='llm_openai_compatible' must set 'model'."
        )
    if profile_kind == "serverless_openai_compatible":
        if not model:
            raise RunpodProfileError(
                f"Profile '{name}' with profile_kind='serverless_openai_compatible' must set 'model'."
            )
        if not openai_base_url_template:
            raise RunpodProfileError(
                f"Profile '{name}' with profile_kind='serverless_openai_compatible' must set "
                f"'openai_base_url_template' (serverless has no pod; the URL is the identity)."
            )

    return RunpodProfile(
        name=name,
        template_id=template_id,
        image_name=image_name,
        gpu_type_ids=gpu_type_ids,
        gpu_count=int(gpu_count),
        cloud_type=cloud_type,
        ports=ports,
        env=_coerce_str_map(payload.get("env"), "env", name),
        container_disk_gb=int(container_disk_gb),
        volume_gb=int(volume_gb),
        network_volume_id=network_volume_id,
        ttl_minutes=int(ttl_minutes),
        owner=owner,
        purpose=purpose,
        docker_args=docker_args,
        profile_kind=profile_kind,
        intended_roles=intended_roles,
        model=model,
        runtime=runtime,
        gpu_class=gpu_class,
        health_path=health_path,
        openai_base_url_template=openai_base_url_template,
        max_context_tokens=max_context_tokens,
        idle_ttl_minutes=idle_ttl_minutes,
        hard_timeout_seconds=hard_timeout_seconds,
        max_cost_per_run_usd=max_cost_per_run_usd,
        hourly_cost_usd_cap=hourly_cost_usd_cap,
        teardown_policy=teardown_policy,
        applicability=applicability,
        required_secrets=required_secrets,
        allow_master=allow_master,
        allow_direct_git_write=allow_direct_git_write,
    )


def load_profile(name: str, settings: Optional[RunpodSettings] = None) -> RunpodProfile:
    """Load a profile YAML from the operator-managed runtime location."""

    config = settings or load_runpod_settings()
    if config is None:
        raise RunpodNotConfigured(
            "Runpod is not configured. Set RUNPOD_API_KEY before loading profiles."
        )
    safe_name = str(name or "").strip()
    if not safe_name or "/" in safe_name or safe_name.startswith("."):
        raise RunpodProfileError(f"Invalid profile name: {name!r}")
    profile_path = config.profiles_dir / f"{safe_name}.yaml"
    if not profile_path.exists():
        raise RunpodProfileError(
            f"Profile '{safe_name}' not found at {profile_path}. "
            f"Materialize one from repos/amof/docs/contracts/examples/runpod-profiles/."
        )
    text = profile_path.read_text(encoding="utf-8")
    try:
        payload = simple_parse_yaml(text)
    except Exception as exc:
        raise RunpodProfileError(f"Profile '{safe_name}' YAML parse failed: {exc}") from exc
    return parse_profile(safe_name, payload if isinstance(payload, dict) else {})


def list_profiles(settings: Optional[RunpodSettings] = None) -> List[Dict[str, Any]]:
    """List profile YAMLs in the runtime profiles dir.

    Each entry is a projection suitable for the operator UI (T8) and the
    ``GET /api/v1/runpod/profiles`` endpoint. Parse errors are surfaced
    per-row as ``{"name": ..., "error": "..."}``; the caller decides
    whether to hide broken profiles.
    """

    config = settings or load_runpod_settings()
    if config is None:
        raise RunpodNotConfigured(
            "Runpod is not configured. Set RUNPOD_API_KEY before listing profiles."
        )
    profiles_dir = config.profiles_dir
    if not profiles_dir.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for path in sorted(profiles_dir.glob("*.yaml")):
        name = path.stem
        try:
            profile = load_profile(name, config)
        except RunpodProfileError as exc:
            rows.append({"name": name, "error": str(exc)[:500]})
            continue
        rows.append(project_profile(profile))
    return rows


def collect_runpod_activity(
    *,
    since_iso: Optional[str] = None,
    until_iso: Optional[str] = None,
    audit_path: Optional[Path] = None,
    settings: Optional[RunpodSettings] = None,
    max_rows: int = 500,
) -> Dict[str, Any]:
    """Read the JSONL audit file and summarize RunPod activity in a window.

    T6 release-record feed: returns counts per op plus an array of the
    most recent lifecycle events. Windowing is best-effort: rows without
    a parseable ``ts_iso`` are included in counts but excluded from the
    window filter.

    Parameters
    ----------
    since_iso / until_iso
        ISO-8601 boundary timestamps. When both are None, all rows count.
    audit_path
        Override path; otherwise uses ``settings.audit_path`` or the
        default under the workspace ``.amof/audit/runpod-pods.jsonl``.
    max_rows
        Cap the returned detail array (never the counts).
    """

    path: Optional[Path] = audit_path
    if path is None:
        config = settings or load_runpod_settings()
        if config is None:
            return {
                "audit_path": None,
                "window": {"since_iso": since_iso, "until_iso": until_iso},
                "counts_by_op": {},
                "events": [],
                "note": "audit_not_available_runpod_not_configured",
            }
        path = config.audit_path
    if not path.exists():
        return {
            "audit_path": str(path),
            "window": {"since_iso": since_iso, "until_iso": until_iso},
            "counts_by_op": {},
            "events": [],
            "note": "audit_file_missing",
        }

    since_dt = _parse_iso(since_iso) if since_iso else None
    until_dt = _parse_iso(until_iso) if until_iso else None

    counts: Dict[str, int] = {}
    events: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        op = str(row.get("op") or "unknown")
        row_ts = _parse_iso(row.get("ts_iso"))
        in_window = True
        if since_dt and row_ts and row_ts < since_dt:
            in_window = False
        if until_dt and row_ts and row_ts > until_dt:
            in_window = False
        if in_window:
            counts[op] = counts.get(op, 0) + 1
            if len(events) < max_rows:
                events.append(
                    {
                        "ts_iso": row.get("ts_iso"),
                        "op": op,
                        "pod_id": row.get("pod_id"),
                        "profile": row.get("profile"),
                        "desired_status": row.get("desired_status"),
                        "hourly_cost_usd": row.get("hourly_cost_usd"),
                        "error": row.get("error"),
                    }
                )
    return {
        "audit_path": str(path),
        "window": {"since_iso": since_iso, "until_iso": until_iso},
        "counts_by_op": counts,
        "events": events[-max_rows:],
        "event_count_in_window": sum(counts.values()),
    }


def estimate_hourly_cost_usd(profile: RunpodProfile) -> Optional[float]:
    """Return the indicative per-hour cost for this profile, or None.

    Only returns a value when ``gpu_class`` maps into
    :data:`GPU_HOURLY_RATES_USD`. Keeps preflight honest: unknown
    gpu_class falls back to the operator-declared
    ``hourly_cost_usd_cap`` without inventing a rate.
    """

    if not profile.gpu_class:
        return None
    rate = GPU_HOURLY_RATES_USD.get(profile.gpu_class)
    if rate is None:
        return None
    return float(rate) * max(int(profile.gpu_count), 1)


def estimate_pod_uptime_cost_usd(
    status: Dict[str, Any], *, seconds_elapsed: Optional[float] = None
) -> Optional[float]:
    """Estimate uptime cost for an already-running pod from its status.

    Uses the ``hourly_cost_usd`` field that Runpod echoes back into pod
    status. ``seconds_elapsed`` is a caller-supplied override; if None,
    the caller can pass the elapsed time since pod creation.
    """

    hourly = status.get("hourly_cost_usd")
    if hourly is None or seconds_elapsed is None:
        return None
    try:
        return float(hourly) * (float(seconds_elapsed) / 3600.0)
    except (TypeError, ValueError):
        return None


def project_profile(profile: RunpodProfile) -> Dict[str, Any]:
    """Project a :class:`RunpodProfile` into a truthful dict view.

    Keeps pod-creation fields and lane-routing fields in one payload so
    the UI and agent_runner can read the same shape.
    """

    return {
        "name": profile.name,
        "profile_kind": profile.profile_kind,
        "template_id": profile.template_id,
        "image_name": profile.image_name,
        "gpu_type_ids": list(profile.gpu_type_ids),
        "gpu_count": profile.gpu_count,
        "gpu_class": profile.gpu_class,
        "ports": list(profile.ports),
        "cloud_type": profile.cloud_type,
        "container_disk_gb": profile.container_disk_gb,
        "volume_gb": profile.volume_gb,
        "docker_args": profile.docker_args,
        "ttl_minutes": profile.ttl_minutes,
        "idle_ttl_minutes": profile.idle_ttl_minutes,
        "hard_timeout_seconds": profile.hard_timeout_seconds,
        "teardown_policy": profile.teardown_policy,
        "intended_roles": list(profile.intended_roles),
        "model": profile.model,
        "runtime": profile.runtime,
        "health_path": profile.health_path,
        "openai_base_url_template": profile.openai_base_url_template,
        "max_context_tokens": profile.max_context_tokens,
        "max_cost_per_run_usd": profile.max_cost_per_run_usd,
        "hourly_cost_usd_cap": profile.hourly_cost_usd_cap,
        "applicability": list(profile.applicability),
        "required_secrets": list(profile.required_secrets),
        "allow_master": profile.allow_master,
        "allow_direct_git_write": profile.allow_direct_git_write,
        "owner": profile.owner,
        "purpose": profile.purpose,
        "estimated_hourly_cost_usd": estimate_hourly_cost_usd(profile),
    }


# ---------------------------------------------------------------------------
# AMOF tenancy markers
# ---------------------------------------------------------------------------


def _amof_marker_env(profile: RunpodProfile, *, deadline: datetime, created: datetime) -> Dict[str, str]:
    """Return the full AMOF marker env block injected into every created pod."""

    return {
        "AMOF_MANAGED": "true",
        "AMOF_PROVIDER": "runpod",
        "AMOF_PROFILE": profile.name,
        "AMOF_OWNER": profile.owner,
        "AMOF_PURPOSE": profile.purpose,
        "AMOF_TTL_MINUTES": str(profile.ttl_minutes),
        "AMOF_TTL_DEADLINE_ISO": deadline.isoformat(),
        "AMOF_CREATED_AT_ISO": created.isoformat(),
    }


def _is_amof_managed(pod: Dict[str, Any]) -> bool:
    env = pod.get("env") or {}
    if not isinstance(env, dict):
        return False
    return (
        str(env.get("AMOF_MANAGED") or "").strip().lower() == "true"
        and str(env.get("AMOF_PROVIDER") or "").strip().lower() == "runpod"
    )


# ---------------------------------------------------------------------------
# Status projection
# ---------------------------------------------------------------------------


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def _parse_cost(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _build_port_mappings(pod: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Join ``portMappings`` (object map) with ``ports`` (array) for protocol."""

    raw = pod.get("portMappings")
    if not isinstance(raw, dict):
        return []
    protocol_by_port: Dict[int, str] = {}
    for spec in pod.get("ports") or []:
        text = str(spec or "").strip()
        if "/" not in text:
            continue
        port_str, protocol = text.split("/", 1)
        try:
            protocol_by_port[int(port_str)] = protocol.strip().lower()
        except ValueError:
            continue
    out: List[Dict[str, Any]] = []
    for private_str, public_value in raw.items():
        try:
            private_port = int(private_str)
        except (TypeError, ValueError):
            continue
        try:
            public_port = int(public_value) if public_value is not None else None
        except (TypeError, ValueError):
            public_port = None
        out.append(
            {
                "private": private_port,
                "public": public_port,
                "protocol": protocol_by_port.get(private_port),
            }
        )
    return out


def _resolve_http_private_port(
    *,
    port_mappings: List[Dict[str, Any]],
    pod_ports: Tuple[str, ...],
    profile_ports: Tuple[str, ...],
) -> Optional[int]:
    """Best-effort resolve the pod's private HTTP port.

    Prefer materialized ``portMappings`` from RunPod when available. When the
    proxy endpoint is already live but RunPod still leaves ``portMappings``
    empty, fall back to the declared ``ports`` contract from the pod/profile
    itself so callers can still construct the deterministic
    ``https://<pod>-<port>.proxy.runpod.net/v1`` URL.
    """

    for row in port_mappings:
        if str(row.get("protocol") or "").lower() == "http":
            private = row.get("private")
            if isinstance(private, int):
                return private

    for spec in list(pod_ports) + list(profile_ports):
        text = str(spec or "").strip()
        if "/" not in text:
            continue
        port_str, protocol = text.split("/", 1)
        if protocol.strip().lower() != "http":
            continue
        try:
            return int(port_str)
        except ValueError:
            continue
    return None


def project_pod_status(pod: Dict[str, Any], *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Project a Runpod pod object into AMOF's truthful status surface.

    Never invents fields. Missing upstream data becomes ``null``.
    """

    if not isinstance(pod, dict):
        raise RunpodClientError(f"Cannot project pod status: expected dict, got {type(pod).__name__}.")

    env = pod.get("env") or {}
    if not isinstance(env, dict):
        env = {}

    deadline = _parse_iso(env.get("AMOF_TTL_DEADLINE_ISO"))
    ttl_remaining: Optional[int] = None
    if deadline is not None:
        delta = deadline - (now or _now())
        ttl_remaining = int(delta.total_seconds())

    return {
        "pod_id": pod.get("id"),
        "name": pod.get("name"),
        "desired_status": pod.get("desiredStatus"),
        "public_ip": pod.get("publicIp"),
        "port_mappings": _build_port_mappings(pod),
        "hourly_cost_usd": _parse_cost(pod.get("costPerHr")),
        "amof_managed": str(env.get("AMOF_MANAGED") or "").strip().lower() == "true",
        "amof_provider": env.get("AMOF_PROVIDER"),
        "amof_profile": env.get("AMOF_PROFILE"),
        "owner": env.get("AMOF_OWNER"),
        "purpose": env.get("AMOF_PURPOSE"),
        "ttl_deadline_iso": env.get("AMOF_TTL_DEADLINE_ISO"),
        "ttl_remaining_seconds": ttl_remaining,
    }


# ---------------------------------------------------------------------------
# REST client + lifecycle
# ---------------------------------------------------------------------------


class RunpodClient:
    """Tiny REST client for the bounded slice of Runpod AMOF needs."""

    def __init__(self, settings: Optional[RunpodSettings] = None):
        self.settings = settings or load_runpod_settings()
        if self.settings is None:
            raise RunpodNotConfigured(
                "Runpod is not configured. Set RUNPOD_API_KEY (and optionally RUNPOD_API_BASE)."
            )
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {self.settings.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    # ---- internal HTTP ----

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        expected_statuses: Optional[List[int]] = None,
    ) -> Any:
        expected = expected_statuses or [200]
        response = self._session.request(
            method,
            f"{self.settings.api_base}{path}",
            params=params,
            json=json_body,
            timeout=self.settings.http_timeout,
        )
        body = response.text or ""
        if response.status_code not in expected:
            raise RunpodHttpError(method, path, response.status_code, body.strip())
        if not body.strip():
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    # ---- audit ----

    def _audit(
        self,
        op: str,
        *,
        pod_id: Optional[str] = None,
        profile: Optional[RunpodProfile] = None,
        status: Optional[Dict[str, Any]] = None,
        error: Optional[BaseException] = None,
    ) -> None:
        record: Dict[str, Any] = {
            "ts_iso": _now_iso(),
            "op": op,
            "pod_id": pod_id,
            "profile": profile.name if profile else None,
            "owner": profile.owner if profile else (status or {}).get("owner"),
            "purpose": profile.purpose if profile else (status or {}).get("purpose"),
        }
        if status:
            record["desired_status"] = status.get("desired_status")
            record["hourly_cost_usd"] = status.get("hourly_cost_usd")
            record["ttl_remaining_seconds"] = status.get("ttl_remaining_seconds")
        if error is not None:
            record["error"] = error.__class__.__name__
            record["error_message"] = str(error)[:500]
            if isinstance(error, RunpodHttpError):
                record["error_status_code"] = error.status_code
        try:
            self.settings.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.settings.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("runpod audit write failed at %s: %s", self.settings.audit_path, exc)

    # ---- body builder ----

    def _build_create_body(self, profile: RunpodProfile, *, deadline: datetime, created: datetime) -> Dict[str, Any]:
        marker_env = _amof_marker_env(profile, deadline=deadline, created=created)
        merged_env = dict(profile.env or {})
        merged_env.update(marker_env)
        body: Dict[str, Any] = {
            "name": f"{NAME_PREFIX}{profile.name}-{secrets.token_hex(4)}",
            "cloudType": profile.cloud_type,
            "gpuCount": profile.gpu_count,
            "containerDiskInGb": profile.container_disk_gb,
            "volumeInGb": profile.volume_gb,
            "env": merged_env,
        }
        if profile.template_id:
            body["templateId"] = profile.template_id
        else:
            body["imageName"] = profile.image_name
            body["gpuTypeIds"] = list(profile.gpu_type_ids)
            body["ports"] = list(profile.ports)
            if profile.docker_args:
                # RunPod REST v1 /pods accepts ``dockerStartCmd`` as a list of
                # strings (override CMD). The human-friendly profile field
                # ``docker_args`` is a single shell-quoted string; split with
                # shlex so the API contract stays array-shaped.
                body["dockerStartCmd"] = shlex.split(profile.docker_args)
        if profile.network_volume_id:
            body["networkVolumeId"] = profile.network_volume_id
        return body

    # ---- lifecycle ----

    def create_pod(self, profile: RunpodProfile) -> Dict[str, Any]:
        if profile.profile_kind == "serverless_openai_compatible":
            raise RunpodProfileError(
                f"Profile '{profile.name}' is serverless; it has no pod. "
                f"Configure RUNPOD_OPENAI_BASE_URL and use the endpoint directly."
            )
        if profile.ttl_minutes > TTL_MAX_MINUTES:
            raise RunpodTtlExceeded(
                f"Profile '{profile.name}' ttl_minutes={profile.ttl_minutes} exceeds hard cap {TTL_MAX_MINUTES}."
            )
        # T5 preflight: refuse if profile-declared hourly cap is exceeded
        # by the known gpu_class rate.
        estimated_hourly = estimate_hourly_cost_usd(profile)
        if (
            profile.hourly_cost_usd_cap is not None
            and estimated_hourly is not None
            and estimated_hourly > float(profile.hourly_cost_usd_cap)
        ):
            raise RunpodBudgetCapExceeded(
                f"Profile '{profile.name}' estimated hourly cost "
                f"${estimated_hourly:.2f} exceeds hourly_cost_usd_cap "
                f"${float(profile.hourly_cost_usd_cap):.2f} "
                f"(gpu_class={profile.gpu_class}, gpu_count={profile.gpu_count})."
            )
        try:
            siblings = self.list_amof_pods()
        except RunpodClientError:
            siblings = []
        live_states = {"RUNNING", "STARTING", "RESUMING", "PENDING"}
        for existing in siblings:
            env = existing.get("env") or {}
            if not isinstance(env, dict):
                continue
            if str(env.get("AMOF_PROFILE") or "").strip() != profile.name:
                continue
            state = str(existing.get("desiredStatus") or "").upper()
            if state in live_states:
                raise RunpodSiblingConflict(
                    f"An AMOF-managed pod for profile '{profile.name}' already "
                    f"exists (pod_id={existing.get('id')}, desired_status={state}). "
                    f"Stop or delete it before creating another."
                )
        created = _now()
        deadline = created + timedelta(minutes=profile.ttl_minutes)
        body = self._build_create_body(profile, deadline=deadline, created=created)
        try:
            response = self._request(
                "POST",
                "/pods",
                json_body=body,
                expected_statuses=[200, 201],
            )
        except BaseException as exc:
            self._audit("create", profile=profile, error=exc)
            raise
        pod = response if isinstance(response, dict) else {}
        status = project_pod_status(pod)
        self._audit("create", pod_id=status.get("pod_id"), profile=profile, status=status)
        return pod

    def get_pod(self, pod_id: str) -> Dict[str, Any]:
        try:
            response = self._request("GET", f"/pods/{pod_id}")
        except BaseException as exc:
            self._audit("get", pod_id=pod_id, error=exc)
            raise
        pod = response if isinstance(response, dict) else {}
        self._audit("get", pod_id=pod_id, status=project_pod_status(pod))
        return pod

    def list_pods(self, *, name_prefix: Optional[str] = None) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {}
        if name_prefix:
            params["name"] = name_prefix
        try:
            response = self._request("GET", "/pods", params=params or None)
        except BaseException as exc:
            self._audit("list", error=exc)
            raise
        if isinstance(response, list):
            return [item for item in response if isinstance(item, dict)]
        return []

    def list_amof_pods(self) -> List[Dict[str, Any]]:
        """List pods filtered to the AMOF tenancy marker conjunction.

        The authoritative filter is the client-side conjunction
        ``env["AMOF_MANAGED"] == "true" AND env["AMOF_PROVIDER"] == "runpod"``.

        Note (verified 2026-04-21 against rest.runpod.io/v1): the upstream
        ``GET /pods?name=<value>`` query is NOT a prefix filter (it returns
        an empty list for ``name=amof-``). We therefore do not pass any
        server-side narrowing hint and filter purely client-side. ``GET
        /pods`` (no params) DOES include the full ``env`` object on each
        pod, so the conjunction can be evaluated without an extra GET.
        """

        candidates = self.list_pods()
        return [pod for pod in candidates if _is_amof_managed(pod)]

    def start_pod(self, pod_id: str) -> Dict[str, Any]:
        try:
            response = self._request(
                "POST",
                f"/pods/{pod_id}/start",
                json_body={},
                expected_statuses=[200],
            )
        except BaseException as exc:
            self._audit("start", pod_id=pod_id, error=exc)
            raise
        pod = response if isinstance(response, dict) else {}
        self._audit("start", pod_id=pod_id, status=project_pod_status(pod))
        return pod

    def stop_pod(self, pod_id: str) -> Dict[str, Any]:
        try:
            response = self._request(
                "POST",
                f"/pods/{pod_id}/stop",
                json_body={},
                expected_statuses=[200],
            )
        except BaseException as exc:
            self._audit("stop", pod_id=pod_id, error=exc)
            raise
        pod = response if isinstance(response, dict) else {}
        self._audit("stop", pod_id=pod_id, status=project_pod_status(pod))
        return pod

    def delete_pod(self, pod_id: str) -> None:
        try:
            self._request(
                "DELETE",
                f"/pods/{pod_id}",
                expected_statuses=[200, 202, 204],
            )
        except BaseException as exc:
            self._audit("delete", pod_id=pod_id, error=exc)
            raise
        self._audit("delete", pod_id=pod_id)

    # ---- Endpoint resolution + per-pod health (T3) ----

    def resolve_endpoint(
        self, pod_id: str, profile: RunpodProfile
    ) -> Dict[str, Any]:
        """Resolve the OpenAI-compatible endpoint for one AMOF-managed pod.

        Returns a dict with ``openai_base_url`` (or ``None`` if not
        resolvable yet), ``expected_model``, ``expected_http_port``, and
        the current pod desired status. Never mutates the pod.
        """

        pod = self.get_pod(pod_id)
        status = project_pod_status(pod)
        http_port = _resolve_http_private_port(
            port_mappings=list(status.get("port_mappings") or []),
            pod_ports=tuple(pod.get("ports") or ()),
            profile_ports=profile.ports,
        )
        openai_base_url: Optional[str] = None
        template = profile.openai_base_url_template
        if template and http_port is not None:
            openai_base_url = (
                template.replace("{pod_id}", str(status.get("pod_id") or pod_id))
                .replace("{port}", str(http_port))
            )
        elif http_port is not None and pod_id:
            openai_base_url = (
                f"https://{pod_id}-{http_port}.proxy.runpod.net/v1"
            )
        return {
            "pod_id": status.get("pod_id") or pod_id,
            "desired_status": status.get("desired_status"),
            "expected_model": profile.model,
            "expected_http_port": http_port,
            "openai_base_url": openai_base_url,
            "health_path": profile.health_path or "/models",
            "port_mappings": status.get("port_mappings"),
        }

    def health_check_endpoint(
        self,
        pod_id: str,
        profile: RunpodProfile,
        *,
        timeout_seconds: int = 10,
    ) -> Dict[str, Any]:
        """Probe the per-pod OpenAI-compatible endpoint.

        Returns the same truthful shape as
        ``evaluate_heavy_lane_status`` but scoped to one pod. Does not
        mutate the pod or re-check account-wide RunPod state.
        """

        import time as _time

        resolved = self.resolve_endpoint(pod_id, profile)
        base_url = resolved.get("openai_base_url")
        expected_model = resolved.get("expected_model")
        missing: List[str] = []
        reasons: List[str] = []
        failure_class: Optional[str] = None
        model_count: Optional[int] = None
        latency_ms: Optional[int] = None
        served_ids: List[str] = []

        if not base_url:
            missing.append("pod_http_port_mapping")
        if not expected_model and profile.profile_kind == "llm_openai_compatible":
            missing.append("profile_model")

        if base_url:
            started = _time.monotonic()
            try:
                response = requests.get(
                    f"{base_url.rstrip('/')}{profile.health_path or '/models'}",
                    headers={
                        "Authorization": f"Bearer {self.settings.api_key}",
                        "Accept": "application/json",
                    },
                    timeout=timeout_seconds,
                )
                latency_ms = int((_time.monotonic() - started) * 1000)
                if response.status_code in (401, 403):
                    failure_class = "provider_unauthorized"
                elif response.status_code == 404:
                    failure_class = "provider_model_endpoint_not_found"
                elif response.status_code == 429:
                    failure_class = "provider_rate_limited"
                elif response.status_code >= 500:
                    failure_class = "provider_unreachable"
                elif response.ok:
                    try:
                        payload = response.json()
                    except ValueError:
                        payload = {}
                    data = payload.get("data") if isinstance(payload, dict) else None
                    if isinstance(data, list):
                        model_count = len(data)
                        served_ids = [
                            str(row.get("id") or "")
                            for row in data
                            if isinstance(row, dict)
                        ]
                        if expected_model and expected_model in served_ids:
                            reasons.append("model_listed")
                        elif expected_model:
                            missing.append("expected_model_listed")
                            failure_class = "provider_model_not_listed"
                    else:
                        failure_class = "provider_invalid_response"
                else:
                    failure_class = "provider_unreachable"
            except requests.Timeout:
                failure_class = "provider_timeout"
            except requests.RequestException:
                failure_class = "provider_unreachable"

        usable = (
            not missing
            and failure_class is None
            and model_count is not None
            and (not expected_model or expected_model in served_ids)
        )
        result = {
            "pod_id": resolved.get("pod_id"),
            "status": "usable" if usable else "unusable",
            "usable": usable,
            "profile": profile.name,
            "expected_model": expected_model,
            "resolved_endpoint": resolved,
            "model_count": model_count,
            "served_ids": served_ids[:10],
            "latency_ms": latency_ms,
            "failure_class": failure_class,
            "missing_prerequisites": sorted(set(missing)),
            "reasons": sorted(set(reasons)),
        }
        self._audit(
            "health_check",
            pod_id=resolved.get("pod_id"),
            profile=profile,
            status={
                "desired_status": resolved.get("desired_status"),
                "hourly_cost_usd": None,
                "ttl_remaining_seconds": None,
                "owner": profile.owner,
                "purpose": profile.purpose,
            },
        )
        return result

    def mark_usable(
        self, pod_id: str, *, usable: bool, reason: str
    ) -> Dict[str, Any]:
        """Record an operator-visible usable/unusable projection.

        This is an audit-only write; the flag does not mutate the pod.
        Agent routing (T7) consults recent audit or re-runs
        ``health_check_endpoint`` before committing a request.
        """

        record: Dict[str, Any] = {
            "op": "mark_usable",
            "pod_id": pod_id,
            "usable": bool(usable),
            "reason": str(reason or "")[:300],
        }
        try:
            pod = self.get_pod(pod_id)
            status = project_pod_status(pod)
            record["desired_status"] = status.get("desired_status")
            record["amof_profile"] = status.get("amof_profile")
        except RunpodClientError as exc:
            record["error"] = str(exc)[:300]
        self._audit(
            "mark_usable",
            pod_id=pod_id,
            status={
                "desired_status": record.get("desired_status"),
                "hourly_cost_usd": None,
                "ttl_remaining_seconds": None,
                "owner": None,
                "purpose": record["reason"],
            },
        )
        return record

    # ---- Garbage collection (T4) ----

    ZOMBIE_STATES = ("EXITED", "DEAD", "TERMINATED", "FAILED")

    def garbage_collect(
        self,
        *,
        dry_run: bool = True,
        now: Optional[datetime] = None,
        overshoot_seconds: int = 3600,
    ) -> List[Dict[str, Any]]:
        """Reap AMOF-managed zombies.

        A pod is considered a zombie when:

        - its ``desiredStatus`` is in :data:`ZOMBIE_STATES`, OR
        - it is past its TTL deadline by more than ``overshoot_seconds``.

        The method is complementary to :meth:`enforce_ttl`: ``enforce_ttl``
        catches pods *just after* deadline and stops+deletes them once;
        ``garbage_collect`` catches pods that for any reason slipped past
        the normal reaper (EXITED containers still billing, TTL far past,
        missing audit after a crash, etc.). Defaults to ``dry_run=True``
        so operators and CI can see what would be deleted without spending.
        """

        reference = now or _now()
        amof_pods = self.list_amof_pods()
        report: List[Dict[str, Any]] = []
        for pod in amof_pods:
            pod_id = pod.get("id")
            status = project_pod_status(pod, now=reference)
            state = str(status.get("desired_status") or "").upper()
            ttl_remaining = status.get("ttl_remaining_seconds")
            reason_bits: List[str] = []
            if state in self.ZOMBIE_STATES:
                reason_bits.append(f"terminal_state:{state}")
            if ttl_remaining is not None and ttl_remaining < -int(overshoot_seconds):
                reason_bits.append(f"ttl_overshot:{-int(ttl_remaining)}s")
            if not reason_bits:
                continue
            row: Dict[str, Any] = {
                "pod_id": pod_id,
                "owner": status.get("owner"),
                "profile": status.get("amof_profile"),
                "desired_status": state or None,
                "ttl_remaining_seconds": ttl_remaining,
                "reason": ",".join(reason_bits),
                "actions": [],
            }
            if dry_run:
                row["actions"] = ["report_only"]
                report.append(row)
                continue
            try:
                # Gracefully stop first if still live; ignore if not.
                if state in ("RUNNING", "STARTING", "RESUMING", "PENDING"):
                    self.stop_pod(pod_id)
                    row["actions"].append("stop")
                self.delete_pod(pod_id)
                row["actions"].append("delete")
            except RunpodHttpError as exc:
                row["error"] = f"HTTP {exc.status_code}: {exc.body[:200]}"
            except RunpodClientError as exc:
                row["error"] = str(exc)[:200]
            report.append(row)
        return report

    # ---- TTL ----

    def enforce_ttl(self, *, dry_run: bool = True, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Report (and optionally reap) AMOF-managed pods past their TTL deadline.

        This is **not** a daemon. Expired pods continue accruing Runpod cost
        until this method is called with ``dry_run=False``.
        """

        reference = now or _now()
        amof_pods = self.list_amof_pods()
        report: List[Dict[str, Any]] = []
        for pod in amof_pods:
            pod_id = pod.get("id")
            status = project_pod_status(pod, now=reference)
            ttl_remaining = status.get("ttl_remaining_seconds")
            if ttl_remaining is None or ttl_remaining > 0:
                continue
            row: Dict[str, Any] = {
                "pod_id": pod_id,
                "owner": status.get("owner"),
                "profile": status.get("amof_profile"),
                "ttl_remaining_seconds": ttl_remaining,
                "reason": "ttl_expired",
                "actions": [],
            }
            if dry_run:
                row["actions"] = ["report_only"]
                report.append(row)
                continue
            try:
                self.stop_pod(pod_id)
                row["actions"].append("stop")
                self.delete_pod(pod_id)
                row["actions"].append("delete")
            except RunpodHttpError as exc:
                row["error"] = f"HTTP {exc.status_code}: {exc.body[:200]}"
            except RunpodClientError as exc:
                row["error"] = str(exc)[:200]
            report.append(row)
        return report

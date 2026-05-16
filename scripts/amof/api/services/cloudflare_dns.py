from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

CF_API_BASE = "https://api.cloudflare.com/client/v4"
TOKEN_ENV_NAMES = (
    "CLOUDFLARE_API_TOKEN",
    "CLOUDFLARE_API_TOKEN_DNS",
    "CLOUDFLARE_API_TOKEN_GLOBAL",
)
AMOF_DEV_ZONE_NAME = "amof.dev"
AMOF_DEV_ZONE_ID_ENV = "CLOUDFLARE_ZONE_ID_AMOF_DEV"
AMOF_ENVIRONMENT_DNS_TARGET_ENV = "AMOF_ENVIRONMENT_DNS_TARGET_HOST"
DEFAULT_TARGET_HOST = "platform.amof.dev"
DEFAULT_PROXIED = True
DEFAULT_TTL = 1
DOTENV_CANDIDATES = (
    Path("/workspace/.env"),
    Path("/app/.env"),
    Path(".env"),
)
RESERVED_HOSTS = {
    "amof.dev",
    "www.amof.dev",
    "platform.amof.dev",
}


class CloudflareDnsError(RuntimeError):
    """Raised when lifecycle DNS automation cannot complete safely."""


def _load_dotenv_defaults(paths: Sequence[Path] = DOTENV_CANDIDATES) -> None:
    for path in paths:
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key or key in os.environ:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ[key] = value


def _pick_token() -> Tuple[str, str]:
    _load_dotenv_defaults()
    for name in TOKEN_ENV_NAMES:
        value = str(os.environ.get(name) or "").strip()
        if value:
            return name, value
    raise CloudflareDnsError(
        "Missing Cloudflare token. Set CLOUDFLARE_API_TOKEN, "
        "CLOUDFLARE_API_TOKEN_DNS, or CLOUDFLARE_API_TOKEN_GLOBAL."
    )


def _cf_request(
    token: str,
    path: str,
    query: Optional[Dict[str, Any]] = None,
    *,
    method: str = "GET",
    body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    url = f"{CF_API_BASE}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        detail = exc.read().decode("utf-8", errors="replace")
        raise CloudflareDnsError(f"Cloudflare API HTTP {exc.code} for {path}: {detail}") from exc
    except urllib.error.URLError as exc:  # type: ignore[attr-defined]
        raise CloudflareDnsError(f"Cloudflare API request failed for {path}: {exc.reason}") from exc
    if not payload.get("success"):
        raise CloudflareDnsError(f"Cloudflare API request failed for {path}: {payload.get('errors')}")
    return payload


def _resolve_zone_id(token: str) -> str:
    env_zone_id = str(os.environ.get(AMOF_DEV_ZONE_ID_ENV) or "").strip()
    if env_zone_id:
        return env_zone_id
    payload = _cf_request(token, "/zones", {"name": AMOF_DEV_ZONE_NAME})
    result = payload.get("result") or []
    if not result:
        raise CloudflareDnsError(f"Cloudflare zone lookup returned no zone for {AMOF_DEV_ZONE_NAME}.")
    return str(result[0]["id"])


def _normalize_host(host: Any) -> Optional[str]:
    value = str(host or "").strip().lower().rstrip(".")
    return value or None


def _normalized_host_from_url(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    candidate = text if "://" in text else f"https://{text}"
    parsed = urlparse(candidate)
    return _normalize_host(parsed.hostname)


def _managed_first_level_host(host: Any) -> Optional[str]:
    normalized = _normalize_host(host)
    if not normalized or normalized in RESERVED_HOSTS:
        return None
    suffix = f".{AMOF_DEV_ZONE_NAME}"
    if not normalized.endswith(suffix):
        return None
    label = normalized[: -len(suffix)]
    if not label or "." in label:
        return None
    return normalized


def extract_managed_amof_hosts(
    *,
    hostname: Any = None,
    public_base_url: Any = None,
    endpoints: Any = None,
) -> List[str]:
    ordered: List[str] = []
    seen: set[str] = set()

    def remember(candidate: Any) -> None:
        host = _managed_first_level_host(candidate)
        if host and host not in seen:
            seen.add(host)
            ordered.append(host)

    remember(hostname)
    remember(_normalized_host_from_url(public_base_url))
    if isinstance(endpoints, list):
        for endpoint in endpoints:
            if isinstance(endpoint, dict):
                remember(_normalized_host_from_url(endpoint.get("url")))
    return ordered


def list_zone_records(zone_id: str, token: str) -> List[Dict[str, Any]]:
    page = 1
    results: List[Dict[str, Any]] = []
    while True:
        payload = _cf_request(token, f"/zones/{zone_id}/dns_records", {"page": page, "per_page": 100})
        results.extend(payload.get("result") or [])
        info = payload.get("result_info") or {}
        total_pages = int(info.get("total_pages") or 1)
        if page >= total_pages:
            return results
        page += 1


def _dns_record_by_name(records: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    by_name: Dict[str, List[Dict[str, Any]]] = {}
    for record in records:
        host = _normalize_host(record.get("name"))
        if host:
            by_name.setdefault(host, []).append(record)
    return by_name


def _target_host() -> str:
    return _normalize_host(os.environ.get(AMOF_ENVIRONMENT_DNS_TARGET_ENV) or DEFAULT_TARGET_HOST) or DEFAULT_TARGET_HOST


def upsert_first_level_amof_dns(hosts: Sequence[str]) -> List[str]:
    desired_hosts = extract_managed_amof_hosts(endpoints=[{"url": f"https://{host}"} for host in hosts])
    if not desired_hosts:
        return []
    token_env, token = _pick_token()
    zone_id = _resolve_zone_id(token)
    current_by_name = _dns_record_by_name(list_zone_records(zone_id, token))
    target = _target_host()
    applied: List[str] = [f"token={token_env}", f"zone={zone_id}"]

    for host in desired_hosts:
        existing = current_by_name.get(host, [])
        if len(existing) > 1:
            raise CloudflareDnsError(f"Refusing to manage duplicate Cloudflare records for {host}.")
        payload = {
            "type": "CNAME",
            "name": host,
            "content": target,
            "proxied": DEFAULT_PROXIED,
            "ttl": DEFAULT_TTL,
            "comment": "Managed by the AMOF environment lifecycle contract.",
        }
        if not existing:
            _cf_request(token, f"/zones/{zone_id}/dns_records", method="POST", body=payload)
            applied.append(f"created {host} -> {target}")
            continue
        current = existing[0]
        current_type = str(current.get("type") or "").upper()
        current_content = _normalize_host(current.get("content"))
        current_proxied = bool(current.get("proxied"))
        current_ttl = int(current.get("ttl") or 1)
        if (
            current_type == "CNAME"
            and current_content == target
            and current_proxied == DEFAULT_PROXIED
            and current_ttl == DEFAULT_TTL
        ):
            applied.append(f"unchanged {host} -> {target}")
            continue
        record_id = str(current.get("id") or "").strip()
        if not record_id:
            raise CloudflareDnsError(f"Cloudflare record id missing for {host}.")
        _cf_request(token, f"/zones/{zone_id}/dns_records/{record_id}", method="PATCH", body=payload)
        applied.append(f"updated {host} -> {target}")
    return applied


def delete_first_level_amof_dns(hosts: Sequence[str]) -> List[str]:
    desired_hosts = extract_managed_amof_hosts(endpoints=[{"url": f"https://{host}"} for host in hosts])
    if not desired_hosts:
        return []
    token_env, token = _pick_token()
    zone_id = _resolve_zone_id(token)
    current_by_name = _dns_record_by_name(list_zone_records(zone_id, token))
    applied: List[str] = [f"token={token_env}", f"zone={zone_id}"]

    for host in desired_hosts:
        existing = current_by_name.get(host, [])
        if len(existing) > 1:
            raise CloudflareDnsError(f"Refusing to delete duplicate Cloudflare records for {host}.")
        if not existing:
            applied.append(f"missing {host}")
            continue
        record_id = str(existing[0].get("id") or "").strip()
        if not record_id:
            raise CloudflareDnsError(f"Cloudflare record id missing for {host}.")
        _cf_request(token, f"/zones/{zone_id}/dns_records/{record_id}", method="DELETE")
        applied.append(f"deleted {host}")
    return applied

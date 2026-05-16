#!/usr/bin/env python3
"""Smoke-test the first AMOF -> OpenSandbox boundary."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Dict, Iterable, Optional

import requests

DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_POLL_TIMEOUT_SECONDS = 120
DEFAULT_EXECD_PORT = 44772


def log(message: str) -> None:
    print(message, flush=True)


def fail(message: str) -> None:
    raise SystemExit(message)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke-test the first AMOF OpenSandbox boundary.")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("AMOF_OPENSANDBOX_BASE_URL", ""),
        help="OpenSandbox server root URL, with or without /v1.",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("AMOF_OPENSANDBOX_API_KEY", ""),
        help="Lifecycle API key for OPEN-SANDBOX-API-KEY.",
    )
    parser.add_argument(
        "--execd-token",
        default=os.environ.get("AMOF_OPENSANDBOX_EXECD_TOKEN", ""),
        help="Optional execd token for X-EXECD-ACCESS-TOKEN.",
    )
    parser.add_argument(
        "--image-uri",
        default=os.environ.get("AMOF_OPENSANDBOX_IMAGE_URI", "python:3.11-slim"),
        help="Sandbox image to launch for the smoke test.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=int(os.environ.get("AMOF_OPENSANDBOX_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)),
        help="Sandbox TTL in seconds.",
    )
    parser.add_argument(
        "--poll-timeout-seconds",
        type=int,
        default=DEFAULT_POLL_TIMEOUT_SECONDS,
        help="How long to wait for the sandbox to reach Running.",
    )
    parser.add_argument(
        "--skip-execd",
        action="store_true",
        help="Only verify lifecycle routes, not proxied execd ping/command.",
    )
    return parser.parse_args()


def normalize_base_urls(raw_base_url: str) -> tuple[str, str]:
    base_url = raw_base_url.strip().rstrip("/")
    if not base_url:
        fail("Missing OpenSandbox base URL. Use --base-url or AMOF_OPENSANDBOX_BASE_URL.")
    if base_url.endswith("/v1"):
        root_url = base_url[:-3]
        lifecycle_base = base_url
    else:
        root_url = base_url
        lifecycle_base = f"{base_url}/v1"
    return root_url.rstrip("/"), lifecycle_base.rstrip("/")


def json_request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    expected: Iterable[int],
    headers: Optional[Dict[str, str]] = None,
    **kwargs,
):
    response = session.request(method, url, headers=headers, timeout=30, **kwargs)
    if response.status_code not in expected:
        fail(f"{method} {url} failed with {response.status_code}: {response.text}")
    return response


def wait_for_running(
    session: requests.Session,
    lifecycle_base: str,
    sandbox_id: str,
    headers: Dict[str, str],
    timeout_seconds: int,
) -> Dict[str, object]:
    deadline = time.time() + timeout_seconds
    last_payload: Dict[str, object] = {}
    while time.time() < deadline:
        response = json_request(
            session,
            "GET",
            f"{lifecycle_base}/sandboxes/{sandbox_id}",
            expected=(200,),
            headers=headers,
        )
        payload = response.json()
        last_payload = payload
        state = str(((payload.get("status") or {}) if isinstance(payload, dict) else {}).get("state") or "")
        if state.lower() == "running":
            return payload
        if state.lower() in {"failed", "terminated"}:
            fail(f"Sandbox {sandbox_id} reached terminal state before Running: {json.dumps(payload)}")
        time.sleep(2)
    fail(f"Timed out waiting for sandbox {sandbox_id} to reach Running: {json.dumps(last_payload)}")


def stream_command_and_get_id(
    session: requests.Session,
    command_url: str,
    execd_headers: Dict[str, str],
) -> str:
    with session.post(
        command_url,
        json={"command": "echo amof-opensandbox-ok", "background": True},
        headers=execd_headers,
        timeout=30,
        stream=True,
    ) as response:
        if response.status_code != 200:
            fail(f"POST {command_url} failed with {response.status_code}: {response.text}")
        for raw_line in response.iter_lines():
            if not raw_line:
                continue
            line = raw_line.decode("utf-8", errors="replace")
            if line.startswith("data:"):
                line = line[5:].strip()
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("type") == "init" and payload.get("text"):
                return str(payload["text"])
    fail("Did not receive a command id from the proxied execd stream.")


def wait_for_command_completion(
    session: requests.Session,
    status_url: str,
    execd_headers: Dict[str, str],
) -> Dict[str, object]:
    deadline = time.time() + 30
    last_payload: Dict[str, object] = {}
    while time.time() < deadline:
        response = json_request(session, "GET", status_url, expected=(200,), headers=execd_headers)
        payload = response.json()
        last_payload = payload
        if not bool(payload.get("running", True)):
            return payload
        time.sleep(1)
    fail(f"Timed out waiting for command completion: {json.dumps(last_payload)}")


def main() -> None:
    args = parse_args()
    root_url, lifecycle_base = normalize_base_urls(args.base_url)

    session = requests.Session()
    lifecycle_headers: Dict[str, str] = {}
    if args.api_key.strip():
        lifecycle_headers["OPEN-SANDBOX-API-KEY"] = args.api_key.strip()

    execd_headers: Dict[str, str] = {}
    if args.execd_token.strip():
        execd_headers["X-EXECD-ACCESS-TOKEN"] = args.execd_token.strip()

    sandbox_id: Optional[str] = None
    try:
        log(f"[opensandbox-smoke] root={root_url}")
        json_request(session, "GET", f"{root_url}/ping", expected=(200,))
        log("[opensandbox-smoke] lifecycle ping ok")

        create_payload = {
            "image": {"uri": args.image_uri},
            "timeout": args.timeout_seconds,
            "resourceLimits": {"cpu": "500m", "memory": "512Mi"},
            "entrypoint": ["sh", "-lc", f"sleep {args.timeout_seconds}"],
            "metadata": {
                "source": "amof-opensandbox-smoke",
                "owner": "amof",
            },
        }
        create_response = json_request(
            session,
            "POST",
            f"{lifecycle_base}/sandboxes",
            expected=(200, 202),
            headers=lifecycle_headers,
            json=create_payload,
        )
        create_json = create_response.json()
        sandbox_id = str(create_json.get("id") or "").strip()
        if not sandbox_id:
            fail("Create sandbox response did not include an id.")
        log(f"[opensandbox-smoke] created sandbox={sandbox_id}")

        sandbox_json = wait_for_running(
            session,
            lifecycle_base,
            sandbox_id,
            lifecycle_headers,
            args.poll_timeout_seconds,
        )
        log(f"[opensandbox-smoke] sandbox running state={sandbox_json.get('status', {}).get('state')}")

        endpoint_response = json_request(
            session,
            "GET",
            f"{lifecycle_base}/sandboxes/{sandbox_id}/endpoints/{DEFAULT_EXECD_PORT}",
            expected=(200,),
            headers=lifecycle_headers,
            params={"use_server_proxy": "true"},
        )
        endpoint_json = endpoint_response.json()
        log(f"[opensandbox-smoke] execd endpoint={endpoint_json.get('endpoint')}")

        if args.skip_execd:
            log("[opensandbox-smoke] execd checks skipped by request")
            return

        proxy_base = f"{lifecycle_base}/sandboxes/{sandbox_id}/proxy/{DEFAULT_EXECD_PORT}"
        json_request(session, "GET", f"{proxy_base}/ping", expected=(200,), headers=execd_headers)
        log("[opensandbox-smoke] proxied execd ping ok")

        command_id = stream_command_and_get_id(session, f"{proxy_base}/command", execd_headers)
        log(f"[opensandbox-smoke] command id={command_id}")

        command_status = wait_for_command_completion(
            session,
            f"{proxy_base}/command/status/{command_id}",
            execd_headers,
        )
        log(f"[opensandbox-smoke] command status={json.dumps(command_status)}")

        logs_response = json_request(
            session,
            "GET",
            f"{proxy_base}/command/{command_id}/logs",
            expected=(200,),
            headers=execd_headers,
            params={"cursor": 0},
        )
        logs_text = logs_response.text
        if "amof-opensandbox-ok" not in logs_text:
            fail(f"Command logs did not contain sentinel text: {logs_text}")
        log("[opensandbox-smoke] proxied execd command ok")
        log("[opensandbox-smoke] PASS")
    finally:
        if sandbox_id:
            try:
                response = session.request(
                    "DELETE",
                    f"{lifecycle_base}/sandboxes/{sandbox_id}",
                    headers=lifecycle_headers,
                    timeout=30,
                )
                if response.status_code not in (200, 202, 204, 404):
                    log(
                        f"[opensandbox-smoke] cleanup warning: DELETE sandbox={sandbox_id} "
                        f"returned {response.status_code}: {response.text}"
                    )
                else:
                    log(f"[opensandbox-smoke] cleanup requested for sandbox={sandbox_id}")
            except Exception as exc:  # pragma: no cover - cleanup best effort
                log(f"[opensandbox-smoke] cleanup warning: {exc}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        if code not in (0, None) and str(exc):
            print(f"[opensandbox-smoke] FAIL: {exc}", file=sys.stderr)
        raise

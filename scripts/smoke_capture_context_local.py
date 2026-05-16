#!/usr/bin/env python3
"""Repeatable local API smoke test for the capture-context happy path."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.cookiejar import CookieJar
from typing import Any, Dict, Optional


DEFAULT_API_BASE = "http://127.0.0.1:18000/api/v1"
DEFAULT_ECOSYSTEM = "amof-platform"


class SmokeError(RuntimeError):
    """Raised when the smoke test hits a failed step."""


@dataclass
class SmokeContext:
    api_base: str
    ecosystem: str
    ticket_id: str
    ticket_repo: str
    timeout_seconds: int
    poll_seconds: float
    access_token: Optional[str]
    step_up_password: Optional[str]
    use_port_forward: bool
    port_forward_namespace: str
    port_forward_service: str
    port_forward_local_port: int


class PortForward:
    def __init__(self, ctx: SmokeContext) -> None:
        self.ctx = ctx
        self.proc: Optional[subprocess.Popen[str]] = None

    def __enter__(self) -> "PortForward":
        if not self.ctx.use_port_forward:
            return self
        command = [
            "kubectl",
            "-n",
            self.ctx.port_forward_namespace,
            "port-forward",
            f"service/{self.ctx.port_forward_service}",
            f"{self.ctx.port_forward_local_port}:8000",
        ]
        self.proc = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise SmokeError("kubectl port-forward exited before the local API became reachable")
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.5)
                if sock.connect_ex(("127.0.0.1", self.ctx.port_forward_local_port)) == 0:
                    print(
                        "[api] port-forward ready "
                        f"service/{self.ctx.port_forward_service} -> 127.0.0.1:{self.ctx.port_forward_local_port}"
                    )
                    return self
            time.sleep(0.25)
        raise SmokeError("kubectl port-forward did not become ready within 20s")

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)


class JsonClient:
    def __init__(self) -> None:
        jar = CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def request_json(
        self,
        method: str,
        url: str,
        *,
        body: Optional[Dict[str, Any]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if extra_headers:
            headers.update(extra_headers)
        request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with self._opener.open(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", errors="replace")
            raise SmokeError(f"{method} {url} failed with HTTP {exc.code}: {payload}") from exc
        except urllib.error.URLError as exc:
            raise SmokeError(f"{method} {url} failed: {exc.reason}") from exc
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise SmokeError(f"{method} {url} returned non-JSON payload: {raw[:400]}") from exc
        if not isinstance(parsed, dict):
            raise SmokeError(f"{method} {url} returned unexpected JSON type: {type(parsed).__name__}")
        return parsed


def _join(base: str, path: str) -> str:
    return base.rstrip("/") + path


def _poll_run(
    client: JsonClient,
    ctx: SmokeContext,
    run_id: str,
    *,
    step_name: str,
) -> Dict[str, Any]:
    deadline = time.time() + ctx.timeout_seconds
    url = _join(ctx.api_base, f"/runs/{urllib.parse.quote(run_id)}/status")
    last_status = None
    while time.time() < deadline:
        payload = client.request_json("GET", url)
        status = str(payload.get("status") or "")
        if status != last_status:
            print(f"[poll] {step_name}: {status}")
            last_status = status
        if payload.get("terminal"):
            if status != "success":
                raise SmokeError(
                    f"{step_name} run {run_id} ended with status={status} last_message={payload.get('last_message')!r}"
                )
            return payload
        time.sleep(ctx.poll_seconds)
    raise SmokeError(f"{step_name} run {run_id} did not finish within {ctx.timeout_seconds}s")


def _ensure_local_auth_path(client: JsonClient, ctx: SmokeContext) -> Dict[str, Any]:
    session = client.request_json("GET", _join(ctx.api_base, "/auth/session"))
    print(
        "[auth] "
        f"auth_enabled={session.get('auth_enabled')} authenticated={session.get('authenticated')} "
        f"step_up_active={(session.get('step_up') or {}).get('active')}"
    )
    if not session.get("auth_enabled"):
        return session
    if not ctx.access_token:
        raise SmokeError(
            "Local auth is enabled, but AMOF_ACCESS_TOKEN was not provided. "
            "Set AMOF_ACCESS_TOKEN and rerun."
        )
    session = client.request_json(
        "POST",
        _join(ctx.api_base, "/auth/session"),
        body={"access_token": ctx.access_token},
    )
    print(
        "[auth] session established "
        f"authenticated={session.get('authenticated')} "
        f"step_up_active={(session.get('step_up') or {}).get('active')}"
    )
    return session


def _ensure_step_up_if_needed(client: JsonClient, ctx: SmokeContext, session: Dict[str, Any]) -> Dict[str, Any]:
    if not session.get("auth_enabled"):
        return session
    if (session.get("step_up") or {}).get("active"):
        return session
    if not ctx.step_up_password:
        raise SmokeError(
            "Capture-context requires step-up when auth is enabled, but AMOF_STEP_UP_PASSWORD was not provided."
        )
    session = client.request_json(
        "POST",
        _join(ctx.api_base, "/auth/reauth"),
        body={"password": ctx.step_up_password},
    )
    print(
        "[auth] step-up granted "
        f"step_up_active={(session.get('step_up') or {}).get('active')} "
        f"expires_at={(session.get('step_up') or {}).get('expires_at')}"
    )
    return session


def _discover_ecosystem(client: JsonClient, ctx: SmokeContext) -> None:
    payload = client.request_json("GET", _join(ctx.api_base, "/ecosystems"))
    ecosystems = payload.get("ecosystems")
    if isinstance(ecosystems, list):
        names = []
        for entry in ecosystems:
            if isinstance(entry, dict):
                names.append(str(entry.get("name") or ""))
            else:
                names.append(str(entry))
        if ctx.ecosystem not in names:
            raise SmokeError(f"Ecosystem {ctx.ecosystem!r} not present in /ecosystems response")
    print(f"[api] ecosystem {ctx.ecosystem} is present")


def _start_ticket(client: JsonClient, ctx: SmokeContext) -> str:
    payload = client.request_json(
        "POST",
        _join(ctx.api_base, f"/ecosystems/{urllib.parse.quote(ctx.ecosystem)}/actions/ticket-start"),
        body={"ticket_id": ctx.ticket_id, "repos": [ctx.ticket_repo]},
    )
    run_id = str(payload.get("run_id") or "")
    if not run_id:
        raise SmokeError(f"ticket-start did not return a run_id: {payload}")
    print(f"[ticket] started run_id={run_id} ticket_id={ctx.ticket_id} repo={ctx.ticket_repo}")
    return run_id


def _ticket_save_options(client: JsonClient, ctx: SmokeContext) -> Dict[str, Any]:
    url = _join(
        ctx.api_base,
        f"/ecosystems/{urllib.parse.quote(ctx.ecosystem)}/ticket-save/options"
        f"?ticket_id={urllib.parse.quote(ctx.ticket_id)}",
    )
    payload = client.request_json("GET", url)
    option_ids = [str(entry.get("id") or "") for entry in payload.get("options") or [] if isinstance(entry, dict)]
    if "workspace_snapshot" not in option_ids:
        raise SmokeError(f"ticket-save/options missing workspace_snapshot option: {payload}")
    print(
        "[capture] options loaded "
        f"current_tag={payload.get('current_tag')} "
        f"control_repo={payload.get('control_repo')} "
        f"option_ids={option_ids}"
    )
    return payload


def _run_ticket_save(client: JsonClient, ctx: SmokeContext, options: Dict[str, Any]) -> Dict[str, Any]:
    payload = client.request_json(
        "POST",
        _join(ctx.api_base, f"/ecosystems/{urllib.parse.quote(ctx.ecosystem)}/actions/ticket-save"),
        body={
            "ticket_id": ctx.ticket_id,
            "option_id": "workspace_snapshot",
            "expected_current_tag": options.get("current_tag"),
        },
    )
    run_id = str(payload.get("run_id") or "")
    if not run_id:
        raise SmokeError(f"ticket-save did not return a run_id: {payload}")
    print(
        "[capture] save queued "
        f"run_id={run_id} target_tag={payload.get('target_tag')} control_repo={payload.get('control_repo')}"
    )
    return payload


def parse_args(argv: list[str]) -> SmokeContext:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--ecosystem", default=DEFAULT_ECOSYSTEM)
    parser.add_argument("--ticket-id", default=f"AMOF-{int(time.time())}")
    parser.add_argument("--ticket-repo", default="amof")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--no-port-forward", action="store_true")
    parser.add_argument("--port-forward-namespace", default="amof-system")
    parser.add_argument("--port-forward-service", default="amof-controlplane")
    parser.add_argument("--port-forward-local-port", type=int, default=18000)
    args = parser.parse_args(argv)
    return SmokeContext(
        api_base=args.api_base,
        ecosystem=args.ecosystem,
        ticket_id=args.ticket_id,
        ticket_repo=args.ticket_repo,
        timeout_seconds=args.timeout_seconds,
        poll_seconds=args.poll_seconds,
        access_token=None,
        step_up_password=None,
        use_port_forward=not args.no_port_forward,
        port_forward_namespace=args.port_forward_namespace,
        port_forward_service=args.port_forward_service,
        port_forward_local_port=args.port_forward_local_port,
    )


def main(argv: list[str]) -> int:
    ctx = parse_args(argv)
    ctx.access_token = (os.environ.get("AMOF_ACCESS_TOKEN") or "").strip() or None
    ctx.step_up_password = (os.environ.get("AMOF_STEP_UP_PASSWORD") or "").strip() or None

    client = JsonClient()
    try:
        with PortForward(ctx):
            print(f"[start] api_base={ctx.api_base} ecosystem={ctx.ecosystem} ticket_id={ctx.ticket_id}")
            session = _ensure_local_auth_path(client, ctx)
            auth_enabled = bool(session.get("auth_enabled"))
            _discover_ecosystem(client, ctx)

            ticket_start_run_id = _start_ticket(client, ctx)
            ticket_start_status = _poll_run(client, ctx, ticket_start_run_id, step_name="ticket-start")
            print(f"[ticket] ticket-start success finished_at={ticket_start_status.get('finished_at')}")

            options_before = _ticket_save_options(client, ctx)
            session = _ensure_step_up_if_needed(client, ctx, session)
            del session

            save_response = _run_ticket_save(client, ctx, options_before)
            save_status = _poll_run(client, ctx, str(save_response["run_id"]), step_name="ticket-save")
            options_after = _ticket_save_options(client, ctx)

            expected_tag = save_response.get("target_tag")
            actual_tag = options_after.get("current_tag")
            if expected_tag != actual_tag:
                raise SmokeError(
                    f"ticket-save completed but current_tag mismatch: expected {expected_tag!r}, got {actual_tag!r}"
                )

            print("[result] PASS")
            print(
                json.dumps(
                    {
                        "api_base": ctx.api_base,
                        "ecosystem": ctx.ecosystem,
                        "ticket_id": ctx.ticket_id,
                        "ticket_repo": ctx.ticket_repo,
                        "ticket_start_run_id": ticket_start_run_id,
                        "ticket_save_run_id": save_response.get("run_id"),
                        "ticket_save_status": save_status.get("status"),
                        "target_tag": expected_tag,
                        "current_tag_after_save": actual_tag,
                        "control_repo": options_after.get("control_repo"),
                        "auth_enabled": auth_enabled,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
    except SmokeError as exc:
        print(f"[result] FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

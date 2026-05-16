"""Bounded preview evidence checks."""

from __future__ import annotations

from datetime import datetime, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..app_config import ensure_default_context_config, get_context, get_current_context_name
from ..app_paths import ensure_parent_dir, evidence_dir


DEFAULT_OUTPUT_FILENAME = "preview-check-result.json"
DEFAULT_RESPONSE_FILENAME = "raw-response.html"
SUPPORTED_BROWSER_BACKEND = "local-http"


class _LinkCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for name, value in attrs:
            if name.lower() == "href" and value:
                self.links.append(value)


def _default_output_path(run_id: str, context_name: str) -> Path:
    return evidence_dir() / "preview-checks" / context_name / run_id / DEFAULT_OUTPUT_FILENAME


def _collect_links(body_text: str) -> list[str]:
    parser = _LinkCollector()
    parser.feed(body_text)
    return parser.links


def _excerpt(body_text: str, needle: str) -> str | None:
    index = body_text.find(needle)
    if index < 0:
        return None
    start = max(0, index - 60)
    end = min(len(body_text), index + len(needle) + 60)
    return body_text[start:end]


def _required_text_checks(body_text: str, expected_items: list[str]) -> tuple[list[dict[str, Any]], bool]:
    checks: list[dict[str, Any]] = []
    all_passed = True
    for expected in expected_items:
        found = expected in body_text
        checks.append(
            {
                "label": f"required_text:{expected}",
                "expected": expected,
                "found": found,
                "passed": found,
                "evidence_excerpt": _excerpt(body_text, expected),
            }
        )
        all_passed = all_passed and found
    return checks, all_passed


def _forbidden_text_checks(body_text: str, forbidden_items: list[str]) -> tuple[list[dict[str, Any]], bool]:
    checks: list[dict[str, Any]] = []
    all_passed = True
    for forbidden in forbidden_items:
        found = forbidden in body_text
        checks.append(
            {
                "label": f"forbidden_text:{forbidden}",
                "expected": forbidden,
                "found": found,
                "passed": not found,
                "evidence_excerpt": _excerpt(body_text, forbidden),
            }
        )
        all_passed = all_passed and (not found)
    return checks, all_passed


def _expected_link_checks(links: list[str], expected_items: list[str]) -> tuple[list[dict[str, Any]], bool]:
    checks: list[dict[str, Any]] = []
    all_passed = True
    for expected in expected_items:
        matched = next((link for link in links if expected in link), None)
        passed = matched is not None
        checks.append(
            {
                "label": f"expected_link:{expected}",
                "expected": expected,
                "actual": matched,
                "passed": passed,
                "evidence_excerpt": matched,
            }
        )
        all_passed = all_passed and passed
    return checks, all_passed


def _write_result(path: Path, payload: dict[str, Any]) -> Path:
    target = ensure_parent_dir(path)
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return target


def cmd_preview(args: Any) -> int:
    if str(getattr(args, "preview_cmd", "") or "").strip() != "check-url":
        sys.stderr.write("Usage: amof preview check-url [options]\n")
        return 1

    ensure_default_context_config()
    context_name = str(getattr(args, "context", "") or "").strip() or get_current_context_name()
    try:
        context_payload = get_context(context_name)
    except KeyError as exc:
        sys.stderr.write(f"[preview] {exc}\n")
        return 1

    browser_metadata = context_payload.get("browser", {}) if isinstance(context_payload.get("browser"), dict) else {}
    context_browser_backend = browser_metadata.get("backend")
    effective_backend = str(getattr(args, "browser_backend", "") or "").strip() or SUPPORTED_BROWSER_BACKEND
    warnings: list[str] = []
    if context_browser_backend and context_browser_backend != SUPPORTED_BROWSER_BACKEND and not getattr(args, "browser_backend", None):
        warnings.append(
            f"context browser backend '{context_browser_backend}' is metadata only in this MVP; using {SUPPORTED_BROWSER_BACKEND}"
        )

    target_url = str(getattr(args, "url", "") or "").strip()
    run_id = str(getattr(args, "run_id", "") or "").strip()
    if not target_url:
        sys.stderr.write("[preview] --url is required\n")
        return 1
    if not run_id:
        sys.stderr.write("[preview] --run-id is required\n")
        return 1
    if effective_backend != SUPPORTED_BROWSER_BACKEND:
        sys.stderr.write(f"[preview] unsupported browser backend for this MVP: {effective_backend}\n")
        return 1
    timeout_seconds = max(1, int(getattr(args, "timeout_seconds", 10) or 10))

    output_arg = str(getattr(args, "output", "") or "").strip()
    output_path = Path(output_arg).expanduser().resolve(strict=False) if output_arg else _default_output_path(run_id, context_name)
    response_path = output_path.with_name(DEFAULT_RESPONSE_FILENAME)

    result: dict[str, Any] = {
        "result_kind": "preview_check_result",
        "run_id": run_id,
        "context_name": context_name,
        "browser_backend": SUPPORTED_BROWSER_BACKEND,
        "target_url": target_url,
        "resolved_url": None,
        "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "error",
        "http_status_code": None,
        "required_text_checks": [],
        "forbidden_text_checks": [],
        "expected_link_checks": [],
        "artifacts": {
            "screenshot_path": None,
            "markdown_path": None,
            "html_snapshot_path": None,
            "recording_ref": None,
            "raw_response_path": None,
        },
        "errors": [],
        "warnings": warnings,
    }

    try:
        request = Request(target_url, headers={"User-Agent": "amof-preview-check/1.0"})
        with urlopen(request, timeout=timeout_seconds) as response:
            http_status_code = response.getcode()
            resolved_url = response.geturl()
            body_bytes = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
        ensure_parent_dir(response_path).write_bytes(body_bytes)
        body_text = body_bytes.decode(charset, errors="replace")
        links = _collect_links(body_text)
        required_checks, required_ok = _required_text_checks(body_text, list(getattr(args, "required_text", []) or []))
        forbidden_checks, forbidden_ok = _forbidden_text_checks(body_text, list(getattr(args, "forbidden_text", []) or []))
        expected_link_checks, links_ok = _expected_link_checks(links, list(getattr(args, "expected_links", []) or []))

        result["resolved_url"] = resolved_url
        result["http_status_code"] = http_status_code
        result["required_text_checks"] = required_checks
        result["forbidden_text_checks"] = forbidden_checks
        result["expected_link_checks"] = expected_link_checks
        result["artifacts"]["html_snapshot_path"] = str(response_path)
        result["artifacts"]["raw_response_path"] = str(response_path)
        result["status"] = "passed" if required_ok and forbidden_ok and links_ok else "failed"
    except HTTPError as exc:
        result["resolved_url"] = exc.geturl()
        result["http_status_code"] = exc.code
        result["errors"].append(f"http_error:{exc.code}")
        try:
            body_bytes = exc.read()
        except Exception:
            body_bytes = b""
        if body_bytes:
            ensure_parent_dir(response_path).write_bytes(body_bytes)
            result["artifacts"]["html_snapshot_path"] = str(response_path)
            result["artifacts"]["raw_response_path"] = str(response_path)
        result["status"] = "error"
    except URLError as exc:
        result["errors"].append(f"url_error:{exc.reason}")
        result["status"] = "error"
    except Exception as exc:
        result["errors"].append(f"unexpected_error:{exc}")
        result["status"] = "error"

    written = _write_result(output_path, result)
    print(str(written))
    return 0 if result["status"] == "passed" else 1


__all__ = ["cmd_preview"]

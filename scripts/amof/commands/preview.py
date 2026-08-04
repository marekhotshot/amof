"""Bounded preview evidence checks."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from ..app_config import ensure_default_context_config, get_context, get_current_context_name
from ..app_paths import ensure_parent_dir, evidence_dir


DEFAULT_OUTPUT_FILENAME = "preview-check-result.json"
DEFAULT_RESPONSE_FILENAME = "raw-response.html"
DEFAULT_SCREENSHOT_FILENAME = "screenshot.png"
DEFAULT_BROWSER_BACKEND = "local-http"
SUPPORTED_BROWSER_BACKENDS = {"local-http", "local-playwright"}
FAILURE_CLASSES = {
    "DEPLOYMENT_FAILED",
    "BROWSER_RUNTIME_FAILED",
    "NAVIGATION_FAILED",
    "READINESS_TIMEOUT",
    "APPLICATION_SMOKE_FAILED",
    "ASSERTION_FAILED",
    "AUTHENTICATION_BLOCKED",
    "OPERATOR_INTERVENTION_REQUIRED",
}


class BrowserRuntimeUnavailable(RuntimeError):
    """The requested local browser runtime could not be started."""


class BrowserNavigationFailed(RuntimeError):
    """The browser could not navigate to the requested URL."""


class BrowserReadinessTimeout(RuntimeError):
    """The page did not reach bounded readiness before the timeout."""


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


def _safe_url(value: str | None) -> str | None:
    """Remove credentials, query, and fragment from persisted browser URLs."""
    if not value:
        return value
    try:
        parts = urlsplit(value)
        if not parts.scheme or not parts.hostname:
            return "[invalid-url]"
        hostname = parts.hostname
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        netloc = hostname
        if parts.port is not None:
            netloc = f"{netloc}:{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path or "/", "", ""))
    except (TypeError, ValueError):
        return "[invalid-url]"


def _safe_identifier(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._:-]+", "-", value).strip("-")
    return normalized[:96] or "unknown"


def _redact_text(value: str) -> str:
    redacted = re.sub(
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+",
        "Bearer [REDACTED]",
        value,
    )
    redacted = re.sub(
        r"(?i)\b(token|password|credential|api[_-]?key|authorization)\s*[:=]\s*[^\s&]+",
        lambda match: f"{match.group(1)}=[REDACTED]",
        redacted,
    )
    return re.sub(
        r"https?://[^\s\"'<>]+",
        lambda match: _safe_url(match.group(0)) or "[invalid-url]",
        redacted,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _set_outcome(
    result: dict[str, Any],
    *,
    status: str,
    governance_status: str,
    failure_class: str | None = None,
    error: str | None = None,
) -> None:
    if failure_class is not None and failure_class not in FAILURE_CLASSES:
        raise ValueError(f"unknown failure class: {failure_class}")
    result["status"] = status
    result["governance_status"] = governance_status
    result["failure_class"] = failure_class
    if error:
        result["errors"].append(_redact_text(error))


def _attach_screenshot_evidence(
    result: dict[str, Any],
    screenshot_path: Path,
    *,
    run_id: str,
) -> None:
    screenshot_sha256 = _sha256_file(screenshot_path)
    evidence_id = f"evd-browser-screenshot-{_safe_identifier(run_id)}"
    result["artifacts"]["screenshot_path"] = str(screenshot_path)
    result["artifacts"]["screenshot_sha256"] = screenshot_sha256
    result["evidence_refs"]["browser_screenshot"] = {
        "identity": {
            "schema_version": 1,
            "contract_version": "amof-evidence-identity-v1",
            "evidence_id": evidence_id[:128],
            "lifecycle": "sealed",
            "kind": "artifact",
            "durable": True,
            "outlives_runtime": True,
            "outlives_worker": True,
            "run_id": run_id if re.fullmatch(r"run-[A-Za-z0-9][A-Za-z0-9._:-]{2,125}", run_id) else None,
            "ref": str(screenshot_path),
        },
        "media_type": "image/png",
        "sha256": screenshot_sha256,
        "runtime_session_id": result["runtime_session_id"],
        "deployment_id": result["deployment_id"],
    }


def _run_playwright_capture(
    *,
    target_url: str,
    screenshot_path: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserRuntimeUnavailable(
            "python Playwright is not installed; install the 'browser' extra and run "
            "'playwright install chromium'"
        ) from exc

    timeout_ms = timeout_seconds * 1000
    console_errors: list[str] = []
    opened_at: str | None = None
    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
            except PlaywrightError:
                try:
                    browser = playwright.chromium.launch(
                        channel="chrome",
                        headless=True,
                        args=["--no-sandbox", "--disable-dev-shm-usage"],
                    )
                except PlaywrightError as exc:
                    raise BrowserRuntimeUnavailable(f"could not launch Chromium: {exc}") from exc

            try:
                context = browser.new_context(
                    viewport={"width": 1440, "height": 900},
                    ignore_https_errors=True,
                )
                page = context.new_page()
                page.on(
                    "console",
                    lambda message: console_errors.append(_redact_text(message.text))
                    if message.type == "error"
                    else None,
                )
                try:
                    response = page.goto(
                        target_url,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                    opened_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                except PlaywrightTimeoutError as exc:
                    raise BrowserReadinessTimeout(f"navigation readiness timed out: {exc}") from exc
                except PlaywrightError as exc:
                    raise BrowserNavigationFailed(f"navigation failed: {exc}") from exc

                try:
                    page.wait_for_load_state("networkidle", timeout=timeout_ms)
                except PlaywrightTimeoutError as exc:
                    ensure_parent_dir(screenshot_path)
                    page.screenshot(path=str(screenshot_path), full_page=False)
                    raise BrowserReadinessTimeout(f"network readiness timed out: {exc}") from exc

                ensure_parent_dir(screenshot_path)
                page.screenshot(path=str(screenshot_path), full_page=False)
                body_text = page.locator("body").inner_text(timeout=timeout_ms)
                links = page.locator("a[href]").evaluate_all(
                    "(elements) => elements.map((element) => element.getAttribute('href'))"
                )
                return {
                    "resolved_url": page.url,
                    "page_title": page.title(),
                    "opened_at": opened_at,
                    "http_status_code": response.status if response is not None else None,
                    "body_text": body_text,
                    "links": [str(link) for link in links if link],
                    "console_errors": console_errors,
                }
            finally:
                browser.close()
    except (BrowserRuntimeUnavailable, BrowserNavigationFailed, BrowserReadinessTimeout):
        raise
    except PlaywrightError as exc:
        raise BrowserRuntimeUnavailable(f"browser runtime failed: {exc}") from exc


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
    requested_backend = str(getattr(args, "browser_backend", "") or "").strip()
    effective_backend = requested_backend or (
        str(context_browser_backend)
        if context_browser_backend in SUPPORTED_BROWSER_BACKENDS
        else DEFAULT_BROWSER_BACKEND
    )
    warnings: list[str] = []
    if (
        context_browser_backend
        and context_browser_backend not in SUPPORTED_BROWSER_BACKENDS
        and not getattr(args, "browser_backend", None)
    ):
        warnings.append(
            f"context browser backend '{context_browser_backend}' is metadata only in this MVP; using {DEFAULT_BROWSER_BACKEND}"
        )

    target_url = str(getattr(args, "url", "") or "").strip()
    run_id = str(getattr(args, "run_id", "") or "").strip()
    if not target_url:
        sys.stderr.write("[preview] --url is required\n")
        return 1
    if not run_id:
        sys.stderr.write("[preview] --run-id is required\n")
        return 1
    if effective_backend not in SUPPORTED_BROWSER_BACKENDS:
        sys.stderr.write(f"[preview] unsupported browser backend for this MVP: {effective_backend}\n")
        return 1
    timeout_seconds = max(1, int(getattr(args, "timeout_seconds", 10) or 10))
    deployment_id = str(getattr(args, "deployment_id", "") or "").strip() or None

    output_arg = str(getattr(args, "output", "") or "").strip()
    output_path = Path(output_arg).expanduser().resolve(strict=False) if output_arg else _default_output_path(run_id, context_name)
    response_path = output_path.with_name(DEFAULT_RESPONSE_FILENAME)
    screenshot_path = output_path.with_name(DEFAULT_SCREENSHOT_FILENAME)
    runtime_session_id = f"browser-{uuid4()}" if effective_backend == "local-playwright" else None

    result: dict[str, Any] = {
        "result_kind": "preview_check_result",
        "run_id": run_id,
        "deployment_id": deployment_id,
        "context_name": context_name,
        "browser_backend": effective_backend,
        "runtime_session_id": runtime_session_id,
        "browser_session_id": runtime_session_id,
        "target_url": _safe_url(target_url),
        "resolved_url": None,
        "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "opened_at": None,
        "status": "error",
        "governance_status": "BLOCKED",
        "failure_class": "OPERATOR_INTERVENTION_REQUIRED",
        "http_status_code": None,
        "page_title": None,
        "required_text_checks": [],
        "forbidden_text_checks": [],
        "expected_link_checks": [],
        "console_errors": [],
        "artifacts": {
            "screenshot_path": None,
            "screenshot_sha256": None,
            "markdown_path": None,
            "html_snapshot_path": None,
            "recording_ref": None,
            "raw_response_path": None,
        },
        "evidence_refs": {},
        "errors": [],
        "warnings": warnings,
    }

    def apply_assertions(body_text: str, links: list[str]) -> bool:
        required_checks, required_ok = _required_text_checks(body_text, list(getattr(args, "required_text", []) or []))
        forbidden_checks, forbidden_ok = _forbidden_text_checks(body_text, list(getattr(args, "forbidden_text", []) or []))
        expected_link_checks, links_ok = _expected_link_checks(links, list(getattr(args, "expected_links", []) or []))
        result["required_text_checks"] = required_checks
        result["forbidden_text_checks"] = forbidden_checks
        result["expected_link_checks"] = expected_link_checks
        return required_ok and forbidden_ok and links_ok

    try:
        if effective_backend == "local-http":
            request = Request(target_url, headers={"User-Agent": "amof-preview-check/1.0"})
            with urlopen(request, timeout=timeout_seconds) as response:
                result["http_status_code"] = response.getcode()
                result["resolved_url"] = _safe_url(response.geturl())
                body_bytes = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
            ensure_parent_dir(response_path).write_bytes(body_bytes)
            body_text = body_bytes.decode(charset, errors="replace")
            assertions_ok = apply_assertions(body_text, _collect_links(body_text))
            result["artifacts"]["html_snapshot_path"] = str(response_path)
            result["artifacts"]["raw_response_path"] = str(response_path)
            _set_outcome(
                result,
                status="passed" if assertions_ok else "failed",
                governance_status="PASS" if assertions_ok else "FAIL",
                failure_class=None if assertions_ok else "ASSERTION_FAILED",
            )
        else:
            capture = _run_playwright_capture(
                target_url=target_url,
                screenshot_path=screenshot_path,
                timeout_seconds=timeout_seconds,
            )
            result["resolved_url"] = _safe_url(capture["resolved_url"])
            result["http_status_code"] = capture["http_status_code"]
            result["page_title"] = capture["page_title"]
            result["opened_at"] = capture["opened_at"]
            result["console_errors"] = capture["console_errors"]
            _attach_screenshot_evidence(result, screenshot_path, run_id=run_id)
            assertions_ok = apply_assertions(capture["body_text"], capture["links"])
            status_code = capture["http_status_code"]
            if status_code in {401, 403}:
                _set_outcome(
                    result,
                    status="error",
                    governance_status="BLOCKED",
                    failure_class="AUTHENTICATION_BLOCKED",
                    error=f"http_status:{status_code}",
                )
            elif status_code is not None and status_code >= 400:
                _set_outcome(
                    result,
                    status="failed",
                    governance_status="FAIL",
                    failure_class="APPLICATION_SMOKE_FAILED",
                    error=f"http_status:{status_code}",
                )
            else:
                _set_outcome(
                    result,
                    status="passed" if assertions_ok else "failed",
                    governance_status="PASS" if assertions_ok else "FAIL",
                    failure_class=None if assertions_ok else "ASSERTION_FAILED",
                )
    except HTTPError as exc:
        result["resolved_url"] = _safe_url(exc.geturl())
        result["http_status_code"] = exc.code
        failure_class = "AUTHENTICATION_BLOCKED" if exc.code in {401, 403} else "APPLICATION_SMOKE_FAILED"
        governance_status = "BLOCKED" if failure_class == "AUTHENTICATION_BLOCKED" else "FAIL"
        try:
            body_bytes = exc.read()
        except Exception:
            body_bytes = b""
        if body_bytes:
            ensure_parent_dir(response_path).write_bytes(body_bytes)
            result["artifacts"]["html_snapshot_path"] = str(response_path)
            result["artifacts"]["raw_response_path"] = str(response_path)
        _set_outcome(
            result,
            status="error" if governance_status == "BLOCKED" else "failed",
            governance_status=governance_status,
            failure_class=failure_class,
            error=f"http_error:{exc.code}",
        )
    except URLError as exc:
        _set_outcome(
            result,
            status="failed",
            governance_status="FAIL",
            failure_class="NAVIGATION_FAILED",
            error=f"url_error:{exc.reason}",
        )
    except BrowserReadinessTimeout as exc:
        if screenshot_path.exists():
            _attach_screenshot_evidence(result, screenshot_path, run_id=run_id)
        _set_outcome(
            result,
            status="failed",
            governance_status="FAIL",
            failure_class="READINESS_TIMEOUT",
            error=str(exc),
        )
    except BrowserNavigationFailed as exc:
        _set_outcome(
            result,
            status="failed",
            governance_status="FAIL",
            failure_class="NAVIGATION_FAILED",
            error=str(exc),
        )
    except BrowserRuntimeUnavailable as exc:
        _set_outcome(
            result,
            status="error",
            governance_status="BLOCKED",
            failure_class="BROWSER_RUNTIME_FAILED",
            error=str(exc),
        )
    except Exception as exc:
        _set_outcome(
            result,
            status="error",
            governance_status="BLOCKED",
            failure_class="OPERATOR_INTERVENTION_REQUIRED",
            error=f"unexpected_error:{exc}",
        )

    written = _write_result(output_path, result)
    print(str(written))
    return 0 if result["status"] == "passed" else 1


__all__ = ["cmd_preview"]

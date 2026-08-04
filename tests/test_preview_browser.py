from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.cli import parse_args
from amof.commands.preview import (
    BrowserNavigationFailed,
    BrowserReadinessTimeout,
    BrowserRuntimeUnavailable,
    _redact_text,
    cmd_preview,
)


def _args(output: Path, **overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "preview_cmd": "check-url",
        "url": "https://example.test/path?token=must-not-persist",
        "run_id": "run-browser-contract-001",
        "deployment_id": "deploy-controlled-001",
        "context": None,
        "browser_backend": "local-playwright",
        "timeout_seconds": 3,
        "output": str(output),
        "required_text": ["Ready"],
        "forbidden_text": ["Secret"],
        "expected_links": ["/health"],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _capture_success(*, screenshot_path: Path, **_kwargs: object) -> dict[str, object]:
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    screenshot_path.write_bytes(b"\x89PNG\r\ncontrolled-test")
    return {
        "resolved_url": "https://example.test/final?credential=redacted",
        "page_title": "Controlled Preview",
        "opened_at": "2026-08-04T20:00:00Z",
        "http_status_code": 200,
        "body_text": "Ready",
        "links": ["/health"],
        "console_errors": ["Bearer [REDACTED]"],
    }


class PreviewBrowserTests(unittest.TestCase):
    def _run(
        self,
        args: SimpleNamespace,
        *,
        capture_side_effect: object = _capture_success,
    ) -> tuple[int, dict[str, object]]:
        output = Path(args.output)
        with tempfile.TemporaryDirectory(prefix="amof-preview-home-") as home:
            with (
                patch.dict(os.environ, {"AMOF_HOME": home}, clear=False),
                patch(
                    "amof.commands.preview._run_playwright_capture",
                    side_effect=capture_side_effect,
                ),
                redirect_stdout(io.StringIO()),
            ):
                code = cmd_preview(args)
        return code, json.loads(output.read_text(encoding="utf-8"))

    def test_cli_accepts_playwright_and_deployment_provenance(self) -> None:
        argv = [
            "amof",
            "preview",
            "check-url",
            "--url",
            "https://example.test",
            "--run-id",
            "run-cli-001",
            "--deployment-id",
            "deploy-cli-001",
            "--browser-backend",
            "local-playwright",
        ]
        with patch.object(sys, "argv", argv):
            args = parse_args()
        self.assertEqual(args.browser_backend, "local-playwright")
        self.assertEqual(args.deployment_id, "deploy-cli-001")

    def test_console_redaction_removes_credentials_and_url_query(self) -> None:
        redacted = _redact_text(
            "Bearer top-secret https://example.test/path?token=top-secret"
        )
        self.assertNotIn("top-secret", redacted)
        self.assertEqual(
            redacted,
            "Bearer [REDACTED] https://example.test/path",
        )

    def test_pass_attaches_typed_screenshot_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-preview-result-") as td:
            output = Path(td) / "result.json"
            code, payload = self._run(_args(output))

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "passed")
            self.assertEqual(payload["governance_status"], "PASS")
            self.assertIsNone(payload["failure_class"])
            self.assertEqual(payload["deployment_id"], "deploy-controlled-001")
            self.assertEqual(payload["target_url"], "https://example.test/path")
            self.assertEqual(payload["resolved_url"], "https://example.test/final")
            self.assertEqual(payload["console_errors"], ["Bearer [REDACTED]"])
            self.assertTrue(payload["runtime_session_id"].startswith("browser-"))
            evidence = payload["evidence_refs"]["browser_screenshot"]
            self.assertEqual(evidence["identity"]["kind"], "artifact")
            self.assertEqual(evidence["identity"]["run_id"], "run-browser-contract-001")
            self.assertEqual(evidence["deployment_id"], "deploy-controlled-001")
            self.assertTrue(Path(payload["artifacts"]["screenshot_path"]).is_file())
            self.assertEqual(len(payload["artifacts"]["screenshot_sha256"]), 64)

    def test_local_http_backend_remains_supported(self) -> None:
        response = MagicMock()
        response.getcode.return_value = 200
        response.geturl.return_value = "https://example.test/final"
        response.read.return_value = b"<body>Ready<a href='/health'>health</a></body>"
        response.headers.get_content_charset.return_value = "utf-8"
        response.__enter__.return_value = response

        with tempfile.TemporaryDirectory(prefix="amof-preview-result-") as td:
            output = Path(td) / "result.json"
            with patch("amof.commands.preview.urlopen", return_value=response):
                code, payload = self._run(
                    _args(output, browser_backend="local-http")
                )

            self.assertEqual(code, 0)
            self.assertEqual(payload["browser_backend"], "local-http")
            self.assertEqual(payload["governance_status"], "PASS")
            self.assertIsNone(payload["artifacts"]["screenshot_path"])
            self.assertTrue(Path(payload["artifacts"]["raw_response_path"]).is_file())

    def test_navigation_failure_is_structured_fail(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-preview-result-") as td:
            output = Path(td) / "result.json"
            code, payload = self._run(
                _args(output),
                capture_side_effect=BrowserNavigationFailed("navigation failed"),
            )

        self.assertEqual(code, 1)
        self.assertEqual(payload["governance_status"], "FAIL")
        self.assertEqual(payload["failure_class"], "NAVIGATION_FAILED")

    def test_readiness_timeout_preserves_best_effort_screenshot(self) -> None:
        def timeout(*, screenshot_path: Path, **_kwargs: object) -> None:
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            screenshot_path.write_bytes(b"\x89PNG\r\nbest-effort")
            raise BrowserReadinessTimeout("network readiness timed out")

        with tempfile.TemporaryDirectory(prefix="amof-preview-result-") as td:
            output = Path(td) / "result.json"
            code, payload = self._run(_args(output), capture_side_effect=timeout)

            self.assertEqual(code, 1)
            self.assertEqual(payload["governance_status"], "FAIL")
            self.assertEqual(payload["failure_class"], "READINESS_TIMEOUT")
            self.assertTrue(Path(payload["artifacts"]["screenshot_path"]).is_file())

    def test_missing_runtime_is_structured_block(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-preview-result-") as td:
            output = Path(td) / "result.json"
            code, payload = self._run(
                _args(output),
                capture_side_effect=BrowserRuntimeUnavailable("playwright unavailable"),
            )

        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["governance_status"], "BLOCKED")
        self.assertEqual(payload["failure_class"], "BROWSER_RUNTIME_FAILED")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import amof.entrypoint as entrypoint
from amof.commands import handoff


class _FakeStdin:
    def __init__(self, data: bytes) -> None:
        self.buffer = io.BytesIO(data)


def _prepare_args(**overrides: object) -> SimpleNamespace:
    payload: dict[str, object] = {
        "command": "handoff",
        "handoff_cmd": "prepare",
        "payload_kind": "selected-text",
        "source": "chatgpt",
        "target": "zed",
        "preview": True,
        "confirm": False,
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def _run_prepare(
    args: SimpleNamespace, stdin_bytes: bytes, amof_home: Path
) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with patch.dict(os.environ, {"AMOF_HOME": str(amof_home)}, clear=False):
        with patch("sys.stdin", _FakeStdin(stdin_bytes)):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = handoff.cmd_handoff_prepare(args)
    return code, stdout.getvalue(), stderr.getvalue()


def _outbox_dir(amof_home: Path) -> Path:
    return amof_home / "share" / "handoff" / "outbox"


class HandoffPrepareTests(unittest.TestCase):
    def test_selected_text_preview_without_confirmation_writes_nothing(self) -> None:
        with TemporaryDirectory(prefix="amof-handoff-preview-") as td:
            amof_home = Path(td)
            code, stdout, stderr = _run_prepare(
                _prepare_args(payload_kind="selected-text", confirm=False),
                b"hello from selected text",
                amof_home,
            )

            self.assertEqual(code, 0)
            self.assertEqual(stdout, "")
            self.assertIn("[handoff] Preview", stderr)
            self.assertIn("payload_kind: selected_text", stderr)
            self.assertIn("character_count: 24", stderr)
            self.assertIn("utf8_byte_count: 24", stderr)
            self.assertIn("hello from selected text", stderr)
            self.assertIn("Preview only; no packet written", stderr)
            self.assertFalse(_outbox_dir(amof_home).exists())
            self.assertEqual(
                [path for path in amof_home.rglob("*") if path.is_file()], []
            )

    def test_last_response_preview_without_confirmation_writes_nothing(self) -> None:
        with TemporaryDirectory(prefix="amof-handoff-last-response-") as td:
            amof_home = Path(td)
            code, stdout, stderr = _run_prepare(
                _prepare_args(payload_kind="last-response", target="chatgpt"),
                b"assistant reply block",
                amof_home,
            )

            self.assertEqual(code, 0)
            self.assertEqual(stdout, "")
            self.assertIn("payload_kind: last_response", stderr)
            self.assertIn("target: chatgpt", stderr)
            self.assertFalse(_outbox_dir(amof_home).exists())

    def test_confirmed_flow_writes_exactly_one_packet_and_one_json_receipt(
        self,
    ) -> None:
        payload_text = "bounded payload for local outbox"
        with TemporaryDirectory(prefix="amof-handoff-confirm-") as td:
            amof_home = Path(td)
            code, stdout, stderr = _run_prepare(
                _prepare_args(confirm=True),
                payload_text.encode("utf-8"),
                amof_home,
            )
            files = [path for path in amof_home.rglob("*") if path.is_file()]

            self.assertEqual(code, 0)
            self.assertIn("[handoff] Preview", stderr)
            self.assertIn(payload_text, stderr)
            self.assertEqual(len(files), 1)
            packet_path = files[0]
            self.assertEqual(packet_path.parent, _outbox_dir(amof_home))

            receipt = json.loads(stdout)
            self.assertEqual(
                stdout.strip(),
                json.dumps(
                    receipt, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                ),
            )
            self.assertEqual(receipt["status"], "prepared")
            self.assertEqual(receipt["packet_path"], str(packet_path))
            self.assertEqual(receipt["character_count"], len(payload_text))
            self.assertEqual(
                receipt["utf8_byte_count"], len(payload_text.encode("utf-8"))
            )
            self.assertNotIn(payload_text, stdout)

            packet = json.loads(packet_path.read_text(encoding="utf-8"))
            self.assertEqual(packet["schema_version"], 1)
            self.assertEqual(packet["handoff_id"], receipt["handoff_id"])
            self.assertEqual(packet["source"], "chatgpt")
            self.assertEqual(packet["target"], "zed")
            self.assertEqual(packet["payload_kind"], "selected_text")
            self.assertEqual(packet["state"], "prepared")
            self.assertEqual(packet["payload"]["text"], payload_text)
            self.assertEqual(
                packet["payload"]["character_count"], receipt["character_count"]
            )
            self.assertEqual(
                packet["payload"]["utf8_byte_count"], receipt["utf8_byte_count"]
            )
            self.assertEqual(packet["payload"]["sha256"], receipt["sha256"])

    def test_packet_permissions_are_restrictive(self) -> None:
        with TemporaryDirectory(prefix="amof-handoff-perms-") as td:
            amof_home = Path(td)
            code, stdout, _stderr = _run_prepare(
                _prepare_args(confirm=True),
                b"permission check",
                amof_home,
            )
            receipt = json.loads(stdout)
            packet_path = Path(receipt["packet_path"])
            outbox_dir = packet_path.parent

            self.assertEqual(code, 0)
            self.assertEqual(stat.S_IMODE(os.stat(outbox_dir).st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(os.stat(packet_path).st_mode), 0o600)

    def test_empty_input_is_rejected(self) -> None:
        with TemporaryDirectory(prefix="amof-handoff-empty-") as td:
            code, stdout, stderr = _run_prepare(_prepare_args(), b"", Path(td))

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("empty", stderr)

    def test_invalid_utf8_input_is_rejected(self) -> None:
        with TemporaryDirectory(prefix="amof-handoff-invalid-utf8-") as td:
            code, stdout, stderr = _run_prepare(
                _prepare_args(), b"\xff\xfe\xfd", Path(td)
            )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("valid UTF-8", stderr)

    def test_nul_input_is_rejected(self) -> None:
        with TemporaryDirectory(prefix="amof-handoff-nul-") as td:
            code, stdout, stderr = _run_prepare(
                _prepare_args(), b"abc\x00def", Path(td)
            )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("NUL", stderr)

    def test_exactly_40000_utf8_bytes_is_accepted(self) -> None:
        payload = b"a" * 40000
        with TemporaryDirectory(prefix="amof-handoff-40000-") as td:
            code, stdout, _stderr = _run_prepare(
                _prepare_args(confirm=True), payload, Path(td)
            )

        receipt = json.loads(stdout)
        self.assertEqual(code, 0)
        self.assertEqual(receipt["utf8_byte_count"], 40000)
        self.assertEqual(receipt["character_count"], 40000)

    def test_more_than_40000_utf8_bytes_is_rejected_without_truncation(self) -> None:
        payload = b"a" * 40001
        with TemporaryDirectory(prefix="amof-handoff-40001-") as td:
            amof_home = Path(td)
            code, stdout, stderr = _run_prepare(
                _prepare_args(confirm=True), payload, amof_home
            )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("40000", stderr)
        self.assertIn("40001", stderr)
        self.assertFalse(_outbox_dir(amof_home).exists())

    def test_multibyte_utf8_count_is_calculated_correctly(self) -> None:
        text = "🙂é漢字"
        raw = text.encode("utf-8")
        with TemporaryDirectory(prefix="amof-handoff-multibyte-") as td:
            code, stdout, stderr = _run_prepare(
                _prepare_args(confirm=True), raw, Path(td)
            )

        receipt = json.loads(stdout)
        self.assertEqual(code, 0)
        self.assertEqual(receipt["character_count"], len(text))
        self.assertEqual(receipt["utf8_byte_count"], len(raw))
        self.assertIn(f"character_count: {len(text)}", stderr)
        self.assertIn(f"utf8_byte_count: {len(raw)}", stderr)

    def test_source_and_target_remain_metadata_only(self) -> None:
        with TemporaryDirectory(prefix="amof-handoff-metadata-only-") as td:
            code, stdout, _stderr = _run_prepare(
                _prepare_args(confirm=True, source="chatgpt", target="zed"),
                b"metadata only",
                Path(td),
            )

            packet = json.loads(
                Path(json.loads(stdout)["packet_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(code, 0)
            self.assertEqual(packet["source"], "chatgpt")
            self.assertEqual(packet["target"], "zed")
            self.assertEqual(
                sorted(packet.keys()),
                [
                    "handoff_id",
                    "payload",
                    "payload_kind",
                    "schema_version",
                    "source",
                    "state",
                    "target",
                ],
            )

    def test_no_agent_invocation_subprocess_or_network_occurs(self) -> None:
        with TemporaryDirectory(prefix="amof-handoff-no-external-") as td:
            with (
                patch(
                    "subprocess.run", side_effect=AssertionError("subprocess forbidden")
                ),
                patch(
                    "socket.create_connection",
                    side_effect=AssertionError("network forbidden"),
                ),
                patch(
                    "amof.commands.agent_cmd.cmd_agent",
                    side_effect=AssertionError("agent forbidden"),
                ),
            ):
                code, stdout, _stderr = _run_prepare(
                    _prepare_args(confirm=True),
                    b"local only",
                    Path(td),
                )

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["status"], "prepared")

    def test_receipt_and_generic_appdata_do_not_expose_payload_text(self) -> None:
        payload_text = "do not leak into generic logs"
        with TemporaryDirectory(prefix="amof-handoff-no-leak-") as td:
            amof_home = Path(td)
            code, stdout, _stderr = _run_prepare(
                _prepare_args(confirm=True),
                payload_text.encode("utf-8"),
                amof_home,
            )
            receipt = json.loads(stdout)
            packet_path = Path(receipt["packet_path"])
            non_packet_texts = []
            for path in amof_home.rglob("*"):
                if not path.is_file() or path == packet_path:
                    continue
                non_packet_texts.append(path.read_text(encoding="utf-8"))

        self.assertEqual(code, 0)
        self.assertNotIn(payload_text, stdout)
        self.assertEqual(non_packet_texts, [])


class HandoffEntrypointTests(unittest.TestCase):
    def test_handoff_is_no_ecosystem_command(self) -> None:
        self.assertIn("handoff", entrypoint.NO_ECOSYSTEM_COMMANDS)

    def test_entrypoint_dispatches_handoff_without_manifest_loading(self) -> None:
        args = _prepare_args(confirm=True)
        with (
            patch("amof.entrypoint.parse_args", return_value=args),
            patch("amof.entrypoint.cmd_handoff", return_value=3) as handoff_mock,
        ):
            with self.assertRaises(SystemExit) as exc:
                entrypoint.main()

        self.assertEqual(exc.exception.code, 3)
        handoff_mock.assert_called_once_with(args)

    def test_public_agent_help_surface_remains_available(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "amof.py"), "agent", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(SCRIPTS_ROOT)},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--request-json", result.stdout)
        self.assertIn("--json", result.stdout)


if __name__ == "__main__":
    unittest.main()

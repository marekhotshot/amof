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
        "studio_session": None,
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


def _canonical_mission_packet(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "contract_version": "canonical-mission-packet-v1",
        "mission": {
            "mission_id": "AMOF-HANDOFF-CANONICAL-MISSION-PACKET-PUBLIC-001",
            "ticket_id": "AMOF-123",
        },
        "task_class": "implementation",
        "classification": "public",
        "goal": "Implement the bounded public handoff transport slice.",
        "objective": "Add strict public AMOF handoff transport support for canonical mission packets.",
        "target_repository": {
            "repo_name": "amof",
            "repo_owner": "public",
            "branch_ref": "origin/main",
        },
        "execution_allowed": True,
        "mutations": {
            "requested_mode": "bounded_worktree",
            "allowed": ["bounded_worktree"],
            "forbidden": [
                "shell_commands",
                "env_mutation",
                "secret_injection",
                "auth_headers",
                "deployment",
            ],
        },
        "validation_gates": [
            "focused_handoff_tests",
            "request_schema_tests",
            "contract_tests",
            "py_compile",
            "git_diff_check",
        ],
    }
    payload.update(overrides)
    return json.loads(json.dumps(payload))


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

    def test_canonical_mission_packet_prepare_writes_canonical_payload_and_redacts_preview(
        self,
    ) -> None:
        packet = _canonical_mission_packet()
        raw = json.dumps(packet, indent=2).encode("utf-8")
        with TemporaryDirectory(prefix="amof-handoff-canonical-prepare-") as td:
            code, stdout, stderr = _run_prepare(
                _prepare_args(
                    confirm=True,
                    payload_kind="canonical-mission-packet",
                    studio_session="studio-20260608-004150",
                ),
                raw,
                Path(td),
            )

            receipt = json.loads(stdout)
            packet_path = Path(receipt["packet_path"])
            written_packet = json.loads(packet_path.read_text(encoding="utf-8"))
            stored_text = written_packet["payload"]["text"]
            expected_packet = dict(packet)
            expected_packet["studio_session_id"] = "studio-20260608-004150"

        self.assertEqual(code, 0)
        self.assertIn("payload_kind: canonical_mission_packet", stderr)
        self.assertIn("canonical_mission_packet:", stderr)
        self.assertIn("mission_id: AMOF-HANDOFF-CANONICAL-MISSION-PACKET-PUBLIC-001", stderr)
        self.assertIn("ticket_id: AMOF-123", stderr)
        self.assertIn("studio_session_id: studio-20260608-004150", stderr)
        self.assertNotIn(expected_packet["objective"], stderr)
        self.assertNotIn(expected_packet["goal"], stderr)
        self.assertEqual(stored_text, handoff._canonical_json(expected_packet))
        self.assertEqual(
            receipt["sha256"],
            handoff.hashlib.sha256(stored_text.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(written_packet["payload_kind"], "canonical_mission_packet")
        self.assertEqual(
            written_packet["studio_session_id"], "studio-20260608-004150"
        )

    def test_canonical_mission_packet_invalid_json_is_rejected(self) -> None:
        with TemporaryDirectory(prefix="amof-handoff-canonical-invalid-json-") as td:
            code, stdout, stderr = _run_prepare(
                _prepare_args(payload_kind="canonical-mission-packet"),
                b'{"schema_version":1,',
                Path(td),
            )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("valid JSON", stderr)

    def test_canonical_mission_packet_non_object_json_is_rejected(self) -> None:
        with TemporaryDirectory(prefix="amof-handoff-canonical-non-object-") as td:
            code, stdout, stderr = _run_prepare(
                _prepare_args(payload_kind="canonical-mission-packet"),
                b'["not","an","object"]',
                Path(td),
            )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("JSON object", stderr)

    def test_canonical_mission_packet_missing_mission_identity_is_rejected(self) -> None:
        packet = _canonical_mission_packet()
        mission = dict(packet["mission"])
        mission.pop("mission_id")
        packet["mission"] = mission
        with TemporaryDirectory(prefix="amof-handoff-canonical-missing-mission-") as td:
            code, stdout, stderr = _run_prepare(
                _prepare_args(payload_kind="canonical-mission-packet"),
                json.dumps(packet).encode("utf-8"),
                Path(td),
            )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("mission", stderr)
        self.assertIn("required fields", stderr)

    def test_canonical_mission_packet_unknown_unsafe_fields_are_rejected(self) -> None:
        packet = _canonical_mission_packet()
        packet["filesystem_path"] = "/tmp/unsafe"
        with TemporaryDirectory(prefix="amof-handoff-canonical-unknown-field-") as td:
            code, stdout, stderr = _run_prepare(
                _prepare_args(payload_kind="canonical-mission-packet"),
                json.dumps(packet).encode("utf-8"),
                Path(td),
            )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("unknown fields", stderr)

    def test_canonical_mission_packet_secret_like_values_are_rejected_without_echoing_them(
        self,
    ) -> None:
        secret_value = "sk-public-test-should-not-leak"
        packet = _canonical_mission_packet(
            objective=f"Do not expose token={secret_value} during handoff."
        )
        with TemporaryDirectory(prefix="amof-handoff-canonical-secret-") as td:
            code, stdout, stderr = _run_prepare(
                _prepare_args(payload_kind="canonical-mission-packet"),
                json.dumps(packet).encode("utf-8"),
                Path(td),
            )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("secret-like material", stderr)
        self.assertNotIn(secret_value, stderr)

    def test_canonical_mission_packet_rejects_shell_env_token_and_self_approval_fields(
        self,
    ) -> None:
        unsafe_fields = {
            "command": "echo unsafe",
            "shell_command": "echo unsafe",
            "env": {"OPENAI_API_KEY": "secret"},
            "token": "secret-value",
            "approve_capabilities": ["secret"],
            "approve_tool_packs": ["ops-jenkins"],
            "approve_writable_roots": ["/tmp/out"],
        }
        for field_name, field_value in unsafe_fields.items():
            with self.subTest(field_name=field_name):
                packet = _canonical_mission_packet()
                packet[field_name] = field_value
                with TemporaryDirectory(
                    prefix=f"amof-handoff-canonical-unsafe-{field_name}-"
                ) as td:
                    code, stdout, stderr = _run_prepare(
                        _prepare_args(payload_kind="canonical-mission-packet"),
                        json.dumps(packet).encode("utf-8"),
                        Path(td),
                    )
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertIn("unknown fields", stderr)

    def test_canonical_mission_packet_schema_is_strict_and_versioned(self) -> None:
        schema = json.loads(
            (
                ROOT / "contracts" / "canonical-mission-packet.schema.json"
            ).read_text(encoding="utf-8")
        )

        self.assertEqual(schema["properties"]["schema_version"]["const"], 1)
        self.assertEqual(
            schema["properties"]["contract_version"]["const"],
            "canonical-mission-packet-v1",
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertIn("runtime", schema["properties"])
        self.assertIn("mission", schema["required"])
        self.assertIn("goal", schema["required"])
        self.assertIn("branch_ref", schema["properties"]["target_repository"]["required"])

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

    def test_prepare_with_studio_session_preserves_exact_packet_field(self) -> None:
        studio_session_id = "studio-20260608-004150"
        with TemporaryDirectory(prefix="amof-handoff-studio-session-") as td:
            code, stdout, stderr = _run_prepare(
                _prepare_args(confirm=True, studio_session=studio_session_id),
                b"correlate this run",
                Path(td),
            )
            packet = json.loads(
                Path(json.loads(stdout)["packet_path"]).read_text(encoding="utf-8")
            )

        self.assertEqual(code, 0)
        self.assertEqual(packet["studio_session_id"], studio_session_id)
        self.assertIn(f"studio_session_id: {studio_session_id}", stderr)

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

    def test_handoff_prepare_help_mentions_optional_studio_session(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "amof.py"), "handoff", "prepare", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(SCRIPTS_ROOT)},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--studio-session", result.stdout)
        self.assertIn("already exist", result.stdout)
        self.assertIn("governed Agent run", result.stdout)


if __name__ == "__main__":
    unittest.main()

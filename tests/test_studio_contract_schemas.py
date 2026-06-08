from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.commands import agent_cmd
from amof.commands import studio as studio_cmd


STUDIO_SESSION_SCHEMA_PATH = ROOT / "contracts" / "studio-session.schema.json"
STUDIO_EVENT_SCHEMA_PATH = ROOT / "contracts" / "studio-event.schema.json"
STUDIO_RUN_REFERENCE_SCHEMA_PATH = ROOT / "contracts" / "studio-run-reference.schema.json"
STUDIO_CHECKPOINT_SCHEMA_PATH = ROOT / "contracts" / "studio-checkpoint.schema.json"
AGENT_RUN_RESULT_SCHEMA_PATH = ROOT / "contracts" / "agent-run-result.schema.json"
CONTRACTS_README_PATH = ROOT / "contracts" / "README.md"
STUDIO_LIFECYCLE_PATH = ROOT / "contracts" / "studio-lifecycle.md"

EXAMPLE_PATHS = [
    ROOT / "contracts" / "examples" / "studio-session.example.json",
    ROOT / "contracts" / "examples" / "studio-event.session-created.example.json",
    ROOT / "contracts" / "examples" / "studio-event.run-attached.example.json",
    ROOT / "contracts" / "examples" / "studio-run-reference.example.json",
    ROOT / "contracts" / "examples" / "studio-checkpoint.example.json",
    ROOT / "contracts" / "examples" / "agent-run-result.example.json",
]


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict]:
    payloads: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payloads.append(json.loads(line))
    return payloads


def _secret_key_present(payload: object) -> bool:
    if isinstance(payload, dict):
        for key, value in payload.items():
            lowered = str(key).lower()
            if any(
                marker in lowered
                for marker in (
                    "secret",
                    "token",
                    "password",
                    "credential",
                    "credentials",
                    "api_key",
                    "apikey",
                    "auth_header",
                    "authorization",
                )
            ):
                return True
            if _secret_key_present(value):
                return True
    if isinstance(payload, list):
        return any(_secret_key_present(item) for item in payload)
    return False


def _fallback_validate(schema_path: Path, payload: dict) -> None:
    schema = _load(schema_path)
    properties = set(schema.get("properties", {}))
    required = set(schema.get("required", []))
    extra = set(payload) - properties
    if extra:
        raise ValueError(f"Unexpected fields for {schema_path.name}: {sorted(extra)}")
    missing = [key for key in required if key not in payload]
    if missing:
        raise ValueError(f"Missing required fields for {schema_path.name}: {missing}")
    if _secret_key_present(payload):
        raise ValueError(f"Secret-like fields are forbidden in {schema_path.name}")

    name = schema_path.name
    if name == "studio-session.schema.json":
        if payload.get("schema_version") != 1:
            raise ValueError("studio session schema_version must equal 1")
        if payload.get("status") not in {"active", "ended"}:
            raise ValueError("studio session status must be active or ended")
    elif name == "studio-event.schema.json":
        if payload.get("event_type") not in {
            "studio_session_created",
            "studio_checkpoint_added",
            "studio_session_ended",
            "run.attached",
            "studio_run_attached",
        }:
            raise ValueError("unsupported studio event type")
    elif name == "studio-run-reference.schema.json":
        if payload.get("schema_version") != 1:
            raise ValueError("studio run reference schema_version must equal 1")
        if not str(payload.get("run_id") or "").strip():
            raise ValueError("run_id is required")
    elif name == "studio-checkpoint.schema.json":
        if payload.get("schema_version") != 1:
            raise ValueError("studio checkpoint schema_version must equal 1")
        if not str(payload.get("summary") or "").strip():
            raise ValueError("checkpoint summary is required")
    elif name == "agent-run-result.schema.json":
        if payload.get("schema_version") != 1:
            raise ValueError("agent run result schema_version must equal 1")
        if payload.get("result_kind") != "agent_run_result":
            raise ValueError("result_kind must equal agent_run_result")


def _validate(schema_path: Path, payload: dict) -> None:
    try:
        import jsonschema  # type: ignore
    except ImportError:
        _fallback_validate(schema_path, payload)
        return
    jsonschema.validate(instance=payload, schema=_load(schema_path))


@contextmanager
def _cwd(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def _commit_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "AMOF Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "AMOF Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
    )
    return env


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    (path / "README.md").write_text("test\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "."],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "test: init"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        env=_commit_env(),
    )


class _FakeRunnerAgent:
    instances: list["_FakeRunnerAgent"] = []
    stop_reason = "completed"

    def __init__(self, *args, **kwargs) -> None:
        self.llm = kwargs.get("llm") or (args[0] if args else None)
        self.model_router = kwargs.get("model_router")
        self.tools = kwargs.get("tools")
        self.__class__.instances.append(self)

    def run(self, goal: str) -> str:
        return f"fake agent response for {goal}"


def _write_run_events(events_path: Path, run_id: str, session_id: str) -> None:
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "event_id": f"{run_id}:0001",
            "run_id": run_id,
            "session_id": session_id,
            "timestamp": "2026-06-08T00:00:00+00:00",
            "event_type": "run_created",
            "severity": "info",
            "actor": "amof.agent",
            "planning_mode": "execute",
        },
        {
            "event_id": f"{run_id}:0002",
            "run_id": run_id,
            "session_id": session_id,
            "timestamp": "2026-06-08T00:00:01+00:00",
            "event_type": "run_finished",
            "severity": "info",
            "actor": "amof.agent",
            "planning_mode": "execute",
            "cost_status": "unknown",
            "estimated_cost": None,
        },
    ]
    with events_path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")


class StudioContractSchemaTests(unittest.TestCase):
    def test_schema_surfaces_are_versioned_and_strict(self) -> None:
        session_schema = _load(STUDIO_SESSION_SCHEMA_PATH)
        event_schema = _load(STUDIO_EVENT_SCHEMA_PATH)
        run_reference_schema = _load(STUDIO_RUN_REFERENCE_SCHEMA_PATH)
        checkpoint_schema = _load(STUDIO_CHECKPOINT_SCHEMA_PATH)
        agent_run_schema = _load(AGENT_RUN_RESULT_SCHEMA_PATH)

        self.assertEqual(session_schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(session_schema["properties"]["schema_version"]["const"], 1)
        self.assertFalse(session_schema["additionalProperties"])

        self.assertEqual(event_schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertFalse(event_schema["additionalProperties"])
        self.assertIn("run.attached", event_schema["properties"]["event_type"]["enum"])
        self.assertIn("studio_run_attached", event_schema["properties"]["event_type"]["enum"])

        self.assertEqual(run_reference_schema["properties"]["schema_version"]["const"], 1)
        self.assertIn("agent_run_id", run_reference_schema["properties"])
        self.assertFalse(run_reference_schema["additionalProperties"])

        self.assertEqual(checkpoint_schema["properties"]["schema_version"]["const"], 1)
        self.assertFalse(checkpoint_schema["additionalProperties"])

        self.assertEqual(
            agent_run_schema["properties"]["studio_session_id"]["pattern"],
            "^studio-[0-9]{8}-[0-9]{6}(?:-[0-9]{2})?$",
        )

    def test_examples_validate_against_contract_schemas(self) -> None:
        schema_by_example = {
            "studio-session.example.json": STUDIO_SESSION_SCHEMA_PATH,
            "studio-event.session-created.example.json": STUDIO_EVENT_SCHEMA_PATH,
            "studio-event.run-attached.example.json": STUDIO_EVENT_SCHEMA_PATH,
            "studio-run-reference.example.json": STUDIO_RUN_REFERENCE_SCHEMA_PATH,
            "studio-checkpoint.example.json": STUDIO_CHECKPOINT_SCHEMA_PATH,
            "agent-run-result.example.json": AGENT_RUN_RESULT_SCHEMA_PATH,
        }

        for example_path in EXAMPLE_PATHS:
            with self.subTest(example=example_path.name):
                _validate(schema_by_example[example_path.name], _load(example_path))

    def test_generated_studio_artifacts_validate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-studio-schema-artifacts-") as td:
            amof_home = Path(td) / "amof-home"
            with patch.dict(os.environ, {"AMOF_HOME": str(amof_home)}, clear=False):
                created = studio_cmd._create_studio_session()
                studio_session_id = created["manifest"]["studio_session_id"]
                session_dir = Path(created["paths"]["session_dir"])

                run_events = amof_home / "share" / "runs" / "run-123" / "events.jsonl"
                _write_run_events(run_events, "run-123", "run-123")
                studio_cmd._attach_run(studio_session_id, "run-123")
                studio_cmd._add_checkpoint(
                    studio_session_id,
                    "Planner reviewed and ready for execution.",
                )
                studio_cmd._end_studio_session(studio_session_id)

                session_payload = _load(session_dir / "session.json")
                event_payloads = _load_jsonl(session_dir / "events.jsonl")
                run_reference_payloads = json.loads(
                    (session_dir / "runs.json").read_text(encoding="utf-8")
                )
                checkpoint_payloads = _load_jsonl(session_dir / "checkpoints.jsonl")

        _validate(STUDIO_SESSION_SCHEMA_PATH, session_payload)
        self.assertEqual(
            [payload["event_id"] for payload in event_payloads],
            sorted(payload["event_id"] for payload in event_payloads),
        )
        for payload in event_payloads:
            _validate(STUDIO_EVENT_SCHEMA_PATH, payload)
        for payload in run_reference_payloads:
            _validate(STUDIO_RUN_REFERENCE_SCHEMA_PATH, payload)
        for payload in checkpoint_payloads:
            _validate(STUDIO_CHECKPOINT_SCHEMA_PATH, payload)

    def test_correlated_agent_run_result_validates(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-studio-schema-run-result-") as td:
            temp = Path(td)
            repo = temp / "demo-repo"
            amof_home = temp / "amof-home"
            _init_git_repo(repo)
            manifest = {
                "ecosystem": "demo-repo",
                "manifest_source": "appdata",
                "repos": [{"name": "demo-repo", "path": str(repo), "url": f"local://{repo}"}],
            }
            plan_file = amof_home / "share" / "plans" / "demo-repo" / "plan.md"
            plan_file.parent.mkdir(parents=True, exist_ok=True)
            plan_file.write_text(
                (
                    "# Execution Plan\n\n"
                    "**Status**: pending\n\n"
                    "## Analysis\n\n"
                    "Inspect the repository without mutating it.\n\n"
                    "---\n\n"
                    "## Tasks\n\n"
                    "- [ ] 1. **Inspect the repo** (code)\n"
                ),
                encoding="utf-8",
            )
            env = {
                "AMOF_HOME": str(amof_home),
                "OPENROUTER_API_KEY": "unit-test-provider-value",
            }
            with patch.dict(os.environ, env, clear=False):
                studio_session_id = studio_cmd._create_studio_session()["manifest"][
                    "studio_session_id"
                ]
                with _cwd(repo):
                    from amof.orchestrator.planner import ExecutionPlan

                    with patch("amof.orchestrator.runners.Agent", _FakeRunnerAgent):
                        with patch(
                            "amof.orchestrator.planner.TaskPlanner.plan",
                            return_value=ExecutionPlan.load_from_markdown(plan_file),
                        ):
                            envelope = agent_cmd.cmd_agent(
                                manifest,
                                goal="Inspect this repo",
                                plan_execute=True,
                                provider="openrouter",
                                no_follow_up=True,
                                approve_plan=True,
                                studio_session_id=studio_session_id,
                                _json_envelope=True,
                            )

        self.assertIsInstance(envelope, agent_cmd.AgentPlanExecuteEnvelope)
        self.assertEqual(envelope.studio_session_id, studio_session_id)
        _validate(AGENT_RUN_RESULT_SCHEMA_PATH, envelope.to_dict())

    def test_legacy_attach_event_remains_schema_compatible(self) -> None:
        legacy_payload = {
            "event_id": "studio-20260608-004150:0002",
            "studio_session_id": "studio-20260608-004150",
            "timestamp": "2026-06-08T00:42:00+00:00",
            "event_type": "studio_run_attached",
            "run_id": "20260608-004200",
            "session_id": "20260608-004200",
            "surface": "agent",
            "mode": "execute",
            "events_path": "/tmp/amof-home/share/runs/20260608-004200/events.jsonl",
        }

        _validate(STUDIO_EVENT_SCHEMA_PATH, legacy_payload)

    def test_secret_like_fields_are_rejected(self) -> None:
        session_payload = _load(ROOT / "contracts" / "examples" / "studio-session.example.json")
        session_payload["api_key"] = "secret"
        with self.assertRaises(Exception):
            _validate(STUDIO_SESSION_SCHEMA_PATH, session_payload)

        event_payload = _load(
            ROOT / "contracts" / "examples" / "studio-event.run-attached.example.json"
        )
        event_payload["authorization"] = "Bearer secret"
        with self.assertRaises(Exception):
            _validate(STUDIO_EVENT_SCHEMA_PATH, event_payload)

        run_reference_payload = _load(
            ROOT / "contracts" / "examples" / "studio-run-reference.example.json"
        )
        run_reference_payload["provider_token"] = "secret"
        with self.assertRaises(Exception):
            _validate(STUDIO_RUN_REFERENCE_SCHEMA_PATH, run_reference_payload)

        checkpoint_payload = _load(
            ROOT / "contracts" / "examples" / "studio-checkpoint.example.json"
        )
        checkpoint_payload["raw_credentials"] = "secret"
        with self.assertRaises(Exception):
            _validate(STUDIO_CHECKPOINT_SCHEMA_PATH, checkpoint_payload)

        agent_run_payload = _load(ROOT / "contracts" / "examples" / "agent-run-result.example.json")
        agent_run_payload["budget_summary"]["api_key"] = "secret"
        with self.assertRaises(Exception):
            _validate(AGENT_RUN_RESULT_SCHEMA_PATH, agent_run_payload)

    def test_contract_docs_list_studio_schema_family(self) -> None:
        readme = CONTRACTS_README_PATH.read_text(encoding="utf-8")
        lifecycle = STUDIO_LIFECYCLE_PATH.read_text(encoding="utf-8")

        self.assertIn("studio-session.schema.json", readme)
        self.assertIn("studio-event.schema.json", readme)
        self.assertIn("studio-run-reference.schema.json", readme)
        self.assertIn("studio-checkpoint.schema.json", readme)
        self.assertIn("AgentRunResult", lifecycle)
        self.assertIn("run.attached", lifecycle)


if __name__ == "__main__":
    unittest.main()

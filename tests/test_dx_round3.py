"""Public DX round 3: httpx fallback, write-scope preflight, doc truth."""

from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
import importlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.commands import agent_cmd
from amof.commands.help_cmd import cmd_help
from amof.orchestrator.llm import anthropic as anthropic_mod
from amof.orchestrator.llm.anthropic import AnthropicClient
from amof.orchestrator.plan_execute_control import preflight_write_scope_flags
from amof.write_scope_approvals import approve_proposal, revoke_approval
from amof.write_scope_bindings import git_rev_parse_head, list_bindings
from amof.write_scope_proposals import persist_write_scope_proposals_from_result

OPERATOR = "operator:dx-round3"


@contextmanager
def _cwd(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def _init_git_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "dx3@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "DX3 Tester"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    (path / "src").mkdir(parents=True, exist_ok=True)
    (path / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/app.py"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    return git_rev_parse_head(path)


class AnthropicHttpxFallbackTests(unittest.TestCase):
    def test_get_client_without_httpx_uses_default_or_fallback(self) -> None:
        fake_sdk = SimpleNamespace()
        constructed: list[dict] = []

        class FakeAnthropic:
            def __init__(self, **kwargs):
                constructed.append(kwargs)
                self.kwargs = kwargs

        fake_sdk.Anthropic = FakeAnthropic
        fake_httpx2 = SimpleNamespace()
        fake_httpx2.Client = MagicMock(return_value=object())

        blocked = {name: None for name in ("httpx",)}
        with patch.dict(sys.modules, {"anthropic": fake_sdk, "httpx2": fake_httpx2, **blocked}):
            with patch.object(
                anthropic_mod, "_resolve_ca_bundle", return_value="/tmp/ca-bundle.crt"
            ):
                client = AnthropicClient(api_key="sk-ant-test")
                sdk_client = client._get_client()

        self.assertEqual(len(constructed), 1)
        self.assertIsInstance(sdk_client, FakeAnthropic)
        fake_httpx2.Client.assert_called_once_with(verify="/tmp/ca-bundle.crt")
        self.assertIn("http_client", constructed[0])

    def test_get_client_without_any_http_library_uses_default(self) -> None:
        fake_sdk = SimpleNamespace()
        constructed: list[dict] = []

        class FakeAnthropic:
            def __init__(self, **kwargs):
                constructed.append(kwargs)

        fake_sdk.Anthropic = FakeAnthropic
        blocked = {name: None for name in ("httpx", "httpx2")}
        with patch.dict(sys.modules, {"anthropic": fake_sdk, **blocked}):
            with patch.object(
                anthropic_mod, "_resolve_ca_bundle", return_value="/tmp/ca-bundle.crt"
            ):
                with self.assertLogs(anthropic_mod.logger, level="WARNING") as logs:
                    client = AnthropicClient(api_key="sk-ant-test")
                    client._get_client()

        self.assertEqual(len(constructed), 1)
        self.assertNotIn("http_client", constructed[0])
        self.assertTrue(any("CA bundle" in line and "ignored" in line for line in logs.output))

    def test_anthropic_module_import_does_not_import_httpx(self) -> None:
        saved_httpx = sys.modules.get("httpx")
        saved_mod = sys.modules.get("amof.orchestrator.llm.anthropic")
        sys.modules["httpx"] = None
        try:
            sys.modules.pop("amof.orchestrator.llm.anthropic", None)
            loaded = importlib.import_module("amof.orchestrator.llm.anthropic")
            self.assertFalse(hasattr(loaded, "httpx"))
            self.assertTrue(hasattr(loaded, "AnthropicClient"))
            self.assertTrue(hasattr(loaded, "_http_client_for_ca_bundle"))
        finally:
            if saved_httpx is None:
                sys.modules.pop("httpx", None)
            else:
                sys.modules["httpx"] = saved_httpx
            if saved_mod is not None:
                sys.modules["amof.orchestrator.llm.anthropic"] = saved_mod


class AnthropicSamplingKwargsCompatTests(unittest.TestCase):
    """anthropic >= 1.3 removed temperature/top_p/top_k from Messages.create()."""

    def _client_with_signature(self, accepts_temperature: bool) -> MagicMock:
        if accepts_temperature:
            def create(*, model, max_tokens, messages, system=None, temperature=None, tools=None, **_):
                return SimpleNamespace(content=[], usage=None)
        else:
            def create(*, model, max_tokens, messages, system=None, tools=None, thinking=None, tool_choice=None):
                return SimpleNamespace(content=[], usage=None)
        client = MagicMock()
        client.messages.create = MagicMock(side_effect=create, wraps=create)
        client.messages.create.__signature__ = __import__("inspect").signature(create)
        return client

    def test_temperature_dropped_when_sdk_rejects_it(self) -> None:
        client = self._client_with_signature(accepts_temperature=False)
        kwargs = {"model": "m", "max_tokens": 5, "messages": [], "temperature": 0.0}
        cleaned = anthropic_mod._drop_unsupported_sampling_kwargs(client, kwargs)
        self.assertNotIn("temperature", cleaned)
        self.assertEqual(cleaned["max_tokens"], 5)
        # Original kwargs untouched; new SDK call would no longer raise TypeError.
        self.assertIn("temperature", kwargs)
        client.messages.create(**cleaned)

    def test_temperature_kept_when_sdk_accepts_it(self) -> None:
        client = self._client_with_signature(accepts_temperature=True)
        kwargs = {"model": "m", "max_tokens": 5, "messages": [], "temperature": 0.2}
        cleaned = anthropic_mod._drop_unsupported_sampling_kwargs(client, kwargs)
        self.assertEqual(cleaned["temperature"], 0.2)

    def test_call_with_retry_survives_new_sdk_signature(self) -> None:
        client = self._client_with_signature(accepts_temperature=False)
        llm = AnthropicClient.__new__(AnthropicClient)
        llm._max_retries = 0
        response = llm._call_with_retry(
            client, {"model": "m", "max_tokens": 5, "messages": [], "temperature": 0.0}
        )
        self.assertEqual(response.content, [])
        _, called_kwargs = client.messages.create.call_args
        self.assertNotIn("temperature", called_kwargs)


class WriteScopePreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.home = Path(self._tmpdir.name)
        self.repo = self.home / "repo"
        self.base_sha = _init_git_repo(self.repo)
        self.target_id = f"local:dx-round3:{self.base_sha}"
        self.env = patch.dict(os.environ, {"AMOF_HOME": str(self.home)}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def _approve_src(self, *, run_id: str = "dx3-run", ttl: str = "2h") -> dict:
        body = {
            "target_id": self.target_id,
            "base_sha": self.base_sha,
            "allowed_roots": ["src/"],
            "denied_roots": [],
            "reason": "src-only fixture",
            "expected_checks": ["git diff --check"],
            "docs_only": False,
            "source_mutation": True,
        }
        outcome = persist_write_scope_proposals_from_result(
            {
                "result_kind": "agent_run_result",
                "contract_version": "agent-run-v1",
                "schema_version": 1,
                "status": "completed",
                "session_id": run_id,
                "exit_code": 0,
                "stop_reason": "completed",
                "final_text": "discovery complete",
                "plan_path": None,
                "checkpoint_path": None,
                "event_log_path": "/tmp/events.jsonl",
                "journal_path": None,
                "budget_summary": {"limit": None, "spent": 0, "remaining": None},
                "write_scope_proposal": body,
            }
        )
        self.assertEqual(len(outcome.persisted), 1)
        return approve_proposal(
            outcome.persisted[0]["proposal_id"],
            ttl=ttl,
            approved_by=OPERATOR,
        )

    def _manifest(self) -> dict:
        return {
            "name": "dx3",
            "source": "appdata",
            "ecosystem": "dx3",
            "repos": [
                {"name": "dx3", "path": str(self.repo), "url": f"local://{self.repo}"}
            ],
        }

    def _run_plan_execute(self, **kwargs):
        planner_factory = MagicMock(name="TaskPlanner")
        env = {
            "AMOF_HOME": str(self.home),
            "ANTHROPIC_API_KEY": "sk-ant-fake",
        }
        captured = StringIO()
        with patch.dict(os.environ, env, clear=False):
            with _cwd(self.repo):
                with patch("amof.orchestrator.planner.TaskPlanner", planner_factory):
                    with redirect_stderr(captured):
                        code = agent_cmd.cmd_agent(
                            self._manifest(),
                            goal='Write src/ok.py only',
                            plan_execute=True,
                            no_follow_up=True,
                            **kwargs,
                        )
        return code, captured.getvalue(), planner_factory

    def test_preflight_passes_correct_flag_set(self) -> None:
        approval = self._approve_src()
        errors = preflight_write_scope_flags(
            write_scope_approval=approval["approval_id"],
            approve_capabilities=["bounded_write"],
            approve_writable_roots=None,
        )
        self.assertEqual(errors, [])
        self.assertEqual(list_bindings(), [])

    def test_write_instead_of_bounded_write_exits_before_planner(self) -> None:
        approval = self._approve_src(run_id="dx3-write")
        code, stderr, planner_factory = self._run_plan_execute(
            write_scope_approval=approval["approval_id"],
            approve_capabilities=["write"],
        )
        self.assertEqual(code, 1)
        self.assertIn("[plan-execute] preflight:", stderr)
        self.assertIn("bounded_write", stderr)
        self.assertNotIn("Planning with", stderr)
        planner_factory.assert_not_called()
        self.assertEqual(list_bindings(), [])

    def test_mixed_writable_root_exits_before_planner(self) -> None:
        approval = self._approve_src(run_id="dx3-mixed")
        code, stderr, planner_factory = self._run_plan_execute(
            write_scope_approval=approval["approval_id"],
            approve_capabilities=["bounded_write"],
            approve_writable_roots=["."],
        )
        self.assertEqual(code, 1)
        self.assertIn("[plan-execute] preflight:", stderr)
        self.assertIn("refuse mixed authority", stderr)
        self.assertNotIn("Planning with", stderr)
        planner_factory.assert_not_called()
        self.assertEqual(list_bindings(), [])

    def test_missing_approval_exits_before_planner(self) -> None:
        code, stderr, planner_factory = self._run_plan_execute(
            write_scope_approval="wsa-doesnotexist",
            approve_capabilities=["bounded_write"],
        )
        self.assertEqual(code, 1)
        self.assertIn("[plan-execute] preflight:", stderr)
        self.assertIn("approval not found", stderr)
        self.assertNotIn("Planning with", stderr)
        planner_factory.assert_not_called()
        self.assertEqual(list_bindings(), [])

    def test_unknown_capability_exits_before_planner(self) -> None:
        approval = self._approve_src(run_id="dx3-unknown")
        code, stderr, planner_factory = self._run_plan_execute(
            write_scope_approval=approval["approval_id"],
            approve_capabilities=["not_a_real_cap"],
        )
        self.assertEqual(code, 1)
        self.assertIn("[plan-execute] preflight:", stderr)
        self.assertIn("Unknown capability", stderr)
        self.assertNotIn("Planning with", stderr)
        planner_factory.assert_not_called()
        self.assertEqual(list_bindings(), [])

    def test_expired_and_revoked_approvals_fail_preflight(self) -> None:
        from datetime import datetime, timedelta, timezone

        from amof.write_scope_approvals import load_approval

        minted = self._approve_src(run_id="dx3-expired", ttl="1s")
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        expired_record = load_approval(
            minted["approval_id"],
            evaluate_ttl=True,
            persist_expiry=False,
            now=future,
        )
        self.assertEqual(expired_record["status"], "expired")
        with patch(
            "amof.write_scope_approvals.load_approval",
            return_value=expired_record,
        ):
            errors = preflight_write_scope_flags(
                write_scope_approval=minted["approval_id"],
                approve_capabilities=["bounded_write"],
            )
        self.assertTrue(any("expired" in line for line in errors), errors)

        live = self._approve_src(run_id="dx3-revoke")
        revoke_approval(
            live["approval_id"],
            reason="dx3 preflight",
            revoked_by=OPERATOR,
        )
        revoked_errors = preflight_write_scope_flags(
            write_scope_approval=live["approval_id"],
            approve_capabilities=["bounded_write"],
        )
        self.assertTrue(any("revoked" in line for line in revoked_errors), revoked_errors)
        self.assertEqual(list_bindings(), [])


BANNED_NOLLM_PHRASE = "No-LLM learning walkthrough"
WALKTHROUGH_DOCS = (
    ROOT / "README.md",
    ROOT / "docs" / "write-scope-authority.md",
    ROOT / "docs" / "runbooks" / "happy-path-agent-workflow.md",
)


class LearningWalkthroughDocTruthTests(unittest.TestCase):
    def test_no_llm_learning_walkthrough_phrase_is_gone(self) -> None:
        for path in WALKTHROUGH_DOCS:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(BANNED_NOLLM_PHRASE, text, path)
            self.assertIn("Learning walkthrough (fixture, not evidence)", text, path)
            self.assertIn("no Binding is created", text, path)

        for topic in ("scope", "execute", "agent"):
            with redirect_stdout(StringIO()) as stdout:
                code = cmd_help(topic)
            self.assertEqual(code, 0, topic)
            help_text = stdout.getvalue()
            collapsed = " ".join(help_text.split())
            self.assertNotIn(BANNED_NOLLM_PHRASE, help_text, topic)
            self.assertIn("no Binding is created", collapsed, topic)


if __name__ == "__main__":
    unittest.main()

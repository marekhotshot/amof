from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover - optional runtime dependency
    TestClient = None

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

if TestClient is not None:
    from amof.api.dependencies import require_step_up_user
    from amof.api.main import app
    from amof.api.services import repo_adoption_service
    from amof.orchestrator.llm.base import StructuredLLMResponse, Usage
else:  # pragma: no cover - no fastapi runtime available
    require_step_up_user = None
    app = None
    repo_adoption_service = None
    StructuredLLMResponse = None
    Usage = None


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


def _git(path: Path | None, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(path) if path is not None else None,
        check=True,
        capture_output=True,
        text=True,
        env=_commit_env(),
    )
    return completed.stdout.strip()


def _create_workspace_with_governed_repo() -> tuple[Path, Path]:
    root = Path(tempfile.mkdtemp(prefix="amof-repo-adoption-api-"))
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "ecosystems" / "demo").mkdir(parents=True, exist_ok=True)
    (workspace / "repos").mkdir(parents=True, exist_ok=True)
    (workspace / "ecosystems" / "demo" / "ecosystem.yaml").write_text(
        (
            "name: demo\n"
            "repos:\n"
            "  - name: source\n"
            f"    url: \"{(root / 'source.git').as_posix()}\"\n"
            "    path: repos/source\n"
            "    readonly: true\n"
            "    enabled: true\n"
        ),
        encoding="utf-8",
    )

    remote = root / "source.git"
    seed = root / "seed"
    source = workspace / "repos" / "source"
    _git(None, "init", "--bare", str(remote))
    _git(None, "init", "-b", "main", str(seed))
    (seed / "README.md").write_text("# source\n", encoding="utf-8")
    commands_dir = seed / "scripts" / "amof" / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)
    (commands_dir / "chat.py").write_text("def plan_read_only_chat():\n    return 'plan'\n", encoding="utf-8")
    (seed / "pyproject.toml").write_text("[project]\nname = 'source'\nversion = '0.1.0'\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "test: seed repo")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")
    _git(None, "clone", str(remote), str(source))
    return workspace, source


class _FakeRemoteIALClient:
    def __init__(self, analysis_payload: dict[str, object]) -> None:
        self.analysis_payload = analysis_payload

    def model_name(self) -> str:
        return "remote-ial/test-model"

    def chat_structured(self, system: str, messages, response_model, **kwargs):  # type: ignore[no-untyped-def]
        lowered = system.lower()
        if "codebase analyst" in lowered:
            payload = {
                "summary": "Source repo planning context.",
                "architecture": "CLI commands and orchestration modules.",
                "files": {
                    "repos/source/scripts/amof/commands/chat.py": {
                        "purpose": "chat planning command",
                        "classes": [],
                        "functions": [{"name": "plan_read_only_chat", "description": "plans chat work"}],
                        "complexity": "medium",
                    },
                    "repos/source/README.md": {
                        "purpose": "project overview",
                        "classes": [],
                        "functions": [],
                        "complexity": "low",
                    },
                },
                "dependency_graph": {
                    "repos/source/scripts/amof/commands/chat.py": ["repos/source/README.md"],
                },
                "entry_points": ["repos/source/scripts/amof/commands/chat.py"],
                "key_abstractions": [{"name": "PlanPacket", "description": "proposal-only output"}],
            }
        elif "incremental" in lowered:
            payload = {
                "files": {
                    "repos/source/README.md": {
                        "purpose": "project overview",
                        "classes": [],
                        "functions": [],
                        "complexity": "low",
                    }
                },
                "dependency_graph_updates": {
                    "repos/source/scripts/amof/commands/chat.py": ["repos/source/README.md"],
                    "repos/source/README.md": [],
                },
            }
        else:
            payload = self.analysis_payload
        parsed = response_model.model_validate(payload)
        usage = Usage(
            model="remote-ial/test-model",
            prompt_tokens=120,
            completion_tokens=60,
            latency_ms=10,
            estimated_cost=0.02,
            provider="remote-ial",
            request_id="req-repo-adoption-001",
            input_hash="input-hash",
            output_hash="output-hash",
            cost_status="observed",
            cost_observed=True,
        )
        return StructuredLLMResponse(
            parsed=parsed,
            usage=usage,
            text=json.dumps(payload),
            raw=payload,
        )


class RepoAdoptionAnalysisApiTests(unittest.TestCase):
    @unittest.skipIf(TestClient is None, "fastapi is not installed")
    def test_control_repo_adoption_analysis_returns_bounded_response(self) -> None:
        assert TestClient is not None
        assert app is not None
        assert require_step_up_user is not None
        assert repo_adoption_service is not None
        workspace, _source = _create_workspace_with_governed_repo()
        amof_home = workspace / ".amof-home"
        fake_client = _FakeRemoteIALClient(
            {
                "overall_status": "inferred",
                "repository_summary": {"text": "Source is a bounded Python CLI repo with planning-oriented modules."},
                "runtime_facts": [
                    {
                        "fact": "stack",
                        "value": ["Python"],
                        "status": "inferred",
                        "source": "repo_adoption_inference",
                    },
                    {
                        "fact": "package_manager",
                        "value": ["pip"],
                        "status": "inferred",
                        "source": "planning_context",
                    },
                ],
                "blockers": [
                    {
                        "message": "No validated runtime execution evidence is available yet.",
                        "status": "blocked",
                        "source": "planning_context",
                    }
                ],
                "recommended_tickets": [
                    {
                        "title": "Document the first bounded replay slice",
                        "severity": "medium",
                        "lane": "replay-now",
                        "expected_impact": "Clarifies the first governed adoption step.",
                    }
                ],
                "recommended_next_action": "Approve the first replay-now adoption ticket.",
            }
        )
        app.dependency_overrides[require_step_up_user] = lambda: {"id": "test-user"}
        try:
            with unittest.mock.patch.dict(
                os.environ,
                {"AMOF_HOME": str(amof_home), "AMOF_WORKSPACE_ROOT": str(workspace)},
                clear=False,
            ):
                with unittest.mock.patch.object(
                    repo_adoption_service,
                    "_build_remote_ial_client",
                    return_value=fake_client,
                ):
                    client = TestClient(app)
                    response = client.post(
                        "/api/v1/control/repo-adoption/analyses",
                        json={
                            "repository": {
                                "kind": "ecosystem_repo",
                                "ecosystem": "demo",
                                "repo_name": "source",
                            },
                            "max_recommended_tickets": 5,
                        },
                    )
                    public_response = client.post(
                        "/api/v1/repo-adoption/analyses",
                        json={
                            "repository": {
                                "kind": "ecosystem_repo",
                                "ecosystem": "demo",
                                "repo_name": "source",
                            }
                        },
                    )
        finally:
            app.dependency_overrides.pop(require_step_up_user, None)

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(public_response.status_code, 404)
        self.assertEqual(payload["repository"]["ecosystem"], "demo")
        self.assertEqual(payload["repository"]["repo_name"], "source")
        self.assertEqual(payload["overall_status"], "inferred")
        self.assertLessEqual(len(payload["recommended_tickets"]), 5)
        self.assertTrue(payload["recommended_next_action"])
        self.assertEqual(payload["references"]["run_id"], payload["analysis_id"])
        self.assertEqual(payload["references"]["request_id"], "req-repo-adoption-001")
        self.assertEqual(payload["references"]["planning_context"]["kind"], "planning_context")
        rendered = json.dumps(payload, sort_keys=True)
        self.assertNotIn(str(workspace), rendered)
        self.assertNotIn("/repos/source", rendered)

    @unittest.skipIf(TestClient is None, "fastapi is not installed")
    def test_control_repo_adoption_analysis_returns_not_found_for_unknown_repo(self) -> None:
        assert TestClient is not None
        assert app is not None
        assert require_step_up_user is not None
        workspace, _source = _create_workspace_with_governed_repo()
        amof_home = workspace / ".amof-home"
        app.dependency_overrides[require_step_up_user] = lambda: {"id": "test-user"}
        try:
            with unittest.mock.patch.dict(
                os.environ,
                {"AMOF_HOME": str(amof_home), "AMOF_WORKSPACE_ROOT": str(workspace)},
                clear=False,
            ):
                client = TestClient(app)
                response = client.post(
                    "/api/v1/control/repo-adoption/analyses",
                    json={
                        "repository": {
                            "kind": "ecosystem_repo",
                            "ecosystem": "demo",
                            "repo_name": "missing",
                        }
                    },
                )
        finally:
            app.dependency_overrides.pop(require_step_up_user, None)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["code"], "repo_adoption_repo_not_found")

    @unittest.skipIf(TestClient is None, "fastapi is not installed")
    def test_control_repo_adoption_analysis_rejects_excess_tickets_from_upstream(self) -> None:
        assert TestClient is not None
        assert app is not None
        assert require_step_up_user is not None
        assert repo_adoption_service is not None
        workspace, _source = _create_workspace_with_governed_repo()
        amof_home = workspace / ".amof-home"
        fake_client = _FakeRemoteIALClient(
            {
                "overall_status": "inferred",
                "repository_summary": {"text": "Too many tickets."},
                "runtime_facts": [],
                "blockers": [],
                "recommended_tickets": [
                    {
                        "title": f"Ticket {idx}",
                        "severity": "low",
                        "lane": "defer",
                        "expected_impact": "bounded",
                    }
                    for idx in range(6)
                ],
                "recommended_next_action": "Pick one ticket.",
            }
        )
        app.dependency_overrides[require_step_up_user] = lambda: {"id": "test-user"}
        try:
            with unittest.mock.patch.dict(
                os.environ,
                {"AMOF_HOME": str(amof_home), "AMOF_WORKSPACE_ROOT": str(workspace)},
                clear=False,
            ):
                with unittest.mock.patch.object(
                    repo_adoption_service,
                    "_build_remote_ial_client",
                    return_value=fake_client,
                ):
                    client = TestClient(app)
                    response = client.post(
                        "/api/v1/control/repo-adoption/analyses",
                        json={
                            "repository": {
                                "kind": "ecosystem_repo",
                                "ecosystem": "demo",
                                "repo_name": "source",
                            },
                            "max_recommended_tickets": 5,
                        },
                    )
        finally:
            app.dependency_overrides.pop(require_step_up_user, None)

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"]["code"], "repo_adoption_ticket_limit_exceeded")


if __name__ == "__main__":
    unittest.main()

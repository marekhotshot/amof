from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
AMOF_SCRIPT = ROOT / "scripts" / "amof.py"
SCRIPTS_ROOT = ROOT / "scripts"


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
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True, text=True)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "commit", "-m", "test: init"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        env=_commit_env(),
    )


def _amof_env(workspace_root: Path, amof_home: Path) -> dict[str, str]:
    env = dict(os.environ)
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(SCRIPTS_ROOT)
        if not existing_pythonpath
        else os.pathsep.join([str(SCRIPTS_ROOT), existing_pythonpath])
    )
    env["AMOF_HOME"] = str(amof_home)
    env["AMOF_WORKSPACE_ROOT"] = str(workspace_root)
    env["AMOF_CWD"] = str(workspace_root)
    env.update(_commit_env())
    return env


def _run_amof(workspace_root: Path, amof_home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(AMOF_SCRIPT), *args],
        cwd=workspace_root,
        env=_amof_env(workspace_root, amof_home),
        capture_output=True,
        text=True,
        check=False,
    )


def _write_workspace_manifest(workspace_root: Path, *, repo_name: str = "amof") -> None:
    ecosystem_dir = workspace_root / "ecosystems" / "demo"
    ecosystem_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "ecosystem": "demo",
        "workspace": {"repo_branch_prefix": "feature"},
        "repos": [
            {
                "name": repo_name,
                "url": "https://github.com/marekhotshot/amof.git",
                "path": f"repos/{repo_name}",
                "branch": "main",
                "readonly": False,
            }
        ],
    }
    (ecosystem_dir / "ecosystem.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False),
        encoding="utf-8",
    )


def _write_workspace_state(amof_home: Path, *, repo_name: str = "amof") -> None:
    config_root = amof_home / "config"
    config_root.mkdir(parents=True, exist_ok=True)
    state = {
        "version": 3,
        "ecosystem": "demo",
        "workspace_branch": "workspace/demo",
        "active_ticket": None,
        "tickets": {},
        "repos": [
            {
                "name": repo_name,
                "url": "https://github.com/marekhotshot/amof.git",
                "path": f"repos/{repo_name}",
                "branch": "main",
                "readonly": False,
                "enabled": True,
            }
        ],
    }
    (config_root / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")


def _create_workspace(*, repo_name: str = "amof", canonical_remote: bool = True) -> tuple[Path, Path, Path]:
    temp_root = Path(tempfile.mkdtemp(prefix="amof-ticket-protocol-"))
    workspace_root = temp_root / "workspace"
    amof_home = temp_root / ".amof-home"
    seed_repo = temp_root / "seed-amof"
    bare_remote = temp_root / "origin-amof.git"
    _init_git_repo(seed_repo)
    subprocess.run(
        ["git", "clone", "--bare", str(seed_repo), str(bare_remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    repo_path = workspace_root / "repos" / repo_name
    repo_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", str(bare_remote), str(repo_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    remote_url = "https://github.com/marekhotshot/amof.git" if canonical_remote else "https://github.com/marekhotshot/amof-oss.git"
    subprocess.run(
        ["git", "-C", str(repo_path), "remote", "set-url", "origin", remote_url],
        check=True,
        capture_output=True,
        text=True,
    )
    _write_workspace_manifest(workspace_root, repo_name=repo_name)
    _write_workspace_state(amof_home, repo_name=repo_name)
    return workspace_root, amof_home, repo_path


def _default_plan_items_json() -> str:
    validation_command = f"{sys.executable} -c \"from pathlib import Path; assert Path('README.md').exists()\""
    return json.dumps(
        [
            {
                "id": "P1",
                "type": "WIRING",
                "title": "Update ticket README evidence",
                "expected_files": ["README.md"],
                "validation": [validation_command],
                "checkpoint_required": True,
            }
        ]
    )


def _ticket_worktree_path(amof_home: Path, ticket_id: str, repo_name: str = "amof") -> Path:
    return amof_home / "share" / "workspaces" / "ticket-worktrees" / ticket_id / repo_name


class TicketDeliveryProtocolTests(unittest.TestCase):
    def test_preflight_passes_for_canonical_amof_remote(self) -> None:
        workspace_root, amof_home, _ = _create_workspace()
        result = _run_amof(
            workspace_root,
            amof_home,
            "-e",
            "demo",
            "ticket",
            "preflight",
            "AMOF-280",
            "--json",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["allowed"])
        self.assertEqual(payload["repo_checks"][0]["repo"], "amof")
        self.assertTrue(payload["repo_checks"][0]["canonical_remote"])

    def test_preflight_blocks_wrong_remote_for_amof(self) -> None:
        workspace_root, amof_home, _ = _create_workspace(canonical_remote=False)
        result = _run_amof(
            workspace_root,
            amof_home,
            "-e",
            "demo",
            "ticket",
            "preflight",
            "AMOF-280",
            "--json",
        )

        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["allowed"])
        self.assertIn("must resolve to https://github.com/marekhotshot/amof.git", payload["blocking_issues"][0])

    def test_start_rejects_dirty_canonical_repo(self) -> None:
        workspace_root, amof_home, repo_path = _create_workspace()
        (repo_path / "DIRTY.txt").write_text("dirty\n", encoding="utf-8")

        result = _run_amof(
            workspace_root,
            amof_home,
            "-e",
            "demo",
            "ticket",
            "start",
            "AMOF-280",
            "--plan-items-json",
            _default_plan_items_json(),
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("Dirty: yes", result.stdout)
        self.assertIn("repo has unrelated local changes", result.stdout)

    def test_status_reports_incomplete_plan_items_and_planner_provenance(self) -> None:
        workspace_root, amof_home, _ = _create_workspace()
        start = _run_amof(
            workspace_root,
            amof_home,
            "-e",
            "demo",
            "ticket",
            "start",
            "AMOF-280",
            "--plan-items-json",
            _default_plan_items_json(),
            "--planner-profile",
            "remote-ial-default",
            "--planner-model",
            "remote-ial/default",
        )
        self.assertEqual(start.returncode, 0, start.stderr)

        status = _run_amof(
            workspace_root,
            amof_home,
            "-e",
            "demo",
            "ticket",
            "status",
            "AMOF-280",
            "--json",
        )

        self.assertEqual(status.returncode, 0, status.stderr)
        payload = json.loads(status.stdout)
        self.assertEqual(payload["phase"], "started")
        self.assertFalse(payload["promote_main_ready"])
        self.assertEqual(payload["plan_items"][0]["status"], "pending")
        self.assertEqual(payload["planner_provenance"]["profile_name"], "remote-ial-default")
        self.assertEqual(payload["planner_provenance"]["resolved_model"], "remote-ial/default")

    def test_checkpoint_rejects_unrelated_dirty_files(self) -> None:
        workspace_root, amof_home, _ = _create_workspace()
        start = _run_amof(
            workspace_root,
            amof_home,
            "-e",
            "demo",
            "ticket",
            "start",
            "AMOF-280",
            "--plan-items-json",
            _default_plan_items_json(),
        )
        self.assertEqual(start.returncode, 0, start.stderr)
        worktree = _ticket_worktree_path(amof_home, "AMOF-280")
        (worktree / "README.md").write_text("updated\n", encoding="utf-8")
        (worktree / "EXTRA.md").write_text("noise\n", encoding="utf-8")

        checkpoint = _run_amof(
            workspace_root,
            amof_home,
            "-e",
            "demo",
            "ticket",
            "checkpoint",
            "AMOF-280",
            "--repo",
            "amof",
            "--plan-item",
            "P1",
            "--file",
            "README.md",
            "--message",
            "update readme",
        )

        self.assertEqual(checkpoint.returncode, 1)
        self.assertIn("unrelated dirty files present: EXTRA.md", checkpoint.stderr)

    def test_checkpoint_requires_plan_item_ids_and_validation_then_marks_ready(self) -> None:
        workspace_root, amof_home, _ = _create_workspace()
        start = _run_amof(
            workspace_root,
            amof_home,
            "-e",
            "demo",
            "ticket",
            "start",
            "AMOF-280",
            "--plan-items-json",
            _default_plan_items_json(),
        )
        self.assertEqual(start.returncode, 0, start.stderr)
        worktree = _ticket_worktree_path(amof_home, "AMOF-280")
        (worktree / "README.md").write_text("updated\n", encoding="utf-8")

        checkpoint = _run_amof(
            workspace_root,
            amof_home,
            "-e",
            "demo",
            "ticket",
            "checkpoint",
            "AMOF-280",
            "--repo",
            "amof",
            "--plan-item",
            "P1",
            "--file",
            "README.md",
            "--message",
            "update readme",
        )
        self.assertEqual(checkpoint.returncode, 0, checkpoint.stderr)
        self.assertIn("[AMOF-280][P1] update readme", checkpoint.stdout)

        status = _run_amof(
            workspace_root,
            amof_home,
            "-e",
            "demo",
            "ticket",
            "status",
            "AMOF-280",
            "--json",
        )
        payload = json.loads(status.stdout)
        self.assertEqual(payload["phase"], "ready_for_promote")
        self.assertTrue(payload["promote_main_ready"])
        self.assertEqual(payload["plan_items"][0]["status"], "done")

    def test_multiple_tickets_keep_isolated_branch_and_worktree_identity(self) -> None:
        workspace_root, amof_home, _ = _create_workspace()
        for ticket_id in ("AMOF-280", "AMOF-281"):
            result = _run_amof(
                workspace_root,
                amof_home,
                "-e",
                "demo",
                "ticket",
                "start",
                ticket_id,
                "--plan-items-json",
                _default_plan_items_json(),
            )
            self.assertEqual(result.returncode, 0, result.stderr)

        state_path = amof_home / "config" / "state.json"
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        ticket_280 = payload["tickets"]["AMOF-280"]
        ticket_281 = payload["tickets"]["AMOF-281"]
        self.assertEqual(ticket_280["repos"]["amof"], "feature/AMOF-280")
        self.assertEqual(ticket_281["repos"]["amof"], "feature/AMOF-281")
        self.assertNotEqual(ticket_280["worktree_base"], ticket_281["worktree_base"])


if __name__ == "__main__":
    unittest.main()

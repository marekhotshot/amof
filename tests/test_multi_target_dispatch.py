"""Multi-target dispatch contract tests.

When the operator confirms several target repositories, the agent must see
every materialized checkout (readable root spans the whole workspace), may
emit one write-scope proposal block per target, and changed-path collection
must cover every target repository — not just the first manifest entry.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.amof.commands import handoff as handoff_cmd
from scripts.amof.execution_backends import hermes_opensandbox


SHA_A = "a" * 40
SHA_B = "b" * 40


def _proposal_block(target_id: str, base_sha: str, allowed_roots: list[str]) -> str:
    proposal = {
        "target_id": target_id,
        "base_sha": base_sha,
        "allowed_roots": allowed_roots,
        "denied_roots": [],
        "reason": "bounded follow-up justified by inspected evidence",
        "expected_checks": ["git diff --check"],
        "docs_only": False,
        "source_mutation": True,
    }
    return (
        f"{hermes_opensandbox.WRITE_SCOPE_PROPOSAL_START}\n"
        f"{json.dumps(proposal)}\n"
        f"{hermes_opensandbox.WRITE_SCOPE_PROPOSAL_END}"
    )


def _init_repo(path: Path, tracked_name: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / tracked_name).write_text("original\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=test",
            "commit",
            "-q",
            "-m",
            "init",
        ],
        check=True,
    )


class MultiProposalExtractionTests(unittest.TestCase):
    def test_one_block_per_target_extracts_every_proposal(self) -> None:
        text = "\n".join(
            [
                _proposal_block("github_app:o/repo-a:" + SHA_A, SHA_A, ["src/lib/a.ts"]),
                _proposal_block("github_app:o/repo-b:" + SHA_B, SHA_B, ["services/b/"]),
                "",
                "# Findings",
                "Both repositories require bounded changes.",
            ]
        )
        proposals, summary = hermes_opensandbox._extract_write_scope_proposal_outputs(text)
        self.assertEqual(len(proposals), 2)
        self.assertEqual(proposals[0]["target_id"], "github_app:o/repo-a:" + SHA_A)
        self.assertEqual(proposals[1]["target_id"], "github_app:o/repo-b:" + SHA_B)
        self.assertIn("Both repositories require bounded changes.", summary)
        self.assertNotIn(hermes_opensandbox.WRITE_SCOPE_PROPOSAL_START, summary)

    def test_duplicate_target_blocks_keep_first(self) -> None:
        text = "\n".join(
            [
                _proposal_block("github_app:o/repo-a:" + SHA_A, SHA_A, ["src/first.ts"]),
                _proposal_block("github_app:o/repo-a:" + SHA_A, SHA_A, ["src/second.ts"]),
            ]
        )
        proposals, _ = hermes_opensandbox._extract_write_scope_proposal_outputs(text)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["allowed_roots"], ["src/first.ts"])

    def test_singular_wrapper_returns_first_proposal(self) -> None:
        text = "\n".join(
            [
                _proposal_block("github_app:o/repo-a:" + SHA_A, SHA_A, ["src/lib/a.ts"]),
                _proposal_block("github_app:o/repo-b:" + SHA_B, SHA_B, ["services/b/"]),
            ]
        )
        proposal, _ = hermes_opensandbox._extract_write_scope_proposal_output(text)
        self.assertIsNotNone(proposal)
        self.assertEqual(proposal["target_id"], "github_app:o/repo-a:" + SHA_A)

    def test_expected_roots_accept_per_target_subsets(self) -> None:
        # A multi-target mission names one explicit path per repository, so the
        # expected roots are the UNION while each proposal carries only its
        # own repository's slice. Both blocks must survive validation.
        expected = ["docs/proof-a.md", "docs/proof-b.md"]
        text = "\n".join(
            [
                _proposal_block("github_app:o/repo-a:" + SHA_A, SHA_A, ["docs/proof-a.md"]),
                _proposal_block("github_app:o/repo-b:" + SHA_B, SHA_B, ["docs/proof-b.md"]),
            ]
        )
        proposals, _ = hermes_opensandbox._extract_write_scope_proposal_outputs(
            text, expected_allowed_roots=expected
        )
        self.assertEqual(len(proposals), 2)
        self.assertEqual(proposals[0]["allowed_roots"], ["docs/proof-a.md"])
        self.assertEqual(proposals[1]["allowed_roots"], ["docs/proof-b.md"])

    def test_expected_roots_still_reject_out_of_scope_paths(self) -> None:
        expected = ["docs/proof-a.md", "docs/proof-b.md"]
        text = _proposal_block(
            "github_app:o/repo-a:" + SHA_A, SHA_A, ["docs/proof-a.md", "src/evil.ts"]
        )
        proposals, _ = hermes_opensandbox._extract_write_scope_proposal_outputs(
            text, expected_allowed_roots=expected
        )
        self.assertEqual(proposals, [])

    def test_invalid_block_is_skipped_but_valid_blocks_survive(self) -> None:
        invalid = (
            f"{hermes_opensandbox.WRITE_SCOPE_PROPOSAL_START}\n"
            "{\"target_id\": \"broken\"}\n"
            f"{hermes_opensandbox.WRITE_SCOPE_PROPOSAL_END}"
        )
        text = "\n".join(
            [invalid, _proposal_block("github_app:o/repo-b:" + SHA_B, SHA_B, ["services/b/"])]
        )
        proposals, _ = hermes_opensandbox._extract_write_scope_proposal_outputs(text)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["target_id"], "github_app:o/repo-b:" + SHA_B)


class MultiTargetPromptTests(unittest.TestCase):
    def _manifest(self) -> dict[str, object]:
        return {
            "repos": [
                {
                    "name": "repo-a",
                    "url": "https://github.com/o/repo-a.git",
                    "path": "/ws/00-repo-a",
                    "target_id": "github_app:o/repo-a:" + SHA_A,
                    "sha": SHA_A,
                },
                {
                    "name": "repo-b",
                    "url": "https://github.com/o/repo-b.git",
                    "path": "/ws/01-repo-b",
                    "target_id": "github_app:o/repo-b:" + SHA_B,
                    "sha": SHA_B,
                },
            ]
        }

    def _selection(self) -> hermes_opensandbox.HermesBackendSelection:
        return hermes_opensandbox.HermesBackendSelection(
            runner_id="hermes-local-ticket-write",
            capabilities=["read"],
            writable_roots=[],
            timeout_seconds=900,
            readable_root=None,
        )

    def test_prompt_enumerates_every_target_repository(self) -> None:
        prompt = hermes_opensandbox._build_prompt(
            "Inspect and return a structured write_scope_proposal for the required change.",
            self._selection(),
            Path("/ws"),
            self._manifest(),
        )
        self.assertIn("Target repositories (2)", prompt)
        self.assertIn("tool_root=00-repo-a", prompt)
        self.assertIn("tool_root=01-repo-b", prompt)
        self.assertNotIn("at /ws/00-repo-a", prompt)
        self.assertNotIn("at /ws/01-repo-b", prompt)
        self.assertIn("github_app:o/repo-a:" + SHA_A, prompt)
        self.assertIn("github_app:o/repo-b:" + SHA_B, prompt)
        self.assertIn("for EACH target repository", prompt)
        self.assertIn("one entry per target", prompt)
        self.assertIn("repository-relative", prompt)

    def test_job_sandbox_absolute_paths_become_relative_tool_roots(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-tool-roots-") as td:
            workspace = Path(td)
            _init_repo(workspace / "00-amof", "README.md")
            _init_repo(workspace / "01-amof-private", "README.md")
            manifest = {
                "repos": [
                    {
                        "name": "amof",
                        "url": "https://github.com/marekhotshot/amof.git",
                        "path": "/run-work/files",
                        "target_id": "github_app:marekhotshot/amof:" + SHA_A,
                        "sha": SHA_A,
                    },
                    {
                        "name": "amof-private",
                        "url": "https://github.com/marekhotshot/amof-private.git",
                        "path": "/run-work/targets/github_app-marekhotshot-amof-private-e6fcb2e0fd5",
                        "target_id": "github_app:marekhotshot/amof-private:" + SHA_B,
                        "sha": SHA_B,
                    },
                ]
            }
            prompt = hermes_opensandbox._build_prompt(
                "Inspect both targets and return the smoke result.",
                self._selection(),
                workspace,
                manifest,
            )
            self.assertIn("tool_root=00-amof", prompt)
            self.assertIn("tool_root=01-amof-private", prompt)
            self.assertNotIn("/run-work/files", prompt)
            self.assertNotIn("/run-work/targets/", prompt)
            self.assertIn("Do not pass absolute sandbox or host paths to tools", prompt)

    def test_single_target_prompt_keeps_exactly_one_block_contract(self) -> None:
        manifest = {"repos": [self._manifest()["repos"][0]]}
        prompt = hermes_opensandbox._build_prompt(
            "Inspect and return a structured write_scope_proposal for the required change.",
            self._selection(),
            Path("/ws/00-repo-a"),
            manifest,
        )
        self.assertIn("exactly one non-empty JSON object", prompt)
        self.assertNotIn("Target repositories (", prompt)


class MultiRepoWorkspaceScanTests(unittest.TestCase):
    def test_changed_paths_cover_every_child_repository(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-multi-ws-") as td:
            workspace = Path(td)
            repo_a = workspace / "00-repo-a"
            repo_b = workspace / "01-repo-b"
            _init_repo(repo_a, "a.txt")
            _init_repo(repo_b, "b.txt")
            (repo_a / "a.txt").write_text("changed\n", encoding="utf-8")
            (repo_b / "new-file.txt").write_text("added\n", encoding="utf-8")

            changed = hermes_opensandbox._changed_paths(workspace)
            self.assertEqual(sorted(changed), ["a.txt", "new-file.txt"])

    def test_restore_read_only_paths_restores_in_the_owning_repository(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amof-multi-restore-") as td:
            workspace = Path(td)
            repo_a = workspace / "00-repo-a"
            repo_b = workspace / "01-repo-b"
            _init_repo(repo_a, "a.txt")
            _init_repo(repo_b, "b.txt")
            (repo_a / "a.txt").write_text("mutated\n", encoding="utf-8")
            (repo_b / "extra.txt").write_text("untracked\n", encoding="utf-8")

            restored = hermes_opensandbox._restore_read_only_paths(
                workspace, ["a.txt", "extra.txt"]
            )
            self.assertEqual(sorted(restored), ["a.txt", "extra.txt"])
            self.assertEqual((repo_a / "a.txt").read_text(encoding="utf-8"), "original\n")
            self.assertFalse((repo_b / "extra.txt").exists())
            # b.txt in repo-b was clean and must remain untouched.
            self.assertEqual((repo_b / "b.txt").read_text(encoding="utf-8"), "original\n")


class MultiTargetReadableRootTests(unittest.TestCase):
    def _readable_root(self, manifest: dict[str, object]) -> str | None:
        captured: dict[str, object] = {}

        class _FakeBackend:
            @staticmethod
            def build_selection(**kwargs: object) -> object:
                captured.update(kwargs)
                raise _StopDispatch()

            @staticmethod
            def run(**kwargs: object) -> dict[str, object]:  # pragma: no cover
                raise AssertionError("run must not be reached")

        class _StopDispatch(Exception):
            pass

        class _Args:
            runner_timeout_seconds = None
            approve_capabilities = None
            approve_writable_roots = None

        class _Packet:
            handoff_id = "handoff-test"
            studio_session_id = "studio-test"

            class payload:  # noqa: D106 - minimal stand-in
                text = "goal"

        try:
            handoff_cmd._dispatch_backend_handoff(
                args=_Args(),
                packet=_Packet(),
                manifest=manifest,
                runner_record={
                    "runner_id": "hermes-local-ticket-write",
                    "execution": {"max_runtime_seconds": 900},
                },
                request_payload={"goal": "goal", "request_id": "req"},
                backend_module=_FakeBackend,
            )
        except _StopDispatch:
            pass
        return captured.get("readable_root")  # type: ignore[return-value]

    def test_multi_repo_manifest_uses_common_workspace_parent(self) -> None:
        manifest = {
            "repos": [
                {"path": "/ws/ws-123/00-repo-a"},
                {"path": "/ws/ws-123/01-repo-b"},
            ]
        }
        self.assertEqual(self._readable_root(manifest), "/ws/ws-123")

    def test_single_repo_manifest_keeps_repo_path(self) -> None:
        manifest = {"repos": [{"path": "/ws/ws-123/00-repo-a"}]}
        self.assertEqual(self._readable_root(manifest), "/ws/ws-123/00-repo-a")


if __name__ == "__main__":
    unittest.main()

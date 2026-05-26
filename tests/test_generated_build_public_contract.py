from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = REPO_ROOT / "scripts"

if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from amof.generated_build.admission import evaluate_admission
from amof.generated_build.candidate import list_candidates, load_candidate, promote_candidate
from amof.generated_build.release_admission import evaluate_release_admission_preview


class GeneratedBuildPublicContractTests(unittest.TestCase):
    def test_admission_preview_returns_fail_closed_public_envelope(self) -> None:
        artifact = {
            "status": "runtime_proven",
            "source_repo": {"host_path": "/tmp/example"},
            "service": "web",
            "build_proof": {"image_digest": "sha256:" + ("a" * 64)},
            "runtime_proof": {"observed": True},
            "dockerfile_" + "template": {"id": "internal-public-test"},
            "risk_" + "flags": ["internal-public-test"],
        }

        result = evaluate_admission(artifact, artifact_path="/tmp/artifact.json")
        encoded = json.dumps(result, sort_keys=True)

        self.assertEqual(result["admission_status"], "refused")
        self.assertEqual(result["precedence_decision"], "not_evaluated")
        self.assertIn("public_contract_only", result["reasons"])
        self.assertNotIn("internal-public-test", encoded)

    def test_candidate_promotion_is_callable_and_does_not_write_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            old_root = os.environ.get("AMOF_GENERATED_BUILDS_ROOT")
            os.environ["AMOF_GENERATED_BUILDS_ROOT"] = tmpdir
            try:
                result = promote_candidate(
                    {
                        "artifact_ref": {
                            "artifact_path": "/tmp/artifact.json",
                            "repo_path": "/tmp/example",
                            "service": "web",
                            "image_digest": "sha256:" + ("b" * 64),
                        },
                        "target_ecosystem": "example",
                        "target_service": "web",
                    }
                )
                self.assertEqual(result["result"], "refused")
                self.assertFalse((Path(tmpdir) / "candidates").exists())
            finally:
                if old_root is None:
                    os.environ.pop("AMOF_GENERATED_BUILDS_ROOT", None)
                else:
                    os.environ["AMOF_GENERATED_BUILDS_ROOT"] = old_root

    def test_candidate_reads_return_sanitized_public_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            old_root = os.environ.get("AMOF_GENERATED_BUILDS_ROOT")
            os.environ["AMOF_GENERATED_BUILDS_ROOT"] = tmpdir
            try:
                records_dir = Path(tmpdir) / "candidates" / "records"
                records_dir.mkdir(parents=True)
                record_path = records_dir / "candidate-1.json"
                record_path.write_text(
                    json.dumps(
                        {
                            "candidate_id": "candidate-1",
                            "status": "candidate_only",
                            "target_ecosystem": "example",
                            "target_service": "web",
                            "image_digest": "sha256:" + ("c" * 64),
                            "created_at": "2026-01-01T00:00:00Z",
                            "artifact_ref": {"repo_path": "/tmp/example", "service": "web"},
                            "admission_policy_result": {"details": "internal-public-test"},
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )

                listed = list_candidates()
                loaded = load_candidate("candidate-1")
                self.assertEqual(len(listed["items"]), 1)
                self.assertNotIn("internal-public-test", json.dumps(listed, sort_keys=True))
                self.assertNotIn("internal-public-test", json.dumps(loaded, sort_keys=True))
                self.assertTrue(loaded["public_contract_only"])
            finally:
                if old_root is None:
                    os.environ.pop("AMOF_GENERATED_BUILDS_ROOT", None)
                else:
                    os.environ["AMOF_GENERATED_BUILDS_ROOT"] = old_root

    def test_release_preview_returns_fail_closed_public_envelope(self) -> None:
        result = evaluate_release_admission_preview(
            {
                "candidate_id": "candidate-1",
                "status": "candidate_only",
                "artifact_ref": {"repo_path": "/tmp/example", "service": "web"},
                "target_ecosystem": "example",
                "target_service": "web",
                "image_digest": "sha256:" + ("d" * 64),
                "admission_policy_result": {"admission_status": "candidate_only"},
            },
            artifact={
                "status": "runtime_proven",
                "build_proof": {"image_digest": "sha256:" + ("d" * 64)},
                "runtime_proof": {"observed": True},
            },
        )

        self.assertEqual(result["release_admission_preview_status"], "unavailable")
        self.assertFalse(result["would_create_release_admission"])
        self.assertFalse(result["would_create_deploy_admission"])
        self.assertIn("public_contract_only", result["reasons"])


if __name__ == "__main__":
    unittest.main()

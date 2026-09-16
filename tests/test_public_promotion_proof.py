"""Public promotion proofs resolve SHA → acceptance without app-data."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from amof.commands.proof import cmd_proof
from amof.public_proof import PublicProofError, compute_evidence_digest, list_proofs, load_proof, normalize_public_proof

REPO = Path(__file__).resolve().parents[1]


class PublicPromotionProofTests(unittest.TestCase):
    def test_published_proofs_validate(self) -> None:
        proofs = list_proofs(REPO)
        tickets = {item["ticket_id"] for item in proofs}
        self.assertIn("AMOF-K8S-LIVE-EXECUTION-GATE-001", tickets)
        self.assertIn("AMOF-15MIN-RUNTIME-AUTHORITY-DEMO-001", tickets)
        for proof in proofs:
            self.assertEqual(proof["acceptance"], "PASS")
            self.assertEqual(proof["verification"], "PASS")
            self.assertIsNone(proof["gitops_sha"])
            self.assertEqual(proof["environment"]["kind"], "none")
            self.assertNotIn("/home/", json.dumps(proof))

    def test_show_by_sha_and_ticket(self) -> None:
        by_ticket = load_proof("AMOF-15MIN-RUNTIME-AUTHORITY-DEMO-001", repo_root=REPO)
        by_sha = load_proof(by_ticket["promotion_sha"], repo_root=REPO)
        self.assertEqual(by_ticket["source_sha"], by_sha["source_sha"])
        self.assertEqual(by_ticket["evidence_digest"], compute_evidence_digest(by_ticket))

    def test_digest_tamper_fails(self) -> None:
        proof = dict(load_proof("AMOF-K8S-LIVE-EXECUTION-GATE-001", repo_root=REPO))
        proof["acceptance"] = "FAIL"
        with self.assertRaises(PublicProofError):
            normalize_public_proof(proof)

    def test_cli_list(self) -> None:
        args = type("Args", (), {"proof_cmd": "list", "json": True, "ref": None})()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cmd_proof(args)
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertGreaterEqual(len(payload), 2)

    def test_cli_show_by_short_sha(self) -> None:
        args = type("Args", (), {"proof_cmd": "show", "json": True, "ref": "2fe9a45"})()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cmd_proof(args)
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["ticket_id"], "AMOF-15MIN-RUNTIME-AUTHORITY-DEMO-001")
        self.assertNotIn("AMOF_HOME", json.dumps(payload))

    def test_unknown_ref_fails(self) -> None:
        with self.assertRaises(PublicProofError):
            load_proof("AMOF-DOES-NOT-EXIST-001", repo_root=REPO)


if __name__ == "__main__":
    unittest.main()

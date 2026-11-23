"""Public promotion proofs resolve SHA → acceptance without app-data."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from importlib.resources import files
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

    def test_package_mirrors_exactly_the_public_allowlist(self) -> None:
        published = list_proofs(REPO)
        self.assertEqual(list_proofs(), published)
        self.assertEqual(len(published), 2)
        resources = files("amof").joinpath("proofs")
        for proof in published:
            name = f"{proof['ticket_id']}.json"
            self.assertEqual(
                resources.joinpath(name).read_bytes(),
                (REPO / "docs" / "proofs" / name).read_bytes(),
            )

    def test_ref_is_an_identifier_not_a_path(self) -> None:
        ticket = "AMOF-15MIN-RUNTIME-AUTHORITY-DEMO-001"
        self.assertEqual(load_proof(load_proof(ticket)["promotion_id"]), load_proof(ticket))
        for ref in (f"./{ticket}", f"docs/proofs/{ticket}", f"{ticket}.json"):
            with self.subTest(ref=ref), self.assertRaises(PublicProofError) as error:
                load_proof(ref, repo_root=REPO)
            self.assertEqual(error.exception.code, "not_found")

    def test_unallowlisted_and_mutated_proofs_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "docs" / "proofs"
            dest.mkdir(parents=True)
            for proof in list_proofs(REPO):
                name = f"{proof['ticket_id']}.json"
                (dest / name).write_bytes((REPO / "docs" / "proofs" / name).read_bytes())
            name = "AMOF-15MIN-RUNTIME-AUTHORITY-DEMO-001.json"
            (dest / "AMOF-UNLISTED-EXAMPLE-001.json").write_bytes((dest / name).read_bytes())
            self.assertEqual(list_proofs(root), list_proofs(REPO))
            changed = json.loads((dest / name).read_bytes())
            changed["verification_method"] = "unapproved"
            changed["evidence_digest"] = compute_evidence_digest(changed)
            (dest / name).write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaisesRegex(PublicProofError, "checksum mismatch"):
                list_proofs(root)

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

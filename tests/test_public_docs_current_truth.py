"""Current public docs path exists; superseded plans are archived."""

from __future__ import annotations

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

CURRENT = (
    "README.md",
    "docs/INDEX.md",
    "docs/runtime-authority.md",
    "docs/capability-authority.md",
    "docs/write-scope-authority.md",
    "docs/architecture/public-private-boundary.md",
    "docs/proofs/INDEX.md",
    "docs/historical/INDEX.md",
    "contracts/INDEX.md",
    "contracts/public-promotion-proof.schema.json",
)

ARCHIVED_AWAY_FROM_CURRENT = (
    "docs/roadmap",
    "docs/governed-cognition-runtime.md",
    "docs/remote-ial.md",
    "docs/security/AMOF-TRUST-MODEL-v1.md",
)

ARCHIVED_PRESENT = (
    "docs/historical/roadmap/AMOF-ULTRAPLAN-300.md",
    "docs/historical/governed-cognition-runtime.md",
    "docs/historical/remote-ial.md",
    "docs/historical/AMOF-TRUST-MODEL-v1.md",
)


class PublicDocsCurrentTruthTests(unittest.TestCase):
    def test_current_map_files_exist(self) -> None:
        for rel in CURRENT:
            self.assertTrue((REPO / rel).is_file(), rel)

    def test_superseded_plans_are_not_in_current_path(self) -> None:
        for rel in ARCHIVED_AWAY_FROM_CURRENT:
            self.assertFalse((REPO / rel).exists(), rel)

    def test_archived_material_is_reachable(self) -> None:
        for rel in ARCHIVED_PRESENT:
            self.assertTrue((REPO / rel).is_file(), rel)

    def test_readme_leads_with_demo_and_proof(self) -> None:
        text = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn("amof demo", text)
        self.assertIn("amof proof", text)
        self.assertIn("docs/INDEX.md", text)
        self.assertNotIn("docs/governed-cognition-runtime.md", text)


if __name__ == "__main__":
    unittest.main()

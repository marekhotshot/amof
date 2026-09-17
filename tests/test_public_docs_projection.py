"""Guard the allowlisted docs.amof.dev projection."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST_PATH = ROOT / "docs" / "public" / "ALLOWLIST.json"
PUBLIC_DIR = ROOT / "docs" / "public"
FORBIDDEN_SNIPPETS = (
    "cloud-dev-hetzner",
    "amof-ial-dev",
    "BEGIN PRIVATE",
)
FORBIDDEN_INCLUDE_MARKERS = (
    "docs/historical/",
    "docs/adr/",
    "docs/engineering/",
    "docs/operations/ticket-delivery-protocol.md",
)


class PublicDocsProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.allowlist = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))

    def test_allowlist_covers_only_expected_pages(self) -> None:
        pages = {Path(p).name for p in self.allowlist["pages"]}
        self.assertEqual(
            pages,
            {
                "index.md",
                "runtime-authority.md",
                "architecture.md",
                "in-practice.md",
                "evidence.md",
                "cli.md",
                "install.md",
                "releases.md",
            },
        )
        extras = [
            p.name
            for p in PUBLIC_DIR.glob("*.md")
            if p.name not in pages
        ]
        self.assertEqual(extras, [])

    def test_includes_are_allowlisted_and_exist(self) -> None:
        allowed = set(self.allowlist["include_sources"])
        for rel in self.allowlist["pages"]:
            text = (ROOT / rel).read_text(encoding="utf-8")
            for spec in _include_specs(text):
                target = ((ROOT / rel).parent / spec).resolve()
                resolved = target.relative_to(ROOT).as_posix()
                self.assertIn(resolved, allowed)
                self.assertTrue(target.is_file(), resolved)

    def test_projection_does_not_embed_internal_or_historical_paths(self) -> None:
        allowed = set(self.allowlist["include_sources"])
        for src in allowed:
            for marker in FORBIDDEN_INCLUDE_MARKERS:
                self.assertFalse(src.startswith(marker.rstrip("/")) or marker in src)
        for rel in self.allowlist["pages"]:
            text = _expanded(ROOT / rel, allowed)
            for snippet in FORBIDDEN_SNIPPETS:
                self.assertNotIn(snippet, text, f"{rel} leaked {snippet}")

    def test_no_ial_product_page(self) -> None:
        names = [Path(p).stem for p in self.allowlist["pages"]]
        self.assertNotIn("ial", names)
        self.assertNotIn("remote-ial", names)

    def test_brand_and_non_claims_present(self) -> None:
        index = (PUBLIC_DIR / "index.md").read_text(encoding="utf-8")
        self.assertIn("AMOF 3.5", index)
        self.assertIn("Not a published Trust Model", index)
        self.assertIn("LOCAL REFERENCE", (PUBLIC_DIR / "in-practice.md").read_text(encoding="utf-8"))

    def test_builder_emits_only_allowlisted_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "site"
            subprocess.run(
                ["python3", str(ROOT / "scripts" / "build-public-docs.py"), "--out", str(out)],
                check=True,
                cwd=ROOT,
            )
            html_names = sorted(p.name for p in out.glob("*.html"))
            self.assertEqual(
                html_names,
                [
                    "architecture.html",
                    "cli.html",
                    "evidence.html",
                    "in-practice.html",
                    "index.html",
                    "install.html",
                    "releases.html",
                    "runtime-authority.html",
                ],
            )
            index = (out / "index.html").read_text(encoding="utf-8")
            self.assertIn("AMOF", index)
            self.assertIn("3.5", index)
            self.assertNotIn("docs/historical/", index)
            self.assertIn("docs.amof.dev", (out / "CNAME").read_text(encoding="utf-8"))


def _include_specs(text: str) -> list[str]:
    import re

    return re.findall(r"<!--\s*public-docs-include:\s*([^\s]+)\s*-->", text)


def _expanded(page: Path, allowed: set[str]) -> str:
    text = page.read_text(encoding="utf-8")
    for spec in _include_specs(text):
        target = (page.parent / spec).resolve()
        rel = target.relative_to(ROOT).as_posix()
        if rel in allowed:
            text = text.replace(
                f"<!-- public-docs-include: {spec} -->",
                target.read_text(encoding="utf-8"),
            )
    return text


if __name__ == "__main__":
    unittest.main()

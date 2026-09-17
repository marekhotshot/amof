"""Guard the allowlisted docs.amof.dev projection."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "build_public_docs",
    ROOT / "scripts" / "build-public-docs.py",
)
builder = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(builder)

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
            self.assertIn('href="install.html"', index)
            self.assertIn('href="in-practice.html"', index)
            self.assertIn('href="runtime-authority.html"', index)
            self.assertIn('href="evidence.html"', index)
            self.assertNotIn('href="install.md"', index)
            self.assertNotIn('href="in-practice.md"', index)
            self.assertNotIn('href="runtime-authority.md"', index)
            self.assertNotIn('href="evidence.md"', index)
            self.assertEqual(builder.broken_internal_hrefs(out), [])

    def test_project_href_public_md_to_html(self) -> None:
        routes = builder.public_routes(self.allowlist)
        source = "docs/public/index.md"
        self.assertEqual(builder.project_href("install.md", source, routes), "install.html")
        self.assertEqual(builder.project_href("./in-practice.md", source, routes), "in-practice.html")
        self.assertEqual(
            builder.project_href("runtime-authority.md", source, routes),
            "runtime-authority.html",
        )
        self.assertEqual(builder.project_href("evidence.md", source, routes), "evidence.html")

    def test_project_href_preserves_anchor(self) -> None:
        routes = builder.public_routes(self.allowlist)
        self.assertEqual(
            builder.project_href(
                "runtime-authority.md#lifecycle",
                "docs/public/index.md",
                routes,
            ),
            "runtime-authority.html#lifecycle",
        )
        self.assertEqual(
            builder.project_href(
                "./install.md#supported-install-paths",
                "docs/public/index.md",
                routes,
            ),
            "install.html#supported-install-paths",
        )

    def test_project_href_external_unchanged(self) -> None:
        routes = builder.public_routes(self.allowlist)
        github = "https://github.com/marekhotshot/amof/blob/v3.5.0/docs/INDEX.md"
        self.assertEqual(
            builder.project_href(github, "docs/public/index.md", routes),
            github,
        )
        self.assertEqual(
            builder.project_href("https://amof.dev", "docs/public/cli.md", routes),
            "https://amof.dev",
        )

    def test_project_href_non_public_md_is_github_not_html(self) -> None:
        routes = builder.public_routes(self.allowlist)
        href = builder.project_href(
            "write-scope-authority.md",
            "docs/runtime-authority.md",
            routes,
        )
        self.assertEqual(
            href,
            "https://github.com/marekhotshot/amof/blob/main/docs/write-scope-authority.md",
        )
        self.assertFalse(href.endswith(".html"))
        self.assertNotEqual(href, "write-scope-authority.html")
        nested = builder.project_href(
            "../INDEX.md",
            "docs/releases/amof-3.5.0.md",
            routes,
        )
        self.assertEqual(
            nested,
            "https://github.com/marekhotshot/amof/blob/main/docs/INDEX.md",
        )

    def test_generated_nav_and_body_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "site"
            subprocess.run(
                ["python3", str(ROOT / "scripts" / "build-public-docs.py"), "--out", str(out)],
                check=True,
                cwd=ROOT,
            )
            index = (out / "index.html").read_text(encoding="utf-8")
            self.assertIn('<nav aria-label="Docs">', index)
            self.assertIn('href="install.html"', index)
            ra = (out / "runtime-authority.html").read_text(encoding="utf-8")
            self.assertIn("github.com/marekhotshot/amof/blob/main/docs/write-scope-authority.md", ra)
            self.assertNotIn('href="write-scope-authority.md"', ra)
            self.assertNotIn('href="write-scope-authority.html"', ra)
            self.assertEqual(builder.broken_internal_hrefs(out), [])


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

#!/usr/bin/env python3
"""Build the allowlisted docs.amof.dev static projection.

Only files listed in docs/public/ALLOWLIST.json may enter the build.
Includes may only resolve to listed include_sources. This is not a
mirror of docs/.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
from pathlib import Path

INCLUDE_RE = re.compile(r"<!--\s*public-docs-include:\s*([^\s]+)\s*-->")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
FENCE_RE = re.compile(r"^```(.*)$")


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_allowlist(root: Path) -> dict:
    path = root / "docs" / "public" / "ALLOWLIST.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != "amof.public_docs_allowlist/v1":
        raise SystemExit("ALLOWLIST schema mismatch")
    return data


def resolve_include(page: Path, spec: str, root: Path, allowed: set[str]) -> str:
    target = (page.parent / spec).resolve()
    try:
        rel = target.relative_to(root).as_posix()
    except ValueError as exc:
        raise SystemExit(f"include escapes repo: {spec}") from exc
    if rel not in allowed:
        raise SystemExit(f"include not allowlisted: {rel}")
    if not target.is_file():
        raise SystemExit(f"include missing: {rel}")
    return target.read_text(encoding="utf-8")


def expand_includes(text: str, page: Path, root: Path, allowed: set[str]) -> str:
    def _sub(match: re.Match[str]) -> str:
        return resolve_include(page, match.group(1), root, allowed)

    return INCLUDE_RE.sub(_sub, text)


def inline(text: str) -> str:
    out = html.escape(text)
    out = re.sub(r"`([^`]+)`", lambda m: f"<code>{m.group(1)}</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", out)
    out = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>',
        out,
    )
    return out


def render_markdown(md: str) -> str:
    lines = md.replace("\r\n", "\n").split("\n")
    parts: list[str] = []
    i = 0
    in_list = False
    in_table = False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            parts.append("</ul>")
            in_list = False

    def close_table() -> None:
        nonlocal in_table
        if in_table:
            parts.append("</tbody></table>")
            in_table = False

    while i < len(lines):
        line = lines[i]
        fence = FENCE_RE.match(line)
        if fence is not None:
            close_list()
            close_table()
            lang = html.escape(fence.group(1).strip())
            i += 1
            buf: list[str] = []
            while i < len(lines) and not lines[i].startswith("```"):
                buf.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1
            cls = f' class="language-{lang}"' if lang else ""
            parts.append(f"<pre><code{cls}>{html.escape(chr(10).join(buf))}</code></pre>")
            continue

        if re.match(r"^\|", line) and i + 1 < len(lines) and re.match(r"^\|\s*-+", lines[i + 1]):
            close_list()
            headers = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 2
            parts.append("<table><thead><tr>")
            parts.extend(f"<th>{inline(h)}</th>" for h in headers)
            parts.append("</tr></thead><tbody>")
            in_table = True
            continue

        if in_table:
            if re.match(r"^\|", line):
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                parts.append("<tr>")
                parts.extend(f"<td>{inline(c)}</td>" for c in cells)
                parts.append("</tr>")
                i += 1
                continue
            close_table()

        heading = HEADING_RE.match(line)
        if heading:
            close_list()
            level = len(heading.group(1))
            title = heading.group(2).strip()
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
            parts.append(f'<h{level} id="{html.escape(slug, quote=True)}">{inline(title)}</h{level}>')
            i += 1
            continue

        if re.match(r"^[-*]\s+", line):
            close_table()
            if not in_list:
                parts.append("<ul>")
                in_list = True
            item = re.sub(r"^[-*]\s+", "", line)
            parts.append(f"<li>{inline(item)}</li>")
            i += 1
            continue

        close_list()
        if not line.strip():
            i += 1
            continue
        parts.append(f"<p>{inline(line)}</p>")
        i += 1

    close_list()
    close_table()
    return "\n".join(parts)


NAV = [
    ("index.html", "Start"),
    ("runtime-authority.html", "Runtime Authority"),
    ("architecture.html", "Architecture"),
    ("in-practice.html", "In practice"),
    ("evidence.html", "Evidence"),
    ("cli.html", "CLI"),
    ("install.html", "Install"),
    ("releases.html", "Releases"),
]


def page_html(title: str, body: str, current: str) -> str:
    links = []
    for href, label in NAV:
        cls = ' class="is-current"' if href == current else ""
        links.append(f'<a href="{href}"{cls}>{html.escape(label)}</a>')
    nav = "\n        ".join(links)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} — AMOF 3.5 docs</title>
  <meta name="description" content="Curated public documentation for AMOF 3.5 Runtime Authority.">
  <link rel="icon" href="./amof-logo.svg" type="image/svg+xml">
  <link rel="stylesheet" href="./styles.css">
</head>
<body>
  <header class="site-nav">
    <a class="brand" href="./index.html" aria-label="AMOF 3.5 docs home">
      <img src="./amof-logo.svg" width="32" height="34" alt="">
      <span>AMOF</span>
      <span class="brand-version">3.5</span>
    </a>
    <nav aria-label="Docs">
        {nav}
    </nav>
    <a class="nav-cta" href="https://amof.dev" target="_blank" rel="noopener noreferrer">amof.dev</a>
  </header>
  <main class="doc">
{body}
  </main>
  <footer class="doc-foot">
    <p>Curated public projection of <a href="https://github.com/marekhotshot/amof">marekhotshot/amof</a>. Git is source of truth.</p>
  </footer>
</body>
</html>
"""


STYLES = """
:root {
  --bg: #0b1216;
  --ink: #e8eef2;
  --ink-muted: #9aafbb;
  --line: #24323a;
  --accent: #7ec8e3;
}
* { box-sizing: border-box; }
html, body { margin: 0; background: var(--bg); color: var(--ink); font-family: ui-sans-serif, system-ui, sans-serif; }
a { color: var(--accent); }
.site-nav {
  position: sticky; top: 0; z-index: 20;
  display: flex; align-items: center; gap: 1rem; flex-wrap: wrap;
  padding: 0.85rem 1.25rem;
  background: rgba(11,18,22,0.92);
  border-bottom: 1px solid var(--line);
}
.brand { display: inline-flex; align-items: center; gap: 0.5rem; color: var(--ink); text-decoration: none; font-weight: 700; }
.brand img { width: 1.7rem; height: auto; }
.brand-version { font-size: 0.78rem; font-weight: 600; letter-spacing: 0.06em; color: var(--ink-muted); }
.site-nav nav { display: flex; flex-wrap: wrap; gap: 0.35rem 0.9rem; margin-left: auto; font-size: 0.95rem; }
.site-nav nav a { color: var(--ink-muted); text-decoration: none; }
.site-nav nav a.is-current, .site-nav nav a:hover { color: var(--ink); }
.nav-cta { color: var(--ink); text-decoration: none; font-weight: 600; }
.doc { max-width: 760px; margin: 0 auto; padding: 2.5rem 1.25rem 4rem; }
.doc h1 { font-size: 2rem; line-height: 1.2; }
.doc h2 { margin-top: 2rem; }
.doc p, .doc li { line-height: 1.65; }
.doc pre { overflow: auto; padding: 0.9rem 1rem; background: #121c22; border: 1px solid var(--line); }
.doc code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.92em; }
.doc table { width: 100%; border-collapse: collapse; margin: 1rem 0; }
.doc th, .doc td { border: 1px solid var(--line); padding: 0.45rem 0.6rem; text-align: left; }
.doc-foot { max-width: 760px; margin: 0 auto; padding: 0 1.25rem 3rem; color: var(--ink-muted); }
"""


def first_heading(md: str) -> str:
    for line in md.splitlines():
        match = HEADING_RE.match(line)
        if match:
            return match.group(2).strip()
    return "AMOF 3.5"


def build(root: Path, out: Path) -> None:
    allowlist = load_allowlist(root)
    pages = [root / rel for rel in allowlist["pages"]]
    include_allowed = set(allowlist["include_sources"])
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / "styles.css").write_text(STYLES.lstrip() + "\n", encoding="utf-8")
    shutil.copy2(root / "docs" / "assets" / "amof-logo.svg", out / "amof-logo.svg")
    (out / "CNAME").write_text("docs.amof.dev\n", encoding="utf-8")
    (out / "_headers").write_text("/*\n  X-Content-Type-Options: nosniff\n", encoding="utf-8")

    for page in pages:
        rel = page.relative_to(root).as_posix()
        if rel not in allowlist["pages"]:
            raise SystemExit(f"unexpected page {rel}")
        raw = page.read_text(encoding="utf-8")
        expanded = expand_includes(raw, page, root, include_allowed)
        title = first_heading(expanded)
        html_name = "index.html" if page.stem == "index" else f"{page.stem}.html"
        (out / html_name).write_text(
            page_html(title, render_markdown(expanded), html_name),
            encoding="utf-8",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default="dist/public-docs",
        help="output directory (default: dist/public-docs)",
    )
    args = parser.parse_args()
    root = repo_root()
    build(root, Path(args.out) if Path(args.out).is_absolute() else root / args.out)
    print(f"built {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

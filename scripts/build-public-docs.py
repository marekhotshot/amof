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
import posixpath
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

INCLUDE_RE = re.compile(r"<!--\s*public-docs-include:\s*([^\s]+)\s*-->")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
FENCE_RE = re.compile(r"^```(.*)$")
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
HTML_HREF_RE = re.compile(r'href="([^"]+)"')
GITHUB_BLOB = "https://github.com/marekhotshot/amof/blob/main/"
EXTERNAL_SCHEMES = ("http://", "https://", "mailto:", "data:")


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_allowlist(root: Path) -> dict:
    path = root / "docs" / "public" / "ALLOWLIST.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != "amof.public_docs_allowlist/v1":
        raise SystemExit("ALLOWLIST schema mismatch")
    return data


def published_name(page_rel: str) -> str:
    stem = Path(page_rel).stem
    return "index.html" if stem == "index" else f"{stem}.html"


def public_routes(allowlist: dict) -> dict[str, str]:
    return {rel: published_name(rel) for rel in allowlist["pages"]}


def include_projection_map(root: Path, allowlist: dict) -> dict[str, str]:
    """Map each include source to the public page that includes it."""
    mapping: dict[str, str] = {}
    for page_rel in allowlist["pages"]:
        text = (root / page_rel).read_text(encoding="utf-8")
        for spec in INCLUDE_RE.findall(text):
            rel = include_rel(root / page_rel, spec, root)
            existing = mapping.get(rel)
            if existing and existing != page_rel:
                raise SystemExit(f"include {rel} mapped to both {existing} and {page_rel}")
            mapping[rel] = page_rel
    return mapping


def projection_link_map(root: Path, allowlist: dict) -> dict[str, str]:
    """Canonical source path -> published HTML route.

    Rule: source canonical doc -> projected public page -> rendered route.
    Public pages, include sources, and explicit projection_map entries
    all resolve to the same published name the nav already uses.
    """
    pages = set(allowlist["pages"])
    routes = public_routes(allowlist)
    derived = include_projection_map(root, allowlist)
    explicit = dict(allowlist.get("projection_map") or {})

    for src in allowlist["include_sources"]:
        page = derived.get(src) or explicit.get(src)
        if page is None:
            raise SystemExit(f"include source has no public page: {src}")
        if page not in pages:
            raise SystemExit(f"projection target is not a public page: {src} -> {page}")
        html = published_name(page)
        if src in routes and routes[src] != html:
            raise SystemExit(f"projection conflict for {src}: {routes[src]} vs {html}")
        routes[src] = html

    for src, page in explicit.items():
        if page not in pages:
            raise SystemExit(f"projection_map target is not a public page: {src} -> {page}")
        html = published_name(page)
        if src in routes and routes[src] != html:
            raise SystemExit(f"projection_map conflict for {src}: {routes[src]} vs {html}")
        routes[src] = html
        if not (root / src).is_file():
            raise SystemExit(f"projection_map source missing: {src}")

    for src, page in derived.items():
        html = published_name(page)
        if src in routes and routes[src] != html:
            raise SystemExit(f"include projection conflict for {src}: {routes[src]} vs {html}")
        routes[src] = html

    return routes


def is_external_href(href: str) -> bool:
    stripped = href.strip()
    if stripped.startswith(EXTERNAL_SCHEMES) or stripped.startswith("//"):
        return True
    parsed = urlparse(stripped)
    return bool(parsed.scheme)


def split_href(href: str) -> tuple[str, str]:
    path, sep, fragment = href.strip().partition("#")
    return path, (sep + fragment if sep else "")


def resolve_repo_rel(source_rel: str, href_path: str) -> str:
    cleaned = href_path[2:] if href_path.startswith("./") else href_path
    if cleaned.startswith("/"):
        return posixpath.normpath(cleaned.lstrip("/"))
    if cleaned.startswith("docs/"):
        return posixpath.normpath(cleaned)
    base_dir = posixpath.dirname(source_rel)
    joined = posixpath.normpath(posixpath.join(base_dir, cleaned))
    if joined in {".", "/"}:
        return source_rel
    return joined.lstrip("/")


def project_href(href: str, source_rel: str, routes: dict[str, str]) -> str:
    """Resolve a markdown href through the public projection map.

    Mapped canonical sources become the curated public route. Explicit
    GitHub/external URLs stay unchanged. Unmapped .md sources become
    GitHub blob URLs so the site never emits a broken local route.
    """
    raw = href.strip()
    if not raw or is_external_href(raw) or raw.startswith("#"):
        return raw
    path, fragment = split_href(raw)
    if not path:
        return raw
    if path.startswith("/") and not (path.endswith(".md") or path.startswith("/docs/")):
        return raw
    resolved = resolve_repo_rel(source_rel, path)
    if resolved in routes:
        return routes[resolved] + fragment
    if path.endswith(".md") or resolved.endswith(".md"):
        return GITHUB_BLOB + resolved + fragment
    return raw


def rewrite_markdown_links(text: str, source_rel: str, routes: dict[str, str]) -> str:
    def _sub(match: re.Match[str]) -> str:
        label, href = match.group(1), match.group(2)
        projected = project_href(href, source_rel, routes)
        return f"[{label}]({projected})"

    return MD_LINK_RE.sub(_sub, text)


def include_rel(page: Path, spec: str, root: Path) -> str:
    target = (page.parent / spec).resolve()
    try:
        return target.relative_to(root).as_posix()
    except ValueError as exc:
        raise SystemExit(f"include escapes repo: {spec}") from exc


def resolve_include(page: Path, spec: str, root: Path, allowed: set[str]) -> tuple[str, str]:
    rel = include_rel(page, spec, root)
    if rel not in allowed:
        raise SystemExit(f"include not allowlisted: {rel}")
    target = root / rel
    if not target.is_file():
        raise SystemExit(f"include missing: {rel}")
    return rel, target.read_text(encoding="utf-8")


def expand_includes(
    text: str,
    page: Path,
    root: Path,
    allowed: set[str],
    routes: dict[str, str],
) -> str:
    def _sub(match: re.Match[str]) -> str:
        spec = match.group(1)
        rel, body = resolve_include(page, spec, root, allowed)
        return rewrite_markdown_links(body, rel, routes)

    expanded = INCLUDE_RE.sub(_sub, text)
    page_rel = page.relative_to(root).as_posix()
    return rewrite_markdown_links(expanded, page_rel, routes)


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
    site_title = (
        "AMOF Runtime Authority — Governed AI Execution"
        if current == "index.html"
        else f"{title} — AMOF Runtime Authority"
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(site_title)}</title>
  <meta name="description" content="Runtime Authority for bounded, verifiable AI execution.">
  <meta property="og:title" content="{html.escape(site_title)}">
  <meta property="og:description" content="Runtime Authority for bounded, verifiable AI execution.">
  <meta name="twitter:card" content="summary">
  <meta name="twitter:title" content="{html.escape(site_title)}">
  <meta name="twitter:description" content="Runtime Authority for bounded, verifiable AI execution.">
  <link rel="icon" href="./amof-logo.svg" type="image/svg+xml">
  <link rel="stylesheet" href="./styles.css">
</head>
<body>
  <header class="site-nav">
    <a class="brand" href="./index.html" aria-label="AMOF Runtime Authority home">
      <img src="./amof-logo.svg" width="32" height="34" alt="">
      <span class="brand-lockup">
        <span class="brand-name">AMOF Runtime Authority</span>
        <span class="brand-version">3.5</span>
      </span>
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
.brand-lockup { display: flex; flex-direction: column; line-height: 1.05; }
.brand-name { font-size: 0.92rem; letter-spacing: 0.02em; }
.brand-version { font-size: 0.7rem; font-weight: 600; letter-spacing: 0.06em; color: var(--ink-muted); }
@media (max-width: 640px) {
  .brand-name { font-size: 0.8rem; }
}
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
    return "AMOF Runtime Authority 3.5"


def iter_html_hrefs(html_text: str) -> list[str]:
    return HTML_HREF_RE.findall(html_text)


def broken_internal_hrefs(out: Path) -> list[tuple[str, str]]:
    """Return (page, href) pairs that are internal and missing from dist."""
    return [(page, href) for page, href, _reason in validate_generated_hrefs(out)]


def validate_generated_hrefs(out: Path) -> list[tuple[str, str, str]]:
    """Crawl generated HTML for internal-link invariant failures."""
    failures: list[tuple[str, str, str]] = []
    for page in sorted(out.glob("*.html")):
        for href in iter_html_hrefs(page.read_text(encoding="utf-8")):
            if is_external_href(href) or href.startswith("#"):
                continue
            path, _fragment = split_href(href)
            if not path:
                continue
            candidate = path[2:] if path.startswith("./") else path.lstrip("/")
            if not candidate:
                continue
            if candidate.endswith(".md"):
                failures.append((page.name, href, "internal href ends in .md"))
                continue
            if candidate.startswith("docs/") or "/docs/" in candidate:
                failures.append((page.name, href, "internal href points to a non-rendered source path"))
                continue
            if not (out / candidate).is_file():
                failures.append((page.name, href, "internal href target missing from dist/public-docs"))
    return failures


def build(root: Path, out: Path) -> None:
    allowlist = load_allowlist(root)
    pages = [root / rel for rel in allowlist["pages"]]
    include_allowed = set(allowlist["include_sources"])
    routes = projection_link_map(root, allowlist)
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
        expanded = expand_includes(raw, page, root, include_allowed, routes)
        title = first_heading(expanded)
        html_name = published_name(rel)
        (out / html_name).write_text(
            page_html(title, render_markdown(expanded), html_name),
            encoding="utf-8",
        )
    failures = validate_generated_hrefs(out)
    if failures:
        detail = "; ".join(f"{page} -> {href} ({reason})" for page, href, reason in failures)
        raise SystemExit(f"broken internal docs links: {detail}")


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

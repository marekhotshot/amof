"""KB command - sync knowledge base with Confluence."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
import urllib.error
import urllib.parse
import base64
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from amof.manifest import get_ecosystem_root, get_journal_dir


def get_confluence_auth() -> tuple[str, str] | None:
    """Get Confluence credentials from environment."""
    user = os.environ.get("ATLASSIAN_USER")
    token = os.environ.get("ATLASSIAN_TOKEN")
    if not user or not token:
        return None
    return (user, token)


def get_confluence_url() -> str:
    """Get Confluence base URL from environment."""
    return os.environ.get("CONFLUENCE_URL", os.environ.get("ATLASSIAN_URL", "https://confluence.example.com"))


def get_default_space() -> str:
    """Get default Confluence space from environment."""
    return os.environ.get("CONFLUENCE_SPACE", "DEMO")


def make_confluence_request(
    endpoint: str,
    method: str = "GET",
    data: dict | None = None,
) -> dict | None:
    """Make a request to Confluence REST API."""
    auth = get_confluence_auth()
    if not auth:
        sys.stderr.write("[kb] Confluence credentials not configured\n")
        sys.stderr.write("[kb] Set ATLASSIAN_USER and ATLASSIAN_TOKEN in .env\n")
        return None
    
    base_url = get_confluence_url()
    api_url = f"{base_url}/rest/api{endpoint}"
    
    try:
        credentials = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        headers = {
            "Accept": "application/json",
            "Authorization": f"Basic {credentials}",
        }
        
        body = None
        if data:
            headers["Content-Type"] = "application/json"
            body = json.dumps(data).encode("utf-8")
        
        req = urllib.request.Request(api_url, data=body, headers=headers, method=method)
        
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None  # Page not found is not an error
        elif e.code == 401:
            sys.stderr.write("[kb] Authentication failed. Check credentials.\n")
        elif e.code == 403:
            sys.stderr.write("[kb] Permission denied. Check your access.\n")
        else:
            error_body = e.read().decode("utf-8") if e.fp else ""
            sys.stderr.write(f"[kb] HTTP {e.code}: {error_body[:200]}\n")
        return None
    
    except urllib.error.URLError as e:
        sys.stderr.write(f"[kb] Connection error: {e.reason}\n")
        return None
    except Exception as e:
        sys.stderr.write(f"[kb] Error: {e}\n")
        return None


def parse_frontmatter(content: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from markdown content.
    
    Returns (frontmatter_dict, body_content).
    """
    if not content.startswith("---"):
        return {}, content
    
    # Find end of frontmatter
    end_match = re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        return {}, content
    
    frontmatter_text = content[3:end_match.start() + 3]
    body = content[end_match.end() + 3:]
    
    # Simple YAML parsing (key: value format)
    frontmatter = {}
    for line in frontmatter_text.strip().split("\n"):
        if ":" in line:
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            frontmatter[key] = value
    
    return frontmatter, body


def add_frontmatter(content: str, frontmatter: dict) -> str:
    """Add or update frontmatter in markdown content."""
    existing_fm, body = parse_frontmatter(content)
    existing_fm.update(frontmatter)
    
    lines = ["---"]
    for key, value in existing_fm.items():
        if isinstance(value, str) and (" " in value or ":" in value):
            lines.append(f'{key}: "{value}"')
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")
    
    return "\n".join(lines) + body


def get_kb_files(ecosystem: str) -> List[Path]:
    """Get all markdown files in ecosystem KB directory."""
    kb_dir = Path(f"ecosystems/{ecosystem}/kb")
    if not kb_dir.exists():
        return []
    return list(kb_dir.glob("**/*.md"))


def search_confluence_page(space: str, title: str) -> dict | None:
    """Search for a page by title in a space."""
    encoded_title = urllib.parse.quote(title)
    result = make_confluence_request(
        f"/content?spaceKey={space}&title={encoded_title}&expand=version"
    )
    if result and result.get("results"):
        return result["results"][0]
    return None


def get_confluence_page(page_id: str) -> dict | None:
    """Get a page by ID."""
    return make_confluence_request(
        f"/content/{page_id}?expand=body.storage,version"
    )


def create_confluence_page(
    space: str,
    title: str,
    content: str,
    parent_id: Optional[str] = None,
) -> dict | None:
    """Create a new Confluence page."""
    data = {
        "type": "page",
        "title": title,
        "space": {"key": space},
        "body": {
            "storage": {
                "value": content,
                "representation": "storage",
            }
        },
    }
    
    if parent_id:
        data["ancestors"] = [{"id": parent_id}]
    
    return make_confluence_request("/content", method="POST", data=data)


def update_confluence_page(
    page_id: str,
    title: str,
    content: str,
    version: int,
) -> dict | None:
    """Update an existing Confluence page."""
    data = {
        "type": "page",
        "title": title,
        "body": {
            "storage": {
                "value": content,
                "representation": "storage",
            }
        },
        "version": {"number": version + 1},
    }
    
    return make_confluence_request(f"/content/{page_id}", method="PUT", data=data)


def markdown_to_confluence(markdown: str) -> str:
    """Convert markdown to Confluence storage format (basic conversion).
    
    Note: This is a simplified converter. For production use,
    consider using a proper markdown-to-confluence converter.
    """
    content = markdown
    
    # Headers
    content = re.sub(r"^### (.+)$", r"<h3>\1</h3>", content, flags=re.MULTILINE)
    content = re.sub(r"^## (.+)$", r"<h2>\1</h2>", content, flags=re.MULTILINE)
    content = re.sub(r"^# (.+)$", r"<h1>\1</h1>", content, flags=re.MULTILINE)
    
    # Bold and italic
    content = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", content)
    content = re.sub(r"\*(.+?)\*", r"<em>\1</em>", content)
    
    # Code blocks
    content = re.sub(
        r"```(\w+)?\n(.*?)```",
        r'<ac:structured-macro ac:name="code"><ac:parameter ac:name="language">\1</ac:parameter><ac:plain-text-body><![CDATA[\2]]></ac:plain-text-body></ac:structured-macro>',
        content,
        flags=re.DOTALL,
    )
    
    # Inline code
    content = re.sub(r"`([^`]+)`", r"<code>\1</code>", content)
    
    # Links
    content = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', content)
    
    # Lists (simple conversion)
    content = re.sub(r"^- (.+)$", r"<li>\1</li>", content, flags=re.MULTILINE)
    content = re.sub(r"(<li>.*</li>\n?)+", r"<ul>\g<0></ul>", content)
    
    # Paragraphs (wrap remaining text)
    lines = content.split("\n\n")
    formatted_lines = []
    for line in lines:
        line = line.strip()
        if line and not line.startswith("<"):
            line = f"<p>{line}</p>"
        formatted_lines.append(line)
    content = "\n".join(formatted_lines)
    
    return content


def confluence_to_markdown(storage: str) -> str:
    """Convert Confluence storage format to markdown (basic conversion)."""
    content = storage
    
    # Headers
    content = re.sub(r"<h1>(.+?)</h1>", r"# \1", content)
    content = re.sub(r"<h2>(.+?)</h2>", r"## \1", content)
    content = re.sub(r"<h3>(.+?)</h3>", r"### \1", content)
    
    # Bold and italic
    content = re.sub(r"<strong>(.+?)</strong>", r"**\1**", content)
    content = re.sub(r"<em>(.+?)</em>", r"*\1*", content)
    
    # Code blocks (simplified)
    content = re.sub(
        r'<ac:structured-macro ac:name="code">.*?<ac:plain-text-body><!\[CDATA\[(.*?)\]\]></ac:plain-text-body></ac:structured-macro>',
        r"```\n\1\n```",
        content,
        flags=re.DOTALL,
    )
    
    # Inline code
    content = re.sub(r"<code>([^<]+)</code>", r"`\1`", content)
    
    # Links
    content = re.sub(r'<a href="([^"]+)">([^<]+)</a>', r"[\2](\1)", content)
    
    # Lists
    content = re.sub(r"<li>(.+?)</li>", r"- \1", content)
    content = re.sub(r"</?ul>", "", content)
    
    # Paragraphs
    content = re.sub(r"<p>(.+?)</p>", r"\1\n", content)
    
    # Clean up remaining HTML
    content = re.sub(r"<[^>]+>", "", content)
    
    return content.strip()


def cmd_kb_pull(ecosystem: str, space: Optional[str] = None) -> int:
    """Pull KB articles from Confluence to local files."""
    space = space or get_default_space()
    kb_dir = Path(f"ecosystems/{ecosystem}/kb")
    
    if not kb_dir.exists():
        print(f"[kb] Creating KB directory: {kb_dir}")
        kb_dir.mkdir(parents=True, exist_ok=True)
    
    # Get local files to find confluence_id mappings
    local_files = get_kb_files(ecosystem)
    pulled = 0
    
    for file_path in local_files:
        content = file_path.read_text(encoding="utf-8")
        frontmatter, body = parse_frontmatter(content)
        
        confluence_id = frontmatter.get("confluence_id")
        if not confluence_id:
            print(f"[kb] Skipping {file_path.name}: no confluence_id")
            continue
        
        print(f"[kb] Pulling {file_path.name} from Confluence...")
        page = get_confluence_page(confluence_id)
        
        if not page:
            sys.stderr.write(f"[kb] Page not found: {confluence_id}\n")
            continue
        
        # Convert to markdown
        storage_body = page.get("body", {}).get("storage", {}).get("value", "")
        markdown_body = confluence_to_markdown(storage_body)
        
        # Update frontmatter
        frontmatter["last_synced"] = datetime.now().isoformat()[:19]
        frontmatter["confluence_version"] = str(page.get("version", {}).get("number", 1))
        
        new_content = add_frontmatter(markdown_body, frontmatter)
        file_path.write_text(new_content, encoding="utf-8")
        
        print(f"[kb] ✓ Updated {file_path.name}")
        pulled += 1
    
    print(f"\n[kb] Pulled {pulled} file(s)")
    return 0


def cmd_kb_push(ecosystem: str, space: Optional[str] = None) -> int:
    """Push local KB files to Confluence."""
    space = space or get_default_space()
    kb_dir = Path(f"ecosystems/{ecosystem}/kb")
    
    if not kb_dir.exists():
        sys.stderr.write(f"[kb] KB directory not found: {kb_dir}\n")
        return 1
    
    local_files = get_kb_files(ecosystem)
    pushed = 0
    created = 0
    
    for file_path in local_files:
        content = file_path.read_text(encoding="utf-8")
        frontmatter, body = parse_frontmatter(content)
        
        # Get title from frontmatter or filename
        title = frontmatter.get("title", file_path.stem.replace("-", " ").title())
        confluence_id = frontmatter.get("confluence_id")
        
        # Convert markdown to Confluence format
        confluence_body = markdown_to_confluence(body)
        
        if confluence_id:
            # Update existing page
            print(f"[kb] Updating {file_path.name}...")
            page = get_confluence_page(confluence_id)
            
            if not page:
                sys.stderr.write(f"[kb] Page not found: {confluence_id}\n")
                continue
            
            version = page.get("version", {}).get("number", 1)
            result = update_confluence_page(confluence_id, title, confluence_body, version)
            
            if result:
                print(f"[kb] ✓ Updated {file_path.name}")
                
                # Update frontmatter
                frontmatter["last_synced"] = datetime.now().isoformat()[:19]
                frontmatter["confluence_version"] = str(version + 1)
                new_content = add_frontmatter(body, frontmatter)
                file_path.write_text(new_content, encoding="utf-8")
                
                pushed += 1
            else:
                sys.stderr.write(f"[kb] Failed to update {file_path.name}\n")
        else:
            # Create new page
            print(f"[kb] Creating {file_path.name}...")
            
            # Check if page with same title exists
            existing = search_confluence_page(space, title)
            if existing:
                print(f"[kb] Page '{title}' already exists, linking...")
                confluence_id = existing.get("id")
            else:
                result = create_confluence_page(space, title, confluence_body)
                if result:
                    confluence_id = result.get("id")
                    created += 1
                else:
                    sys.stderr.write(f"[kb] Failed to create {file_path.name}\n")
                    continue
            
            if confluence_id:
                # Update frontmatter with confluence_id
                frontmatter["confluence_id"] = confluence_id
                frontmatter["last_synced"] = datetime.now().isoformat()[:19]
                frontmatter["confluence_version"] = "1"
                new_content = add_frontmatter(body, frontmatter)
                file_path.write_text(new_content, encoding="utf-8")
                
                print(f"[kb] ✓ Linked {file_path.name} to {confluence_id}")
    
    print(f"\n[kb] Pushed {pushed} file(s), created {created} new page(s)")
    return 0


def cmd_kb_diff(ecosystem: str, space: Optional[str] = None) -> int:
    """Show differences between local KB and Confluence."""
    space = space or get_default_space()
    kb_dir = Path(f"ecosystems/{ecosystem}/kb")
    
    if not kb_dir.exists():
        sys.stderr.write(f"[kb] KB directory not found: {kb_dir}\n")
        return 1
    
    local_files = get_kb_files(ecosystem)
    
    print(f"[kb] Comparing {len(local_files)} local file(s) with Confluence...\n")
    
    for file_path in local_files:
        content = file_path.read_text(encoding="utf-8")
        frontmatter, body = parse_frontmatter(content)
        
        confluence_id = frontmatter.get("confluence_id")
        last_synced = frontmatter.get("last_synced", "never")
        
        if not confluence_id:
            print(f"  {file_path.name}: NEW (not in Confluence)")
            continue
        
        page = get_confluence_page(confluence_id)
        if not page:
            print(f"  {file_path.name}: DELETED (page not found in Confluence)")
            continue
        
        remote_version = page.get("version", {}).get("number", 1)
        local_version = int(frontmatter.get("confluence_version", 0))
        
        if remote_version > local_version:
            print(f"  {file_path.name}: REMOTE UPDATED (v{local_version} → v{remote_version})")
        elif remote_version < local_version:
            print(f"  {file_path.name}: LOCAL UPDATED (last sync: {last_synced})")
        else:
            print(f"  {file_path.name}: IN SYNC (v{remote_version})")
    
    return 0


def cmd_kb_sync(ecosystem: str, space: Optional[str] = None) -> int:
    """Bi-directional sync between local KB and Confluence."""
    space = space or get_default_space()
    
    print("[kb] Pulling remote changes...")
    cmd_kb_pull(ecosystem, space)
    
    print("\n[kb] Pushing local changes...")
    cmd_kb_push(ecosystem, space)
    
    return 0


## ---------------------------------------------------------------------------
## Journal → KB consolidation
## ---------------------------------------------------------------------------

# Topic detection patterns: (topic_slug, display_name, filename_keywords, content_keywords)
# A journal matches a topic if ANY filename keyword matches OR 2+ content keywords match.
TOPIC_PATTERNS: List[tuple] = [
    (
        "architecture",
        "Architecture & Design",
        ["orchestrator", "architecture", "design", "custom-orchestrator"],
        ["agent loop", "tool system", "context builder", "guardrail", "system prompt", "tool registry"],
    ),
    (
        "benchmarks",
        "Benchmarks & Performance",
        ["benchmark", "performance", "comparison"],
        ["benchmark", "cost", "wall clock", "llm calls", "tokens", "efficiency", "cursor vs", "vs orchestrator"],
    ),
    (
        "llm-integration",
        "LLM Integration",
        ["thinking", "llm", "anthropic", "openai", "model"],
        ["extended thinking", "model ladder", "thinking budget", "prefill", "json parse", "opus", "sonnet", "haiku"],
    ),
    (
        "cli-ux",
        "CLI & User Experience",
        ["interactive", "shell", "cli", "ux"],
        ["interactive shell", "repl", "slash command", "colored output", "user prompt", "plan-execute"],
    ),
    (
        "agent-features",
        "Agent Features & Continuity",
        ["continuity", "resume", "checkpoint", "journal", "plan"],
        ["resume", "checkpoint", "signal handler", "session save", "follow-up", "plan persistence", "auto-journal"],
    ),
    (
        "deployment-ops",
        "Deployment & Operations",
        ["deploy", "k8s", "kubernetes", "helm", "jenkins", "ops"],
        ["kubernetes", "helm", "jenkins", "k8s", "deployment", "pipeline", "ops tool", "image migrat"],
    ),
    (
        "indexing",
        "Codebase Indexing",
        ["merkle", "index", "indexing"],
        ["merkle tree", "codebase index", "incremental", "hash tree", "change detection"],
    ),
    (
        "linting",
        "Linting & Code Quality",
        ["lint", "linter", "quality"],
        ["linter", "lint", "ruff", "shellcheck", "yamllint", "end-of-task lint", "modified files"],
    ),
]


def _detect_topic(filename: str, content: str) -> str:
    """Detect the best matching topic for a journal entry.

    Returns the topic slug.  Falls back to ``"general"`` when no pattern
    matches strongly enough.
    """
    fname_lower = filename.lower()
    content_lower = content.lower()

    best_topic = "general"
    best_score = 0

    for slug, _display, fname_kws, content_kws in TOPIC_PATTERNS:
        score = 0
        # Filename keywords (strong signal)
        for kw in fname_kws:
            if kw in fname_lower:
                score += 3
        # Content keywords (weaker per-hit, but cumulative)
        for kw in content_kws:
            if kw in content_lower:
                score += 1
        if score > best_score:
            best_score = score
            best_topic = slug

    # Require a minimum score to avoid false positives
    if best_score < 2:
        return "general"
    return best_topic


def _topic_display_name(slug: str) -> str:
    """Return the human-readable name for a topic slug."""
    for s, display, _, _ in TOPIC_PATTERNS:
        if s == slug:
            return display
    return slug.replace("-", " ").title()


def _extract_key_learnings(body: str) -> str:
    """Extract the most valuable parts of a journal body for KB consolidation.

    Keeps headings, bullet points, tables, and code blocks.
    Strips raw metrics tables (LLM calls, tokens, etc.) and generated footers.
    """
    lines = body.strip().splitlines()
    out: list[str] = []
    skip_section = False
    in_code_block = False

    for line in lines:
        stripped = line.strip()

        # Track code blocks (don't skip inside them)
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            out.append(line)
            continue
        if in_code_block:
            out.append(line)
            continue

        # Skip auto-generated footer
        if stripped.startswith("*Generated by AMOF"):
            continue
        if stripped.startswith("*Manual journal entry"):
            continue

        # Skip raw metrics sections (tables with LLM call counts, tokens, etc.)
        if stripped.startswith("## Metrics") or stripped.startswith("## Model Usage") or stripped.startswith("## Tool Usage"):
            skip_section = True
            continue
        if skip_section:
            if stripped.startswith("## ") or stripped.startswith("# "):
                skip_section = False
            else:
                continue

        out.append(line)

    # Trim trailing empty lines
    while out and not out[-1].strip():
        out.pop()

    return "\n".join(out)


def _build_kb_article(topic_slug: str, entries: List[tuple], existing_body: str = "") -> str:
    """Build or update a KB article from journal entries.

    *entries* is a list of ``(date_str, filename, body_text)`` tuples sorted
    by date ascending.

    If *existing_body* is not empty, the new entries are appended under a new
    date heading, avoiding duplicate content.
    """
    parts: list[str] = []

    if existing_body:
        parts.append(existing_body.rstrip())
        parts.append("")  # blank separator

    for date_str, filename, body in entries:
        # Skip if this journal's content is already present (dedup by filename)
        if filename in existing_body:
            continue

        learnings = _extract_key_learnings(body)
        if not learnings.strip():
            continue

        parts.append(f"## Entry: {date_str} ({filename})")
        parts.append("")
        parts.append(learnings)
        parts.append("")

    return "\n".join(parts)


def cmd_kb_consolidate(ecosystem: str, dry_run: bool = False) -> int:
    """Consolidate journal entries into KB articles by topic.

    1. Reads journals from ``ecosystems/<eco>/journal/`` (not archived, not README)
    2. Detects topic per journal
    3. Groups journals by topic
    4. Creates or updates ``ecosystems/<eco>/kb/<topic>.md``
    5. Moves processed journals to ``ecosystems/<eco>/journal/archived/``
    """
    journal_dir = get_journal_dir(ecosystem)
    archived_dir = journal_dir / "archived"
    kb_dir = get_ecosystem_root(ecosystem) / "kb"

    if not journal_dir.exists():
        sys.stderr.write(f"[kb] Journal directory not found: {journal_dir}\n")
        return 1

    # Collect journal files (skip archived/, README, and other non-journal files)
    journal_files: List[Path] = []
    for p in sorted(journal_dir.glob("*.md")):
        if p.name.lower() == "readme.md":
            continue
        journal_files.append(p)

    if not journal_files:
        print("[kb] No journal entries to consolidate.")
        return 0

    print(f"[kb] Found {len(journal_files)} journal(s) to consolidate\n")

    # Parse and detect topics
    topic_groups: Dict[str, List[tuple]] = {}  # slug -> [(date, filename, body)]

    for jf in journal_files:
        content = jf.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(content)

        # Try to extract date from filename (YYYY-MM-DD-slug.md) or frontmatter
        date_str = fm.get("Date", fm.get("date", ""))
        if not date_str:
            # Extract from filename pattern YYYY-MM-DD-...
            match = re.match(r"(\d{4}-\d{2}-\d{2})", jf.name)
            date_str = match.group(1) if match else "unknown"

        topic = _detect_topic(jf.name, content)
        print(f"  {jf.name}  →  {_topic_display_name(topic)}")

        topic_groups.setdefault(topic, []).append((date_str, jf.name, body))

    print()

    if dry_run:
        print("[kb] Dry run — would create/update these KB articles:")
        for slug, entries in sorted(topic_groups.items()):
            kb_file = kb_dir / f"{slug}.md"
            action = "UPDATE" if kb_file.exists() else "CREATE"
            print(f"  {action}  {kb_file}  ({len(entries)} journal(s))")
        print("\n[kb] No files modified (dry run)")
        return 0

    # Ensure directories exist
    kb_dir.mkdir(parents=True, exist_ok=True)
    archived_dir.mkdir(parents=True, exist_ok=True)

    created = 0
    updated = 0

    for slug, entries in sorted(topic_groups.items()):
        kb_file = kb_dir / f"{slug}.md"
        display_name = _topic_display_name(slug)
        today = datetime.now().strftime("%Y-%m-%d")

        if kb_file.exists():
            # Update existing article
            existing_content = kb_file.read_text(encoding="utf-8")
            existing_fm, existing_body = parse_frontmatter(existing_content)

            new_body = _build_kb_article(slug, entries, existing_body)

            existing_fm["updated"] = today
            new_content = add_frontmatter(new_body, existing_fm)
            kb_file.write_text(new_content, encoding="utf-8")

            print(f"  UPDATED  {kb_file}  (+{len(entries)} entries)")
            updated += 1
        else:
            # Create new article
            new_body = f"# {display_name}\n\n"
            new_body += _build_kb_article(slug, entries)

            fm = {
                "title": display_name,
                "created": today,
                "updated": today,
                "type": "kb",
                "topic": slug,
            }
            new_content = add_frontmatter(new_body, fm)
            kb_file.write_text(new_content, encoding="utf-8")

            print(f"  CREATED  {kb_file}  ({len(entries)} entries)")
            created += 1

    # Archive processed journals
    archived_count = 0
    for jf in journal_files:
        dest = archived_dir / jf.name
        if dest.exists():
            # Avoid overwriting — append a suffix
            stem = jf.stem
            suffix = jf.suffix
            counter = 1
            while dest.exists():
                dest = archived_dir / f"{stem}-{counter}{suffix}"
                counter += 1
        jf.rename(dest)
        archived_count += 1

    print(f"\n[kb] Done: {created} created, {updated} updated, {archived_count} archived")
    return 0


def cmd_kb(args, manifest: Dict[str, Any], ecosystem: str) -> int:
    """Handle kb subcommands."""
    kb_cmd = getattr(args, "kb_cmd", None)
    space = getattr(args, "space", None)
    
    if kb_cmd == "pull":
        return cmd_kb_pull(ecosystem, space)
    elif kb_cmd == "push":
        return cmd_kb_push(ecosystem, space)
    elif kb_cmd == "diff":
        return cmd_kb_diff(ecosystem, space)
    elif kb_cmd == "sync":
        return cmd_kb_sync(ecosystem, space)
    elif kb_cmd == "consolidate":
        dry_run = getattr(args, "dry_run", False)
        return cmd_kb_consolidate(ecosystem, dry_run)
    else:
        sys.stderr.write("Usage: amof kb <pull|push|diff|sync|consolidate>\n")
        return 1


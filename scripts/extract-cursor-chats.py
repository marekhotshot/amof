#!/usr/bin/env python3
"""
extract-cursor-chats.py

Extracts Cursor chat/composer histories from:
  1. Host-side Cursor app data (Windows %APPDATA%\Cursor or Linux ~/.cursor)
  2. All Docker containers (running + stopped) that have cursor-server data

Data format:
  Cursor stores chat data in state.vscdb (SQLite) databases with these keys:
  - composer.composerData -> {allComposers: [{composerId, name, createdAt, ...}]}
  - aiService.prompts -> [{text, commandType}]  (user messages)
  - aiService.generations -> [{unixMs, generationUUID, type, textDescription}]

  Full AI responses are stored server-side only and cannot be extracted locally.

Usage:
    python3 extract-cursor-chats.py [output_directory]
    python3 extract-cursor-chats.py ./out --search supabase,sql,migration,CREATE TABLE

Requirements:
    - python3 (with built-in sqlite3, json, os)
    - docker CLI (optional, for container extraction)
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# ─── Configuration ──────────────────────────────────────────────────────────

TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")

# Keys to extract from state.vscdb
INTERESTING_KEYS = [
    "composer.composerData",
    "aiService.prompts",
    "aiService.generations",
    "composer.planRegistry",
]

# Additional key patterns (LIKE queries)
INTERESTING_PATTERNS = [
    "%chat%",
    "%conversation%",
    "%aiChat%",
    "%aichat%",
]

# ─── Colors ─────────────────────────────────────────────────────────────────

class C:
    RED = "\033[0;31m"
    GREEN = "\033[0;32m"
    YELLOW = "\033[1;33m"
    BLUE = "\033[0;34m"
    CYAN = "\033[0;36m"
    BOLD = "\033[1m"
    NC = "\033[0m"


def log_info(msg):  print(f"{C.BLUE}[INFO]{C.NC}  {msg}")
def log_ok(msg):    print(f"{C.GREEN}[OK]{C.NC}    {msg}")
def log_warn(msg):  print(f"{C.YELLOW}[WARN]{C.NC}  {msg}")
def log_error(msg): print(f"{C.RED}[ERROR]{C.NC} {msg}")
def log_step(msg):  print(f"{C.CYAN}[STEP]{C.NC}  {msg}")


# ─── Helpers ────────────────────────────────────────────────────────────────

def ts_to_str(ts):
    """Convert millisecond timestamp to readable string."""
    if not ts:
        return ""
    try:
        if isinstance(ts, (int, float)):
            if ts > 1e12:
                ts = ts / 1000
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except:
        pass
    return str(ts)


def safe_filename(name, max_len=80):
    """Make a string safe for use as a filename."""
    return re.sub(r'[^a-zA-Z0-9 _-]', '_', str(name))[:max_len].strip()


def run_cmd(cmd, timeout=30):
    """Run a command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except Exception as e:
        return -1, "", str(e)


def open_vscdb(db_path: Path):
    """Open a state.vscdb safely by copying to temp (avoids WAL lock issues)."""
    if not db_path.is_file():
        return None, None

    try:
        tmp = tempfile.mktemp(suffix=".vscdb")
        shutil.copy2(db_path, tmp)
        for ext in ["-wal", "-shm"]:
            src = db_path.parent / (db_path.name + ext)
            if src.exists():
                shutil.copy2(src, tmp + ext)
        conn = sqlite3.connect(tmp)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn, tmp
    except Exception as e:
        log_warn(f"  Cannot open database: {db_path} ({e})")
        return None, None


def close_vscdb(conn, tmp_path):
    """Close connection and clean up temp files."""
    if conn:
        conn.close()
    if tmp_path:
        for ext in ["", "-wal", "-shm"]:
            try:
                os.unlink(tmp_path + ext)
            except:
                pass


# ─── Find Cursor Data Directories ──────────────────────────────────────────

def find_cursor_host_dirs():
    """Find all Cursor data directories on the host."""
    dirs = []
    mnt_c = Path("/mnt/c/Users")
    if mnt_c.is_dir():
        for user_dir in mnt_c.iterdir():
            if not user_dir.is_dir():
                continue
            for sub in ["AppData/Roaming/Cursor", "AppData/Local/Cursor"]:
                appdata = user_dir / sub
                if appdata.is_dir():
                    dirs.append(appdata)

    home = Path.home()
    for p in [home / ".cursor", home / ".config" / "Cursor"]:
        if p.is_dir():
            dirs.append(p)

    xdg = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    xdg_cursor = xdg / "Cursor"
    if xdg_cursor.is_dir() and xdg_cursor not in dirs:
        dirs.append(xdg_cursor)

    return dirs


# ─── Extract from state.vscdb ──────────────────────────────────────────────

def extract_from_vscdb(db_path: Path):
    """Extract all interesting data from a state.vscdb. Returns dict of key -> parsed data."""
    conn, tmp = open_vscdb(db_path)
    if not conn:
        return {}

    results = {}
    try:
        # Exact key matches
        for key in INTERESTING_KEYS:
            try:
                row = conn.execute("SELECT value FROM ItemTable WHERE key = ?", (key,)).fetchone()
                if row and row[0] and len(str(row[0])) > 2:
                    try:
                        results[key] = json.loads(row[0])
                    except json.JSONDecodeError:
                        results[key] = row[0]
            except:
                pass

        # Pattern matches
        for pattern in INTERESTING_PATTERNS:
            try:
                for key, value in conn.execute(
                    "SELECT key, value FROM ItemTable WHERE key LIKE ? AND key NOT IN (" +
                    ",".join("?" * len(INTERESTING_KEYS)) + ")",
                    [pattern] + INTERESTING_KEYS
                ):
                    if value and len(str(value)) > 10:
                        try:
                            results[key] = json.loads(value)
                        except:
                            results[key] = value
            except:
                pass

        # Dump all keys for reference
        try:
            all_keys = conn.execute("SELECT key, length(value) as size FROM ItemTable ORDER BY size DESC").fetchall()
            results["_all_keys"] = all_keys
        except:
            pass

    finally:
        close_vscdb(conn, tmp)

    return results


def get_workspace_info(ws_dir: Path):
    """Get workspace info from workspace.json."""
    ws_json = ws_dir / "workspace.json"
    if not ws_json.is_file():
        return {"id": ws_dir.name, "name": "unknown", "folder": "unknown"}

    try:
        with open(ws_json) as f:
            d = json.load(f)
        folder = d.get("folder", d.get("workspace", "unknown"))
        return {"id": ws_dir.name, "raw": d, "name": folder, "folder": folder}
    except:
        return {"id": ws_dir.name, "name": "unknown", "folder": "unknown"}


# ─── Process Host-Side Data ────────────────────────────────────────────────

def process_host_cursor_data(cursor_dir: Path):
    """Process a host-side Cursor data directory. Returns list of workspace results."""
    workspaces = []

    log_step(f"Processing host Cursor data: {cursor_dir}")

    # 1. workspaceStorage
    ws_storage = cursor_dir / "User" / "workspaceStorage"
    if ws_storage.is_dir():
        log_info("  Scanning workspaceStorage...")
        for ws_dir in sorted(ws_storage.iterdir()):
            if not ws_dir.is_dir():
                continue
            state_db = ws_dir / "state.vscdb"
            if not state_db.is_file():
                continue

            ws_info = get_workspace_info(ws_dir)
            log_info(f"    Workspace: {ws_info['name']}")

            data = extract_from_vscdb(state_db)
            if not data or (not data.get("composer.composerData") and not data.get("aiService.prompts")):
                continue

            composers = []
            cd = data.get("composer.composerData", {})
            if isinstance(cd, dict):
                composers = cd.get("allComposers", [])

            prompts = data.get("aiService.prompts", [])
            generations = data.get("aiService.generations", [])

            if not composers and not prompts:
                continue

            ws_result = {
                "workspace_info": ws_info,
                "composers": composers,
                "prompts": prompts if isinstance(prompts, list) else [],
                "generations": generations if isinstance(generations, list) else [],
                "source": f"host/{cursor_dir.name}/{ws_dir.name}",
                "all_keys": data.get("_all_keys", []),
                "extra_data": {k: v for k, v in data.items() if k not in [
                    "composer.composerData", "aiService.prompts",
                    "aiService.generations", "_all_keys", "composer.planRegistry"
                ]},
            }

            plan_registry = data.get("composer.planRegistry")
            if plan_registry:
                ws_result["plan_registry"] = plan_registry

            n_composers = len(composers)
            n_prompts = len(ws_result["prompts"])
            n_gens = len(ws_result["generations"])
            log_ok(f"      {n_composers} composer(s), {n_prompts} prompt(s), {n_gens} generation(s)")

            workspaces.append(ws_result)

    # 2. globalStorage
    global_storage = cursor_dir / "User" / "globalStorage"
    if global_storage.is_dir():
        log_info("  Scanning globalStorage...")
        global_state = global_storage / "state.vscdb"
        if global_state.is_file():
            data = extract_from_vscdb(global_state)
            if data.get("composer.composerData") or data.get("aiService.prompts"):
                workspaces.append({
                    "workspace_info": {"id": "global", "name": "Global Storage", "folder": "global"},
                    "composers": data.get("composer.composerData", {}).get("allComposers", []) if isinstance(data.get("composer.composerData"), dict) else [],
                    "prompts": data.get("aiService.prompts", []) if isinstance(data.get("aiService.prompts"), list) else [],
                    "generations": data.get("aiService.generations", []) if isinstance(data.get("aiService.generations"), list) else [],
                    "source": f"host/{cursor_dir.name}/global",
                    "all_keys": data.get("_all_keys", []),
                    "extra_data": {},
                })

    log_info(f"  Found {len(workspaces)} workspace(s) with chat data")
    return workspaces


# ─── Process Docker Containers ──────────────────────────────────────────────

def process_docker_containers():
    """Extract cursor-server data from all Docker containers. Returns list of workspace results."""
    workspaces = []
    log_step("Scanning Docker containers...")

    rc, _, _ = run_cmd(["docker", "info"])
    if rc != 0:
        log_warn("Docker is not accessible. Skipping container extraction.")
        return workspaces

    rc, out, _ = run_cmd(["docker", "ps", "-a", "--format",
                          "{{.ID}}|{{.Names}}|{{.Image}}|{{.Status}}|{{.CreatedAt}}"])
    if rc != 0 or not out:
        log_warn("No Docker containers found.")
        return workspaces

    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) < 5:
            continue
        cid, name, image, status, created = parts[0], parts[1], parts[2], parts[3], parts[4]

        log_info(f"  Container: {name} ({cid}) - {status}")

        cursor_paths = [
            "/root/.cursor-server/data",
            "/home/vscode/.cursor-server/data",
            "/home/node/.cursor-server/data",
            "/home/nex/.cursor-server/data",
        ]

        if "up" in status.lower():
            rc2, user, _ = run_cmd(["docker", "exec", cid, "whoami"])
            if rc2 == 0 and user and user != "root":
                cursor_paths.insert(0, f"/home/{user}/.cursor-server/data")

        for cursor_path in cursor_paths:
            with tempfile.TemporaryDirectory() as tmp_dir:
                rc3, _, _ = run_cmd(["docker", "cp", f"{cid}:{cursor_path}", f"{tmp_dir}/cursor-data"])
                if rc3 == 0 and Path(f"{tmp_dir}/cursor-data").exists():
                    log_ok(f"    Found cursor-server data at: {cursor_path}")

                    # Find and process any vscdb files
                    data_root = Path(f"{tmp_dir}/cursor-data")
                    for vscdb in data_root.rglob("state.vscdb"):
                        data = extract_from_vscdb(vscdb)
                        if data.get("composer.composerData") or data.get("aiService.prompts"):
                            ws_dir = vscdb.parent
                            ws_info = get_workspace_info(ws_dir)
                            ws_info["container"] = {"id": cid, "name": name, "image": image, "status": status}

                            composers = []
                            cd = data.get("composer.composerData", {})
                            if isinstance(cd, dict):
                                composers = cd.get("allComposers", [])

                            ws_result = {
                                "workspace_info": ws_info,
                                "composers": composers,
                                "prompts": data.get("aiService.prompts", []) if isinstance(data.get("aiService.prompts"), list) else [],
                                "generations": data.get("aiService.generations", []) if isinstance(data.get("aiService.generations"), list) else [],
                                "source": f"container/{name}/{ws_dir.name}",
                                "all_keys": data.get("_all_keys", []),
                                "extra_data": {},
                            }
                            workspaces.append(ws_result)
                            log_ok(f"      {len(composers)} composer(s), {len(ws_result['prompts'])} prompt(s)")
                    break

        else:
            log_warn(f"    No cursor-server data found")

    log_info(f"  Extracted chat data from {len(workspaces)} container workspace(s)")
    return workspaces


# ─── Markdown Generation ───────────────────────────────────────────────────

def workspace_to_markdown(ws):
    """Convert a workspace result to a Markdown document."""
    lines = []
    info = ws["workspace_info"]

    lines.append(f"# Workspace: {info.get('name', 'Unknown')}")
    lines.append("")
    lines.append(f"**ID:** `{info.get('id', 'unknown')}`")
    lines.append(f"**Source:** `{ws['source']}`")
    if "container" in info:
        c = info["container"]
        lines.append(f"**Container:** {c['name']} ({c['id']}) - {c['status']}")
    lines.append("")

    # Composer sessions
    composers = ws.get("composers", [])
    if composers:
        lines.append("---")
        lines.append("")
        lines.append(f"## Composer Sessions ({len(composers)})")
        lines.append("")

        # Sort by creation time
        sorted_composers = sorted(composers, key=lambda c: c.get("createdAt", 0), reverse=True)

        for i, comp in enumerate(sorted_composers):
            name = comp.get("name", comp.get("composerId", f"Session {i+1}"))
            created = ts_to_str(comp.get("createdAt"))
            updated = ts_to_str(comp.get("lastUpdatedAt"))
            mode = comp.get("unifiedMode", "")
            lines_added = comp.get("totalLinesAdded", 0)
            lines_removed = comp.get("totalLinesRemoved", 0)
            files_changed = comp.get("filesChangedCount", 0)
            subtitle = comp.get("subtitle", "")
            context_pct = comp.get("contextUsagePercent", 0)
            is_archived = comp.get("isArchived", False)
            branch = comp.get("createdOnBranch", "")

            status_str = " (archived)" if is_archived else ""
            lines.append(f"### {i+1}. {name}{status_str}")
            lines.append("")
            lines.append(f"- **Created:** {created}")
            if updated:
                lines.append(f"- **Last Updated:** {updated}")
            if mode:
                lines.append(f"- **Mode:** {mode}")
            if branch:
                lines.append(f"- **Branch:** {branch}")
            if files_changed:
                lines.append(f"- **Files Changed:** {files_changed}")
            if lines_added or lines_removed:
                lines.append(f"- **Lines:** +{lines_added} / -{lines_removed}")
            if context_pct:
                lines.append(f"- **Context Usage:** {context_pct:.1f}%")
            if subtitle:
                lines.append(f"- **Files:** {subtitle}")
            lines.append("")

    # User prompts
    prompts = ws.get("prompts", [])
    if prompts:
        lines.append("---")
        lines.append("")
        lines.append(f"## User Prompts ({len(prompts)})")
        lines.append("")
        lines.append("All messages sent to the AI assistant, in chronological order:")
        lines.append("")

        for i, prompt in enumerate(prompts):
            text = prompt.get("text", "")
            cmd_type = prompt.get("commandType", "")
            if not text:
                continue

            lines.append(f"### Prompt {i+1}")
            lines.append("")
            # Wrap in a blockquote for readability
            for line in text.splitlines():
                lines.append(f"> {line}")
            lines.append("")

    # Generations
    generations = ws.get("generations", [])
    if generations:
        lines.append("---")
        lines.append("")
        lines.append(f"## AI Generations ({len(generations)})")
        lines.append("")
        lines.append("Metadata about AI-generated responses (full responses are stored server-side):")
        lines.append("")

        # Sort by timestamp
        sorted_gens = sorted(generations, key=lambda g: g.get("unixMs", 0))

        for i, gen in enumerate(sorted_gens):
            ts = ts_to_str(gen.get("unixMs"))
            gen_type = gen.get("type", "unknown")
            desc = gen.get("textDescription", "")
            uuid = gen.get("generationUUID", "")

            lines.append(f"#### Generation {i+1} ({ts}) [{gen_type}]")
            lines.append("")
            if desc:
                # Truncate very long descriptions (error logs etc.)
                if len(desc) > 500:
                    desc = desc[:500] + "... (truncated)"
                for line in desc.splitlines():
                    lines.append(f"> {line}")
            lines.append("")

    return "\n".join(lines)


def generate_index(all_workspaces):
    """Generate INDEX.md with overview of all extracted data."""
    lines = []
    lines.append("# Cursor Chat Export Index")
    lines.append("")
    lines.append(f"**Exported:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    total_composers = sum(len(ws.get("composers", [])) for ws in all_workspaces)
    total_prompts = sum(len(ws.get("prompts", [])) for ws in all_workspaces)
    total_gens = sum(len(ws.get("generations", [])) for ws in all_workspaces)

    lines.append(f"**Workspaces:** {len(all_workspaces)}")
    lines.append(f"**Total Composer Sessions:** {total_composers}")
    lines.append(f"**Total User Prompts:** {total_prompts}")
    lines.append(f"**Total AI Generations:** {total_gens}")
    lines.append("")

    lines.append("## Note on Data Completeness")
    lines.append("")
    lines.append("Cursor stores **user prompts** locally but **AI responses are stored server-side**.")
    lines.append("This export includes:")
    lines.append("- Full text of all user messages/prompts")
    lines.append("- Composer session metadata (name, timestamps, files changed, lines added/removed)")
    lines.append("- AI generation timestamps and brief descriptions")
    lines.append("- It does NOT include the full text of AI assistant responses")
    lines.append("")

    lines.append("## Workspaces")
    lines.append("")
    lines.append("| # | Workspace | Composers | Prompts | Generations | Source |")
    lines.append("|---|-----------|-----------|---------|-------------|--------|")

    for i, ws in enumerate(all_workspaces):
        info = ws["workspace_info"]
        name = info.get("name", "unknown")
        # Shorten long names
        if len(name) > 60:
            name = "..." + name[-57:]
        n_comp = len(ws.get("composers", []))
        n_prompt = len(ws.get("prompts", []))
        n_gen = len(ws.get("generations", []))
        source = ws["source"]
        lines.append(f"| {i+1} | {name} | {n_comp} | {n_prompt} | {n_gen} | `{source}` |")

    lines.append("")

    # Composer sessions summary
    lines.append("## All Composer Sessions (by date)")
    lines.append("")
    lines.append("| # | Session Name | Created | Updated | Mode | Lines +/- | Files | Workspace |")
    lines.append("|---|-------------|---------|---------|------|-----------|-------|-----------|")

    all_sessions = []
    for ws in all_workspaces:
        for comp in ws.get("composers", []):
            comp["_workspace_name"] = ws["workspace_info"].get("name", "unknown")
            all_sessions.append(comp)

    all_sessions.sort(key=lambda c: c.get("createdAt", 0), reverse=True)

    for i, comp in enumerate(all_sessions):
        name = comp.get("name", "unnamed")[:50]
        created = ts_to_str(comp.get("createdAt"))
        updated = ts_to_str(comp.get("lastUpdatedAt"))
        mode = comp.get("unifiedMode", "")
        added = comp.get("totalLinesAdded", 0)
        removed = comp.get("totalLinesRemoved", 0)
        files = comp.get("filesChangedCount", 0)
        ws_name = comp.get("_workspace_name", "")
        if len(ws_name) > 30:
            ws_name = "..." + ws_name[-27:]
        lines.append(f"| {i+1} | {name} | {created} | {updated} | {mode} | +{added}/-{removed} | {files} | {ws_name} |")

    return "\n".join(lines)


# ─── Search for terms in extracted content ────────────────────────────────────

def search_extracted_content(all_workspaces, search_terms, output_dir):
    """Search prompts and composer content for given terms. Returns list of hits."""
    hits = []
    terms = [t.strip().lower() for t in search_terms if t.strip()]

    def check_text(text, context):
        if not text or not isinstance(text, str):
            return
        text_lower = text.lower()
        for term in terms:
            if term in text_lower:
                # Extract surrounding context (up to 200 chars)
                idx = text_lower.find(term)
                start = max(0, idx - 80)
                end = min(len(text), idx + len(term) + 120)
                snippet = text[start:end]
                if start > 0:
                    snippet = "..." + snippet
                if end < len(text):
                    snippet = snippet + "..."
                hits.append({
                    "term": term,
                    "context": context,
                    "snippet": snippet,
                    "full_len": len(text),
                })
                break

    for ws in all_workspaces:
        info = ws["workspace_info"]
        ws_name = info.get("name", "unknown")
        source = ws.get("source", "")

        for i, prompt in enumerate(ws.get("prompts", [])):
            text = prompt.get("text", "")
            check_text(text, f"Workspace: {ws_name} | Prompt #{i+1} | Source: {source}")

        for comp in ws.get("generations", []):
            desc = comp.get("textDescription", "")
            check_text(desc, f"Workspace: {ws_name} | Generation | Source: {source}")

    return hits


def write_search_report(hits, search_terms, output_path):
    """Write SEARCH_HITS.md with all matches."""
    lines = []
    lines.append("# Search Results in Extracted Cursor Chats")
    lines.append("")
    lines.append(f"**Search terms:** `{', '.join(search_terms)}`")
    lines.append(f"**Total hits:** {len(hits)}")
    lines.append("")
    lines.append("---")
    lines.append("")

    for i, h in enumerate(hits, 1):
        lines.append(f"## Hit {i}: `{h['term']}`")
        lines.append("")
        lines.append(f"**Context:** {h['context']}")
        lines.append("")
        lines.append("```")
        lines.append(h["snippet"])
        lines.append("```")
        lines.append("")
        lines.append("---")
        lines.append("")

    output_path.write_text("\n".join(lines))


# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Extract Cursor chat histories")
    parser.add_argument(
        "output_dir",
        nargs="?",
        default="./cursor-chats-export",
        help="Output directory (default: ./cursor-chats-export)",
    )
    parser.add_argument(
        "--search",
        type=str,
        default="",
        help="Comma-separated terms to search for after extraction (e.g. supabase,sql,migration)",
    )
    args = parser.parse_args()

    output_base = Path(args.output_dir)
    OUTPUT_DIR = output_base.parent / f"{output_base.name}_{TIMESTAMP}"
    search_terms = [t.strip() for t in args.search.split(",") if t.strip()] if args.search else []

    print()
    print("========================================")
    print("  Cursor Chat History Extractor")
    print("========================================")
    print()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    log_info(f"Output directory: {OUTPUT_DIR}")
    print()

    all_workspaces = []

    # Step 1: Host-side
    log_step("Step 1/3: Scanning host Cursor data directories...")
    cursor_dirs = find_cursor_host_dirs()
    if cursor_dirs:
        for cursor_dir in cursor_dirs:
            ws_list = process_host_cursor_data(cursor_dir)
            all_workspaces.extend(ws_list)
    else:
        log_warn("No host Cursor data directories found.")
    print()

    # Step 2: Docker containers
    log_step("Step 2/3: Extracting from Docker containers...")
    container_ws = process_docker_containers()
    all_workspaces.extend(container_ws)
    print()

    # Step 3: Generate output
    log_step("Step 3/3: Generating Markdown output...")

    # Sort workspaces: those with most composers/prompts first
    all_workspaces.sort(
        key=lambda ws: len(ws.get("composers", [])) + len(ws.get("prompts", [])),
        reverse=True
    )

    readable_dir = OUTPUT_DIR / "readable"
    readable_dir.mkdir(parents=True, exist_ok=True)

    # Write per-workspace files
    for i, ws in enumerate(all_workspaces):
        info = ws["workspace_info"]
        ws_name = safe_filename(info.get("name", "unknown"))
        md_content = workspace_to_markdown(ws)
        md_file = readable_dir / f"{i+1:03d}_{ws_name}.md"
        md_file.write_text(md_content)
        log_ok(f"  Written: {md_file.name}")

    # Write index
    index_content = generate_index(all_workspaces)
    (readable_dir / "INDEX.md").write_text(index_content)
    log_ok(f"  Written: INDEX.md")

    # Write raw JSON dump
    raw_dir = OUTPUT_DIR / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    for i, ws in enumerate(all_workspaces):
        info = ws["workspace_info"]
        ws_name = safe_filename(info.get("name", "unknown"))
        raw_file = raw_dir / f"{i+1:03d}_{ws_name}.json"

        # Don't include _all_keys in the raw dump (too noisy)
        dump = {k: v for k, v in ws.items() if k != "all_keys"}
        with open(raw_file, "w") as f:
            json.dump(dump, f, indent=2, default=str)

    # Write key inventory (useful for debugging)
    inventory_dir = OUTPUT_DIR / "inventory"
    inventory_dir.mkdir(parents=True, exist_ok=True)

    for i, ws in enumerate(all_workspaces):
        info = ws["workspace_info"]
        ws_name = safe_filename(info.get("name", "unknown"))
        inv_file = inventory_dir / f"{i+1:03d}_{ws_name}_keys.txt"
        all_keys = ws.get("all_keys", [])
        with open(inv_file, "w") as f:
            f.write(f"Workspace: {info.get('name')}\n")
            f.write(f"ID: {info.get('id')}\n")
            f.write(f"Source: {ws['source']}\n\n")
            f.write(f"{'Size':>12}  Key\n")
            f.write(f"{'----':>12}  ---\n")
            for key, size in all_keys:
                f.write(f"{size:>12,}  {key}\n")

    print()
    print("========================================")
    print("  Extraction Complete!")
    print("========================================")
    print()

    total_composers = sum(len(ws.get("composers", [])) for ws in all_workspaces)
    total_prompts = sum(len(ws.get("prompts", [])) for ws in all_workspaces)
    total_gens = sum(len(ws.get("generations", [])) for ws in all_workspaces)

    log_ok(f"Output: {OUTPUT_DIR}")
    log_info(f"Workspaces with chat data: {len(all_workspaces)}")
    log_info(f"Total composer sessions:   {total_composers}")
    log_info(f"Total user prompts:        {total_prompts}")
    log_info(f"Total AI generations:      {total_gens}")
    print()
    log_info(f"Start with: {readable_dir / 'INDEX.md'}")
    log_info(f"Raw JSON:   {raw_dir}")
    print()
    log_warn("Note: Full AI responses are stored server-side and cannot be extracted locally.")
    log_warn("This export contains user prompts, session metadata, and generation timestamps.")
    print()


if __name__ == "__main__":
    main()

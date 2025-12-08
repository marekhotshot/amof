#!/usr/bin/env python3
"""AMOF CLI v0.1 implementation.

Commands:
- sync
- status
- context <service>
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

MANIFEST_PATH = Path("amof.yaml")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AMOF CLI v0.1")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser(
        "sync", help="Synchronize repositories defined in amof.yaml"
    )
    sync_parser.add_argument(
        "--repo",
        action="append",
        dest="repos",
        help="Sync only the specified repo(s); can be repeated",
    )

    status_parser = subparsers.add_parser("status", help="Show repository status")
    status_parser.add_argument(
        "--repo",
        action="append",
        dest="repos",
        help="Show status only for the specified repo(s); can be repeated",
    )

    add_repo_parser = subparsers.add_parser(
        "add-repo",
        help="Append a repository to amof.yaml and optionally sync it",
    )
    add_repo_parser.add_argument("name", help="Repository name (manifest key)")
    add_repo_parser.add_argument("url", help="Git URL")
    add_repo_parser.add_argument(
        "--branch", default="main", help="Branch to track (default: main)"
    )
    add_repo_parser.add_argument(
        "--path",
        help="Local path; defaults to repos/<name>",
    )
    add_repo_parser.add_argument(
        "--include",
        action="append",
        default=[],
        help="Include glob (can repeat). Default is all files",
    )
    add_repo_parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Exclude glob (can repeat)",
    )
    add_repo_parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace existing repo with the same name",
    )
    add_repo_parser.add_argument(
        "--sync",
        action="store_true",
        help="Run sync for the new repo after updating the manifest",
    )

    context_parser = subparsers.add_parser(
        "context", help="Generate context for a given service"
    )
    context_parser.add_argument("service", help="Service name from manifest")

    return parser.parse_args()


def parse_scalar(value: str) -> Any:
    if value in {"true", "false"}:
        return value == "true"
    if value in {"null", "~"}:
        return None
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            pass
    if (value.startswith("\"") and value.endswith("\"")) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    return value


def simple_parse_yaml(text: str) -> Any:
    lines = [line.rstrip("\n") for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]

    def parse_block(index: int, indent: int) -> Tuple[Any, int]:
        data: Any = None
        while index < len(lines):
            line = lines[index]
            current_indent = len(line) - len(line.lstrip(" "))
            if current_indent < indent:
                break
            content = line.strip()
            if content.startswith("- "):
                if data is None:
                    data = []
                elif not isinstance(data, list):
                    raise ValueError("Mixed list and mapping at same level")
                item_text = content[2:].strip()
                index += 1
                if item_text:
                    if ":" in item_text:
                        key, val = item_text.split(":", 1)
                        item: Dict[str, Any] = {}
                        if val.strip():
                            item[key.strip()] = parse_scalar(val.strip())
                            nested, index = parse_block(index, current_indent + 2)
                            if isinstance(nested, dict):
                                item.update(nested)
                        else:
                            nested, index = parse_block(index, current_indent + 2)
                            item[key.strip()] = nested
                        data.append(item)
                    else:
                        value = parse_scalar(item_text)
                        nested, index = parse_block(index, current_indent + 2)
                        if nested not in ({}, [], None):
                            value = nested
                        data.append(value)
                else:
                    nested, index = parse_block(index, current_indent + 2)
                    data.append(nested)
            else:
                if ":" not in content:
                    raise ValueError(f"Invalid line in YAML: {line}")
                if data is None:
                    data = {}
                elif not isinstance(data, dict):
                    raise ValueError("Mixed mapping and list at same level")
                key, val = content.split(":", 1)
                key = key.strip()
                val = val.strip()
                index += 1
                if val:
                    data[key] = parse_scalar(val)
                else:
                    nested, index = parse_block(index, current_indent + 2)
                    data[key] = nested
        if data is None:
            data = {}
        return data, index

    parsed, _ = parse_block(0, 0)
    return parsed


def load_manifest(path: Path = MANIFEST_PATH) -> Dict[str, Any]:
    if not path.exists():
        sys.stderr.write(f"Manifest not found at {path}\n")
        sys.exit(1)

    data = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        return yaml.safe_load(data)
    except Exception:
        try:
            return simple_parse_yaml(data)
        except Exception as exc:  # pragma: no cover - fallback errors
            sys.stderr.write(f"Failed to parse manifest: {exc}\n")
            sys.exit(1)


def format_scalar(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if any(ch in text for ch in [":", "#", "\"", "'", "{", "}", ",", "[", "]", "\n"]):
        return json.dumps(text)
    return text


def dump_yaml(data: Any, indent: int = 0) -> str:
    lines: List[str] = []
    spacer = " " * indent
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                lines.append(f"{spacer}{key}:")
                lines.append(dump_yaml(value, indent + 2))
            else:
                lines.append(f"{spacer}{key}: {format_scalar(value)}")
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, (dict, list)):
                lines.append(f"{spacer}-")
                lines.append(dump_yaml(item, indent + 2))
            else:
                lines.append(f"{spacer}- {format_scalar(item)}")
    else:
        lines.append(f"{spacer}{format_scalar(data)}")
    return "\n".join(lines)


def write_manifest(manifest: Dict[str, Any], path: Path = MANIFEST_PATH) -> None:
    try:
        import yaml  # type: ignore

        text = yaml.safe_dump(manifest, sort_keys=False)
    except Exception:
        text = dump_yaml(manifest)
    path.write_text(text + "\n", encoding="utf-8")


def run_command(args: List[str], cwd: Path | None = None) -> Tuple[int, str]:
    process = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return process.returncode, process.stdout.strip()


def prepare_patterns(patterns: Iterable[str]) -> List[str]:
    prepared = []
    for pattern in patterns:
        if pattern.endswith("/"):
            prepared.append(pattern + "**")
        else:
            prepared.append(pattern)
    return prepared


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def cmd_sync(manifest: Dict[str, Any], only: set[str] | None = None) -> int:
    repos = manifest.get("repos", [])
    if not repos:
        sys.stderr.write("No repositories defined in manifest.\n")
        return 1

    overall = 0
    for repo in repos:
        name = repo.get("name")
        if only and name not in only:
            continue
        url = repo.get("url")
        branch = repo.get("branch", "main")
        path = Path(repo.get("path", name))

        if not name or not url:
            sys.stderr.write("Skipping repo with missing name or url in manifest.\n")
            overall = 1
            continue

        actions: List[str] = []
        if not path.exists():
            code, out = run_command(["git", "clone", url, str(path)])
            if code != 0:
                sys.stderr.write(f"[sync:{name}] clone failed: {out}\n")
                overall = 1
                continue
            actions.append("cloned")
        else:
            code, out = run_command(["git", "-C", str(path), "fetch", "--all"])
            if code != 0:
                sys.stderr.write(f"[sync:{name}] fetch failed: {out}\n")
                overall = 1
                continue
            actions.append("fetched")

        code, out = run_command(["git", "-C", str(path), "checkout", branch])
        if code != 0:
            sys.stderr.write(f"[sync:{name}] checkout failed: {out}\n")
            overall = 1
            continue
        actions.append(f"checked out {branch}")

        code, out = run_command(["git", "-C", str(path), "pull", "origin", branch])
        if code != 0:
            sys.stderr.write(f"[sync:{name}] pull failed: {out}\n")
            overall = 1
            continue
        actions.append("updated")

        print(f"[sync] {name} ({path}): {', '.join(actions)}")
    return overall


def get_git_branch(path: Path) -> str | None:
    code, out = run_command(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
    if code == 0:
        return out
    return None


def is_git_dirty(path: Path) -> bool:
    code, out = run_command(["git", "status", "--porcelain"], cwd=path)
    return code == 0 and bool(out)


def cmd_status(manifest: Dict[str, Any], only: set[str] | None = None) -> int:
    repos = manifest.get("repos", [])
    if not repos:
        sys.stderr.write("No repositories defined in manifest.\n")
        return 1

    header = f"{ 'REPO':<12}{'PATH':<25}{'BRANCH':<25}{'STATUS'}"
    print(header)
    print("-" * len(header))
    for repo in repos:
        name = repo.get("name")
        if only and name not in only:
            continue
        expected_branch = repo.get("branch", "main")
        path = Path(repo.get("path", name))
        if not path.exists():
            print(f"{name:<12}{str(path):<25}{'-':<25}MISSING")
            continue
        current_branch = get_git_branch(path) or "?"
        dirty = is_git_dirty(path)
        status = "OK"
        if current_branch != expected_branch:
            status = "WRONG_BRANCH"
        if dirty:
            status = "DIRTY" if status == "OK" else f"{status}+DIRTY"
        print(f"{name:<12}{str(path):<25}{current_branch + ' / ' + expected_branch:<25}{status}")
    return 0


def find_repo(manifest: Dict[str, Any], name: str) -> Dict[str, Any] | None:
    for repo in manifest.get("repos", []):
        if repo.get("name") == name:
            return repo
    return None


def gather_files(
    repo_path: Path,
    include: List[str],
    exclude: List[str],
    max_files: int,
    max_bytes: int,
) -> List[Dict[str, Any]]:
    files: List[Dict[str, Any]] = []
    total_bytes = 0
    for file_path in sorted(repo_path.rglob("*")):
        if not file_path.is_file():
            continue
        rel_path = file_path.relative_to(repo_path).as_posix()
        if include and not matches_any(rel_path, include):
            continue
        if exclude and matches_any(rel_path, exclude):
            continue
        size = file_path.stat().st_size
        if total_bytes + size > max_bytes or len(files) >= max_files:
            break
        files.append(
            {
                "path": rel_path,
                "size": size,
                "type": file_path.suffix.lstrip(".") or "unknown",
            }
        )
        total_bytes += size
    return files


def find_todos(repo_path: Path, indexed_files: List[Dict[str, Any]]) -> List[str]:
    todos: List[str] = []
    for entry in indexed_files:
        rel_path = entry["path"]
        file_path = repo_path / rel_path
        try:
            with file_path.open("r", encoding="utf-8", errors="ignore") as f:
                for idx, line in enumerate(f, start=1):
                    if "TODO" in line or "FIXME" in line:
                        todos.append(f"{rel_path}:L{idx} {line.strip()}")
        except Exception:
            continue
    return todos


def summarize_repo(repo_path: Path, indexed_files: List[Dict[str, Any]]) -> str:
    top_dirs = sorted([p.name for p in repo_path.iterdir() if p.is_dir()])
    notable: List[str] = []
    notable_names = ["README.md", "Dockerfile", "Makefile"]
    notable_names += [f"main.{ext}" for ext in ("py", "js", "ts", "go", "rs", "java")]
    for name in notable_names:
        candidate = repo_path / name
        if candidate.exists():
            notable.append(candidate.relative_to(repo_path).as_posix())
    todos = find_todos(repo_path, indexed_files)

    lines = ["# Context Summary", "", "## Top-level directories"]
    if top_dirs:
        lines.extend([f"- {d}" for d in top_dirs])
    else:
        lines.append("- (none)")

    lines.append("\n## Notable files")
    if notable:
        lines.extend([f"- {n}" for n in notable])
    else:
        lines.append("- (none found)")

    lines.append("\n## TODO / FIXME markers")
    if todos:
        lines.extend([f"- {t}" for t in todos])
    else:
        lines.append("- (none found)")
    return "\n".join(lines)


def cmd_context(manifest: Dict[str, Any], service: str) -> int:
    repo = find_repo(manifest, service)
    if not repo:
        sys.stderr.write(f"Service '{service}' not found in manifest.\n")
        return 1

    repo_path = Path(repo.get("path", service))
    if not repo_path.exists():
        sys.stderr.write(f"Repository path missing: {repo_path}\n")
        return 1

    ctx_config = manifest.get("context", {})
    max_files = int(ctx_config.get("max_files", 200))
    max_bytes = int(ctx_config.get("summary_tokens", 8000))

    include = prepare_patterns(repo.get("include", ["**"]))
    exclude = prepare_patterns(repo.get("exclude", []))

    indexed_files = gather_files(repo_path, include, exclude, max_files, max_bytes)

    output_dir = Path("context") / service
    output_dir.mkdir(parents=True, exist_ok=True)

    index_path = output_dir / "index.json"
    with index_path.open("w", encoding="utf-8") as f:
        json.dump(indexed_files, f, indent=2)

    summary_content = summarize_repo(repo_path, indexed_files)
    (output_dir / "summary.md").write_text(summary_content, encoding="utf-8")

    print(f"Context generated under {output_dir}")
    print(f"Indexed files: {len(indexed_files)}")
    return 0


def upsert_repo(
    manifest: Dict[str, Any],
    name: str,
    url: str,
    branch: str,
    path: str | None,
    include: List[str],
    exclude: List[str],
    replace: bool,
) -> None:
    repo_entry = {
        "name": name,
        "url": url,
        "branch": branch,
        "path": path or f"repos/{name}",
    }
    if include:
        repo_entry["include"] = include
    if exclude:
        repo_entry["exclude"] = exclude

    repos = manifest.setdefault("repos", [])
    existing_idx = next((idx for idx, r in enumerate(repos) if r.get("name") == name), None)
    if existing_idx is not None:
        if not replace:
            sys.stderr.write(
                f"Repository '{name}' already exists in manifest; use --replace to overwrite.\n"
            )
            sys.exit(1)
        repos[existing_idx] = repo_entry
    else:
        repos.append(repo_entry)


def cmd_add_repo(args: argparse.Namespace, manifest: Dict[str, Any]) -> int:
    upsert_repo(
        manifest,
        name=args.name,
        url=args.url,
        branch=args.branch,
        path=args.path,
        include=args.include,
        exclude=args.exclude,
        replace=args.replace,
    )

    write_manifest(manifest)
    print(f"Added {args.name} to manifest at {MANIFEST_PATH}")

    if args.sync:
        return cmd_sync(manifest, only={args.name})
    return 0


def main() -> None:
    args = parse_args()
    manifest = load_manifest()

    if args.command == "sync":
        only = set(args.repos) if getattr(args, "repos", None) else None
        sys.exit(cmd_sync(manifest, only=only))
    if args.command == "status":
        only = set(args.repos) if getattr(args, "repos", None) else None
        sys.exit(cmd_status(manifest, only=only))
    if args.command == "context":
        sys.exit(cmd_context(manifest, args.service))
    if args.command == "add-repo":
        sys.exit(cmd_add_repo(args, manifest))


if __name__ == "__main__":
    main()

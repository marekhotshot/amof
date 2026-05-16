"""Manifest validation and management commands."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

from ..manifest import get_manifest_path, load_manifest, validate_manifest, simple_parse_yaml


def cmd_manifest_validate(ecosystem: str | None, args: Any) -> None:
    """Validate manifest file and show detailed errors."""
    path = get_manifest_path(ecosystem)
    
    if not path.exists():
        sys.stderr.write(f"\n✗ Manifest not found: {path}\n")
        sys.exit(1)
    
    # Parse without validation first
    data = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
        manifest = yaml.safe_load(data)
    except Exception:
        try:
            manifest = simple_parse_yaml(data)
        except Exception as exc:
            sys.stderr.write(f"\n✗ Failed to parse manifest: {path}\n\n")
            sys.stderr.write(f"Error: {exc}\n\n")
            sys.stderr.write("Common causes:\n")
            sys.stderr.write("  • Incorrect indentation (YAML uses spaces, not tabs)\n")
            sys.stderr.write("  • Missing colon after key name\n")
            sys.stderr.write("  • Unquoted special characters (: # { } [ ])\n")
            sys.exit(1)
    
    if manifest is None:
        sys.stderr.write(f"\n✗ Manifest is empty: {path}\n")
        sys.exit(1)
    
    # Validate
    strict = getattr(args, "strict", False)
    errors = validate_manifest(manifest, detailed=True, strict=strict)
    
    if not errors:
        print(f"\n✓ Manifest is valid: {path}\n")
        
        # Show summary
        repos = manifest.get("repos", [])
        print(f"Summary:")
        print(f"  • {len(repos)} repository(ies)")
        
        readonly_count = sum(1 for r in repos if r.get("readonly", False))
        if readonly_count:
            print(f"  • {readonly_count} readonly")
        
        if "guardrails" in manifest:
            no_touch = manifest["guardrails"].get("no_touch_paths", [])
            if no_touch:
                print(f"  • {len(no_touch)} guardrail pattern(s)")
        
        print()
        sys.exit(0)
    
    # Show errors
    sys.stderr.write(f"\n✗ Manifest validation failed: {path}\n\n")
    for error in errors:
        sys.stderr.write(f"  {error}\n")
    
    sys.stderr.write(f"\n✓ Fix the above issues and run again\n")
    sys.exit(1)


def cmd_manifest_show(ecosystem: str | None, args: Any) -> None:
    """Show manifest contents in a readable format."""
    manifest = load_manifest(ecosystem, validate=False)
    path = get_manifest_path(ecosystem)
    
    print(f"\nManifest: {path}\n")
    print("=" * 60)
    
    # Repos
    repos = manifest.get("repos", [])
    print(f"\nRepositories ({len(repos)}):")
    for i, repo in enumerate(repos, 1):
        name = repo.get("name", "<unnamed>")
        url = repo.get("url", "<no url>")
        branch = repo.get("branch", "main")
        readonly = " [readonly]" if repo.get("readonly", False) else ""
        print(f"  {i}. {name}{readonly}")
        print(f"     URL: {url}")
        print(f"     Branch: {branch}")
        if "path" in repo:
            print(f"     Path: {repo['path']}")
    
    # Workspace
    if "workspace" in manifest:
        ws = manifest["workspace"]
        print(f"\nWorkspace:")
        if "branch_prefix" in ws:
            print(f"  Branch prefix: {ws['branch_prefix']}")
        if "repo_branch_prefix" in ws:
            print(f"  Repo branch prefix: {ws['repo_branch_prefix']}")
    
    # Guardrails
    if "guardrails" in manifest:
        gr = manifest["guardrails"]
        if "no_touch_paths" in gr:
            patterns = gr["no_touch_paths"]
            print(f"\nGuardrails ({len(patterns)} patterns):")
            for pattern in patterns:
                print(f"  • {pattern}")
    
    # Context
    if "context" in manifest:
        ctx = manifest["context"]
        print(f"\nContext:")
        if "max_files" in ctx:
            print(f"  Max files: {ctx['max_files']}")
        if "summary_tokens" in ctx:
            print(f"  Summary tokens: {ctx['summary_tokens']}")
    
    print()


def cmd_manifest(ecosystem: str | None, args: Any) -> None:
    """Manifest management commands."""
    if not args.manifest_command:
        sys.stderr.write("Usage: amof manifest <validate|show>\n")
        sys.exit(1)
    
    if args.manifest_command == "validate":
        cmd_manifest_validate(ecosystem, args)
    elif args.manifest_command == "show":
        cmd_manifest_show(ecosystem, args)
    else:
        sys.stderr.write(f"Unknown manifest command: {args.manifest_command}\n")
        sys.stderr.write("Available: validate, show\n")
        sys.exit(1)

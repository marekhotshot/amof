"""Manifest (ecosystem.yaml) parsing and management."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional

ECOSYSTEMS_DIR = Path("ecosystems")


def _looks_like_workspace_root(path: Path) -> bool:
    if not (path / "ecosystems").is_dir():
        return False
    return any(
        candidate.exists()
        for candidate in (
            path / ".git",
            path / "pyproject.toml",
            path / "scripts" / "amof.py",
            path / "repos",
        )
    )


def resolve_workspace_root(base: Optional[str] = None) -> Path:
    """Resolve the AMOF workspace root from explicit base, env, or cwd."""
    explicit = base or os.environ.get("AMOF_WORKSPACE_ROOT") or os.environ.get("AMOF_CWD")
    root = Path(explicit or os.getcwd()).resolve()
    if explicit:
        return root
    for candidate in (root, *root.parents):
        if _looks_like_workspace_root(candidate):
            return candidate
    return root


def get_ecosystems_dir(base: Optional[str] = None) -> Path:
    """Return the ecosystems directory under the resolved workspace root."""
    return resolve_workspace_root(base) / "ecosystems"


def get_ecosystem_root(ecosystem: str, base: Optional[str] = None) -> Path:
    """Return the canonical ecosystem directory under the resolved workspace root."""
    return get_ecosystems_dir(base) / ecosystem


def get_journal_dir(ecosystem: str, base: Optional[str] = None) -> Path:
    """Return the canonical journal directory for an ecosystem."""
    return get_ecosystem_root(ecosystem, base) / "journal"


def get_manifest_path(ecosystem: Optional[str] = None) -> Path:
    """Get path to manifest file for given ecosystem."""
    if ecosystem:
        return get_ecosystems_dir() / ecosystem / "ecosystem.yaml"
    # Fallback to root (for backwards compat during transition)
    return Path("ecosystem.yaml")


def parse_manifest_text(text: str) -> Dict[str, Any]:
    """Parse manifest text with yaml when available, else fallback parser."""
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text) or {}
    except Exception:
        parsed = simple_parse_yaml(text)
        return parsed or {}


def read_manifest_file(path: Path) -> Dict[str, Any]:
    """Read and parse a manifest file without exiting on failure."""
    return parse_manifest_text(path.read_text(encoding="utf-8"))


def manifest_is_retired(manifest: Dict[str, Any]) -> bool:
    """Return True when the manifest is marked retired from current truth."""
    return bool(manifest.get("retired"))


def list_available_ecosystems(*, base: Optional[str] = None, include_retired: bool = False) -> List[str]:
    """Return ecosystem names that exist under ecosystems/."""
    ecosystems_dir = get_ecosystems_dir(base)
    ecosystems: List[str] = []
    if not ecosystems_dir.exists():
        pass
    else:
        for eco_dir in ecosystems_dir.iterdir():
            manifest_path = eco_dir / "ecosystem.yaml"
            if not eco_dir.is_dir() or not manifest_path.exists():
                continue
            try:
                manifest = read_manifest_file(manifest_path)
            except Exception:
                # Keep invalid manifests visible instead of silently hiding them.
                ecosystems.append(eco_dir.name)
                continue
            if include_retired or not manifest_is_retired(manifest):
                ecosystems.append(eco_dir.name)
    try:
        from .app_config import get_adopted_ecosystem_manifest, list_adopted_ecosystems

        for name in list_adopted_ecosystems():
            manifest = get_adopted_ecosystem_manifest(name) or {}
            if include_retired or not manifest_is_retired(manifest):
                ecosystems.append(name)
    except Exception:
        pass
    return sorted(set(ecosystems))


def parse_scalar(value: str) -> Any:
    """Parse a YAML scalar value.

    Handles inline comments: ``my-project  # comment`` -> ``my-project``
    Quoted strings are returned as-is (comments inside quotes are preserved).
    """
    # Strip inline comments (but not inside quoted strings)
    if not (value.startswith('"') or value.startswith("'")):
        comment_idx = value.find("  #")
        if comment_idx >= 0:
            value = value[:comment_idx].rstrip()
        # Also handle single-space comment at end: "value #comment"
        elif " #" in value:
            value = value[: value.index(" #")].rstrip()

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
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    return value


def simple_parse_yaml(text: str) -> Any:
    """Parse simplified YAML without external dependencies."""
    lines = [
        line.rstrip("\n")
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

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


class ManifestError(Exception):
    """Error in manifest validation."""
    pass


def validate_manifest(
    manifest: Dict[str, Any],
    detailed: bool = True,
    strict: bool = False,
) -> List[str]:
    """Validate manifest schema and return list of errors.
    
    Args:
        manifest: Manifest dictionary to validate
        detailed: If True, include suggestions and fixes in error messages
        strict: If True, treat warnings as errors
    
    Returns empty list if valid, otherwise list of error messages.
    """
    errors: List[str] = []
    warnings: List[str] = []
    
    # Check required top-level keys
    if "repos" not in manifest:
        errors.append("✗ Missing required key: 'repos'")
        errors.append("  → Add a 'repos' section with at least one repository")
    elif not isinstance(manifest.get("repos"), list):
        errors.append("✗ 'repos' must be a list, got: " + type(manifest.get("repos")).__name__)
    else:
        # Validate each repo
        seen_paths = set()
        for i, repo in enumerate(manifest["repos"]):
            prefix = f"repos[{i}]"
            if not isinstance(repo, dict):
                errors.append(f"✗ {prefix}: must be a mapping, got: {type(repo).__name__}")
                continue
            
            repo_name = repo.get("name", f"<unnamed-{i}>")
            
            # Required fields
            if "name" not in repo:
                errors.append(f"✗ {prefix}: missing required field 'name'")
                errors.append(f"  → Add: name: \"my-repo\"")
            elif not isinstance(repo["name"], str):
                errors.append(f"✗ {prefix}.name: must be a string, got: {type(repo['name']).__name__}")
            
            if "url" not in repo:
                errors.append(f"✗ {prefix} ({repo_name}): missing required field 'url'")
                errors.append(f"  → Add: url: \"git@github.com:org/repo.git\"")
            elif not isinstance(repo["url"], str):
                errors.append(f"✗ {prefix}.url: must be a string, got: {type(repo['url']).__name__}")
            elif not repo["url"].startswith(("git@", "https://", "http://", "ssh://", "file://", "local")):
                warnings.append(
                    f"⚠ {prefix}.url: unusual format (should start with git@, https://, http://, ssh://, file://, or local)"
                )
            
            # Optional fields with type checks
            if "branch" in repo and not isinstance(repo["branch"], str):
                errors.append(f"✗ {prefix}.branch: must be a string, got: {type(repo['branch']).__name__}")
            
            if "path" in repo:
                if not isinstance(repo["path"], str):
                    errors.append(f"✗ {prefix}.path: must be a string, got: {type(repo['path']).__name__}")
                else:
                    if repo["path"] in seen_paths:
                        errors.append(f"✗ {prefix}.path: path '{repo['path']}' is not unique")
                        errors.append(f"  → Use a unique path like '{repo['path']}-{i}'")
                    else:
                        seen_paths.add(repo["path"])
            
            if "readonly" in repo and not isinstance(repo["readonly"], bool):
                errors.append(f"✗ {prefix}.readonly: must be a boolean, got: {type(repo['readonly']).__name__}")
            
            if "include" in repo:
                if not isinstance(repo["include"], list):
                    errors.append(f"✗ {prefix}.include: must be a list, got: {type(repo['include']).__name__}")
                elif not all(isinstance(p, str) for p in repo["include"]):
                    errors.append(f"✗ {prefix}.include: all items must be strings")
            
            if "exclude" in repo:
                if not isinstance(repo["exclude"], list):
                    errors.append(f"✗ {prefix}.exclude: must be a list, got: {type(repo['exclude']).__name__}")
                elif not all(isinstance(p, str) for p in repo["exclude"]):
                    errors.append(f"✗ {prefix}.exclude: all items must be strings")
    
    # Validate workspace section
    if "retired" in manifest and not isinstance(manifest["retired"], bool):
        errors.append("✗ 'retired' must be a boolean")

    if "workspace" in manifest:
        ws = manifest["workspace"]
        if not isinstance(ws, dict):
            errors.append("✗ 'workspace' must be a mapping, got: " + type(ws).__name__)
        else:
            if "branch_prefix" in ws and not isinstance(ws["branch_prefix"], str):
                errors.append("✗ workspace.branch_prefix: must be a string")
            if "repo_branch_prefix" in ws and not isinstance(ws["repo_branch_prefix"], str):
                errors.append("✗ workspace.repo_branch_prefix: must be a string")
    
    # Validate devcontainer section
    if "devcontainer" in manifest:
        dc = manifest["devcontainer"]
        if not isinstance(dc, dict):
            errors.append("✗ 'devcontainer' must be a mapping, got: " + type(dc).__name__)
        elif "enabled" in dc and not isinstance(dc["enabled"], bool):
            errors.append("✗ devcontainer.enabled: must be a boolean")
    
    # Validate context section
    if "context" in manifest:
        ctx = manifest["context"]
        if not isinstance(ctx, dict):
            errors.append("✗ 'context' must be a mapping, got: " + type(ctx).__name__)
        else:
            if "max_files" in ctx and not isinstance(ctx["max_files"], int):
                errors.append("✗ context.max_files: must be an integer, got: " + type(ctx["max_files"]).__name__)
            if "summary_tokens" in ctx and not isinstance(ctx["summary_tokens"], int):
                errors.append("✗ context.summary_tokens: must be an integer, got: " + type(ctx["summary_tokens"]).__name__)
    
    # Validate guardrails section
    if "guardrails" in manifest:
        gr = manifest["guardrails"]
        if not isinstance(gr, dict):
            errors.append("✗ 'guardrails' must be a mapping, got: " + type(gr).__name__)
        else:
            if "no_touch_paths" in gr:
                if not isinstance(gr["no_touch_paths"], list):
                    errors.append("✗ guardrails.no_touch_paths: must be a list, got: " + type(gr["no_touch_paths"]).__name__)
                elif not all(isinstance(p, str) for p in gr["no_touch_paths"]):
                    errors.append("✗ guardrails.no_touch_paths: all items must be strings")
                else:
                    # Validate glob patterns
                    for i, pattern in enumerate(gr["no_touch_paths"]):
                        if pattern.startswith("/"):
                            warnings.append(f"⚠ guardrails.no_touch_paths[{i}]: glob patterns should not start with /")
                            warnings.append(f"  → Change '{pattern}' to '{pattern.lstrip('/')}'")
    
    # Value range checks
    if "context" in manifest and isinstance(manifest["context"], dict):
        ctx = manifest["context"]
        if "max_files" in ctx and isinstance(ctx["max_files"], int):
            if ctx["max_files"] < 1:
                errors.append("✗ context.max_files: must be >= 1")
            elif ctx["max_files"] > 50000:
                warnings.append("⚠ context.max_files: very large value (>50000) may slow indexing")
        if "summary_tokens" in ctx and isinstance(ctx["summary_tokens"], int):
            if ctx["summary_tokens"] < 100:
                errors.append("✗ context.summary_tokens: must be >= 100")
            elif ctx["summary_tokens"] > 200000:
                warnings.append("⚠ context.summary_tokens: very large value (>200000) may be expensive")

    # Warn on repos with no branch specified
    if isinstance(manifest.get("repos"), list):
        for i, repo in enumerate(manifest["repos"]):
            if isinstance(repo, dict) and "branch" not in repo:
                name = repo.get("name", f"<unnamed-{i}>")
                warnings.append(f"⚠ repos[{i}] ({name}): no branch specified (defaults to 'main')")

    # Combine errors and warnings
    result = errors[:]
    if strict:
        result.extend(warnings)
    elif warnings and detailed:
        result.extend(warnings)
    
    return result


def load_manifest(ecosystem: Optional[str] = None, validate: bool = True) -> Dict[str, Any]:
    """Load and parse the manifest file.

    Args:
        ecosystem: Ecosystem name (loads from ecosystems/<name>/ecosystem.yaml)
        validate: Whether to validate the manifest schema

    Returns:
        Parsed manifest dictionary

    Raises:
        SystemExit: If manifest is missing, unparseable, or invalid

    When AMOF_CWD is set (by the amof shell alias), manifest is resolved relative
    to that directory so worktrees use their own ecosystem.yaml.
    """
    import os
    base = os.environ.get("AMOF_CWD")
    if base and ecosystem:
        path = (Path(base) / "ecosystems" / ecosystem / "ecosystem.yaml").resolve()
    else:
        path = get_manifest_path(ecosystem)
    
    if not path.exists():
        manifest = None
        if ecosystem:
            try:
                from .app_config import get_adopted_ecosystem_manifest

                manifest = get_adopted_ecosystem_manifest(ecosystem)
            except Exception:
                manifest = None
        if manifest is not None:
            if validate:
                errors = validate_manifest(manifest)
                if errors:
                    sys.stderr.write(f"\n✗ App-data manifest validation failed for ecosystem '{ecosystem}'\n\n")
                    for error in errors:
                        sys.stderr.write(f"  {error}\n")
                    sys.stderr.write(f"\n✓ Re-run: amof init --adopt . --name {ecosystem}\n")
                    sys.exit(1)
            return manifest
        if ecosystem:
            sys.stderr.write(f"\n✗ Ecosystem '{ecosystem}' not found at {path}\n\n")
            available = []
            ecosystems_dir = Path(base) / "ecosystems" if base else ECOSYSTEMS_DIR
            if ecosystems_dir.exists():
                available = [
                    eco.name for eco in ecosystems_dir.iterdir()
                    if eco.is_dir() and (eco / "ecosystem.yaml").exists()
                ]
            try:
                from .app_config import list_adopted_ecosystems

                available.extend(list_adopted_ecosystems())
            except Exception:
                pass
            if available:
                sys.stderr.write("Available ecosystems:\n")
                for name in sorted(set(available)):
                    sys.stderr.write(f"  • {name}\n")
                sys.stderr.write(f"\nUsage: amof -e {sorted(set(available))[0]} <command>\n")
            else:
                sys.stderr.write("No ecosystems found. Create one with:\n")
                sys.stderr.write(f"  amof ecosystem create {ecosystem}\n")
        else:
            sys.stderr.write(f"\n✗ Manifest not found at {path}\n\n")
            sys.stderr.write("Quick fix:\n")
            sys.stderr.write("  1. Specify ecosystem: amof -e <ecosystem> <command>\n")
            sys.stderr.write("  2. List ecosystems:   amof ecosystem list\n")
            sys.stderr.write("  3. Create one:        amof ecosystem create my-project\n")
        sys.exit(1)

    data = path.read_text(encoding="utf-8")
    try:
        manifest = parse_manifest_text(data)
    except Exception as exc:
        sys.stderr.write(f"\n✗ Failed to parse manifest: {path}\n\n")
        sys.stderr.write(f"Error: {exc}\n\n")
        sys.stderr.write("Common causes:\n")
        sys.stderr.write("  • Incorrect indentation (YAML uses spaces, not tabs)\n")
        sys.stderr.write("  • Missing colon after key name\n")
        sys.stderr.write("  • Unquoted special characters (: # { } [ ])\n")
        sys.stderr.write(f"\nValidate your YAML: amof -e {ecosystem or 'my-project'} manifest validate\n")
        sys.exit(1)
    
    if manifest is None:
        sys.stderr.write(f"\n✗ Manifest is empty: {path}\n\n")
        sys.stderr.write("The file exists but contains no data.\n")
        sys.stderr.write("Add at least a 'repos' section:\n\n")
        sys.stderr.write("  repos:\n")
        sys.stderr.write("    - name: my-repo\n")
        sys.stderr.write("      url: git@github.com:org/repo.git\n")
        sys.stderr.write("      branch: main\n")
        sys.exit(1)
    
    # Validate schema
    if validate:
        errors = validate_manifest(manifest)
        if errors:
            sys.stderr.write(f"\n✗ Manifest validation failed: {path}\n\n")
            for error in errors:
                sys.stderr.write(f"  {error}\n")
            sys.stderr.write(f"\n✓ Fix the above issues and try again\n")
            sys.stderr.write(f"✓ Run: amof -e {ecosystem or 'my-project'} manifest validate\n")
            sys.exit(1)
    
    return manifest


def format_scalar(value: Any) -> str:
    """Format a Python value as a YAML scalar."""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if any(ch in text for ch in [":", "#", '"', "'", "{", "}", ",", "[", "]", "\n"]):
        return json.dumps(text)
    return text


def dump_yaml(data: Any, indent: int = 0) -> str:
    """Dump Python data to YAML string."""
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


def write_manifest(manifest: Dict[str, Any], ecosystem: Optional[str] = None) -> None:
    """Write manifest to file.
    
    Args:
        manifest: Manifest dictionary to write
        ecosystem: Ecosystem name (writes to ecosystems/<name>/ecosystem.yaml)
    """
    path = get_manifest_path(ecosystem)
    try:
        import yaml  # type: ignore

        text = yaml.safe_dump(manifest, sort_keys=False)
    except Exception:
        text = dump_yaml(manifest)
    path.write_text(text + "\n", encoding="utf-8")


def find_repo(manifest: Dict[str, Any], name: str) -> Dict[str, Any] | None:
    """Find a repository by name in the manifest."""
    for repo in manifest.get("repos", []):
        if repo.get("name") == name:
            return repo
    return None


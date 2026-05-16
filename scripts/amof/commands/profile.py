"""Repo profiler -- generates .amof/profile.md for agent navigation.

Content-aware tech stack detection that actually reads manifest files
(Chart.yaml, package.json, pom.xml, etc.) instead of generic regex matching.
Replaces the broken context.py approach that produced identical output for all repos.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..manifest import find_repo
from ..state import get_effective_repos


# ---------------------------------------------------------------------------
# Tech Stack Detection -- reads ACTUAL files
# ---------------------------------------------------------------------------

def detect_tech_stack(repo_path: Path) -> List[Dict[str, Any]]:
    """Detect technology stack by reading real manifest/config files.

    Returns a list of detected technologies with details parsed from files.
    """
    techs: List[Dict[str, Any]] = []

    # --- Helm ---
    chart_yaml = repo_path / "Chart.yaml"
    if chart_yaml.exists():
        info = _parse_yaml_simple(chart_yaml)
        deps = info.get("dependencies", [])
        if isinstance(deps, list):
            dep_names = [d.get("name", "?") for d in deps if isinstance(d, dict)]
        else:
            dep_names = []
        techs.append({
            "type": "helm",
            "name": info.get("name", "unknown"),
            "version": info.get("version", "?"),
            "api_version": info.get("apiVersion", "?"),
            "description": info.get("description", ""),
            "dependencies": dep_names,
        })

    # Check for subcharts
    charts_dir = repo_path / "charts"
    if charts_dir.is_dir():
        for subchart in sorted(charts_dir.iterdir()):
            sub_chart_yaml = subchart / "Chart.yaml"
            if sub_chart_yaml.exists():
                info = _parse_yaml_simple(sub_chart_yaml)
                techs.append({
                    "type": "helm-subchart",
                    "name": info.get("name", subchart.name),
                    "version": info.get("version", "?"),
                    "description": info.get("description", ""),
                })

    # --- Kubernetes / Kustomize ---
    if (repo_path / "kustomization.yaml").exists() or (repo_path / "kustomization.yml").exists():
        techs.append({"type": "kustomize"})

    # --- Docker ---
    dockerfile = repo_path / "Dockerfile"
    if dockerfile.exists():
        base_image, ports = _parse_dockerfile(dockerfile)
        techs.append({
            "type": "docker",
            "base_image": base_image,
            "exposed_ports": ports,
        })

    # --- Jenkins ---
    jenkinsfile = repo_path / "Jenkinsfile"
    if jenkinsfile.exists():
        stages = _parse_jenkinsfile(jenkinsfile)
        techs.append({
            "type": "jenkins",
            "stages": stages,
        })

    # --- Node.js ---
    pkg_json = repo_path / "package.json"
    if pkg_json.exists():
        info = _parse_json_safe(pkg_json)
        techs.append({
            "type": "nodejs",
            "name": info.get("name", "?"),
            "version": info.get("version", "?"),
            "scripts": list(info.get("scripts", {}).keys()),
            "dependencies": list(info.get("dependencies", {}).keys())[:20],
            "dev_dependencies": list(info.get("devDependencies", {}).keys())[:10],
        })

    # --- Python ---
    if (repo_path / "pyproject.toml").exists():
        techs.append({"type": "python", "build": "pyproject.toml"})
    elif (repo_path / "setup.py").exists():
        techs.append({"type": "python", "build": "setup.py"})
    elif (repo_path / "requirements.txt").exists():
        deps = _read_requirements(repo_path / "requirements.txt")
        techs.append({"type": "python", "build": "requirements.txt", "dependencies": deps[:20]})

    # --- Java ---
    if (repo_path / "pom.xml").exists():
        techs.append({"type": "java", "build": "maven"})
    elif (repo_path / "build.gradle").exists() or (repo_path / "build.gradle.kts").exists():
        techs.append({"type": "java", "build": "gradle"})

    # --- Go ---
    go_mod = repo_path / "go.mod"
    if go_mod.exists():
        module_name = _parse_go_mod(go_mod)
        techs.append({"type": "go", "module": module_name})

    # --- Terraform ---
    tf_files = list(repo_path.glob("*.tf"))
    if tf_files:
        techs.append({"type": "terraform", "files": len(tf_files)})

    # --- Makefile ---
    if (repo_path / "Makefile").exists():
        targets = _parse_makefile_targets(repo_path / "Makefile")
        techs.append({"type": "make", "targets": targets[:15]})

    # --- Ansible ---
    if (repo_path / "playbook.yml").exists() or (repo_path / "ansible.cfg").exists():
        techs.append({"type": "ansible"})

    # --- Renovate / Dependabot ---
    if (repo_path / "renovate.json").exists():
        techs.append({"type": "renovate"})
    if (repo_path / ".github" / "dependabot.yml").exists():
        techs.append({"type": "dependabot"})

    return techs


# ---------------------------------------------------------------------------
# Structure Analysis -- tech-aware
# ---------------------------------------------------------------------------

def analyze_structure(repo_path: Path, tech_stack: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Analyze repository structure based on detected tech stack.

    Returns a dict with annotated directory tree and file counts.
    """
    structure: Dict[str, Any] = {"tree": [], "stats": {}}

    # Count files by extension
    ext_counts: Dict[str, int] = defaultdict(int)
    total_files = 0
    for f in repo_path.rglob("*"):
        if f.is_file() and not any(p.startswith(".") for p in f.relative_to(repo_path).parts):
            ext = f.suffix.lower() or "(no ext)"
            ext_counts[ext] += 1
            total_files += 1

    structure["stats"]["total_files"] = total_files
    structure["stats"]["by_extension"] = dict(sorted(ext_counts.items(), key=lambda x: -x[1])[:10])

    # Build annotated tree
    tech_types = {t["type"] for t in tech_stack}

    for entry in sorted(repo_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if entry.name.startswith("."):
            continue

        if entry.is_dir():
            annotation = _annotate_dir(entry, tech_types)
            child_count = sum(1 for _ in entry.rglob("*") if _.is_file())
            tree_entry = {
                "name": entry.name + "/",
                "annotation": annotation,
                "files": child_count,
            }

            # Show notable children for important dirs
            children = []
            for child in sorted(entry.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                if child.name.startswith("."):
                    continue
                suffix = "/" if child.is_dir() else ""
                child_annotation = ""
                if child.is_dir():
                    child_annotation = _annotate_dir(child, tech_types)
                elif child.name in ("values.yaml", "Chart.yaml", "Chart.lock"):
                    child_annotation = _describe_file(child)
                children.append(f"{child.name}{suffix}")
                if len(children) >= 8:
                    remaining = sum(1 for _ in entry.iterdir()) - 8
                    if remaining > 0:
                        children.append(f"... +{remaining} more")
                    break

            tree_entry["children"] = children
            structure["tree"].append(tree_entry)
        else:
            structure["tree"].append({
                "name": entry.name,
                "annotation": _describe_file(entry),
            })

    return structure


# ---------------------------------------------------------------------------
# Key Files -- with 1-line descriptions from actual content
# ---------------------------------------------------------------------------

def find_key_files(repo_path: Path, tech_stack: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Identify important files with descriptions extracted from content."""
    key_files: List[Dict[str, str]] = []
    tech_types = {t["type"] for t in tech_stack}

    # Always important
    important = [
        ("README.md", "Project documentation"),
        ("CODEOWNERS", "Code review ownership rules"),
        ("Jenkinsfile", "CI/CD pipeline definition"),
        ("Dockerfile", "Container build definition"),
        ("Makefile", "Build targets"),
        (".gitignore", "Git ignore patterns"),
    ]

    # Tech-specific
    if "helm" in tech_types:
        important.extend([
            ("Chart.yaml", ""),  # will be filled from content
            ("values.yaml", ""),
            ("Chart.lock", "Locked dependency versions"),
        ])

    if "nodejs" in tech_types:
        important.extend([
            ("package.json", ""),
            ("tsconfig.json", "TypeScript configuration"),
            ("package-lock.json", "Locked dependency versions"),
        ])

    if "python" in tech_types:
        important.extend([
            ("pyproject.toml", "Python project configuration"),
            ("requirements.txt", "Python dependencies"),
            ("setup.py", "Python package setup"),
        ])

    for filename, default_desc in important:
        fpath = repo_path / filename
        if fpath.exists():
            desc = default_desc or _describe_file(fpath)
            key_files.append({"path": filename, "description": desc})

    return key_files


# ---------------------------------------------------------------------------
# Cross-Repo Dependencies
# ---------------------------------------------------------------------------

def detect_cross_repo_deps(
    repo_path: Path,
    repo_name: str,
    manifest: Dict[str, Any],
) -> Dict[str, List[str]]:
    """Detect references between repos in the ecosystem.

    Reads dependency files to find sibling repo names.
    """
    sibling_names = set()
    for r in manifest.get("repos", []):
        if r.get("name") != repo_name and r.get("enabled", True):
            sibling_names.add(r["name"])

    if not sibling_names:
        return {"references": [], "referenced_by": []}

    # Search for sibling repo names in key files
    references: set = set()
    search_files = list(repo_path.glob("*.yaml")) + list(repo_path.glob("*.yml"))
    search_files += list(repo_path.glob("*.json"))
    search_files += list(repo_path.glob("**/*.yaml"))

    for fpath in search_files[:50]:  # cap to avoid huge repos
        if not fpath.is_file() or fpath.stat().st_size > 100_000:
            continue
        try:
            content = fpath.read_text(encoding="utf-8", errors="ignore")
            for name in sibling_names:
                if name in content:
                    references.add(name)
        except Exception:
            continue

    return {
        "references": sorted(references),
        "referenced_by": [],  # filled by caller across all repos
    }


# ---------------------------------------------------------------------------
# Guardrails from manifest
# ---------------------------------------------------------------------------

def get_guardrails(repo_name: str, manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Extract guardrails for this repo from the ecosystem manifest."""
    guardrails: Dict[str, Any] = {}

    # Readonly status
    for r in manifest.get("repos", []):
        if r.get("name") == repo_name:
            guardrails["readonly"] = r.get("readonly", False)
            break

    # No-touch paths
    no_touch = manifest.get("guardrails", {}).get("no_touch_paths", [])
    guardrails["no_touch_paths"] = no_touch

    return guardrails


# ---------------------------------------------------------------------------
# Recent Git Activity
# ---------------------------------------------------------------------------

def get_recent_activity(repo_path: Path) -> Dict[str, Any]:
    """Get recent git activity for the repo."""
    activity: Dict[str, Any] = {}

    def _git(cmd: List[str]) -> Optional[str]:
        try:
            result = subprocess.run(
                ["git", "-C", str(repo_path)] + cmd,
                capture_output=True, text=True, timeout=10,
            )
            return result.stdout.strip() if result.returncode == 0 else None
        except Exception:
            return None

    # Current branch
    activity["branch"] = _git(["rev-parse", "--abbrev-ref", "HEAD"])

    # Last commit
    log_line = _git(["log", "-1", "--format=%h %s (%ar)"])
    activity["last_commit"] = log_line

    # Uncommitted changes count
    status = _git(["status", "--porcelain"])
    if status:
        activity["uncommitted_files"] = len(status.splitlines())
    else:
        activity["uncommitted_files"] = 0

    # Recent commits (last 5)
    recent = _git(["log", "--oneline", "-5"])
    activity["recent_commits"] = recent.splitlines() if recent else []

    return activity


# ---------------------------------------------------------------------------
# Render Profile Markdown
# ---------------------------------------------------------------------------

def render_profile(
    repo_name: str,
    tech_stack: List[Dict[str, Any]],
    structure: Dict[str, Any],
    key_files: List[Dict[str, str]],
    cross_deps: Dict[str, List[str]],
    guardrails: Dict[str, Any],
    activity: Dict[str, Any],
) -> str:
    """Render the .amof/profile.md content.

    Only includes sections that have actual content -- no empty boilerplate.
    """
    lines: List[str] = []

    # Header with description from tech stack
    desc = ""
    for t in tech_stack:
        if t.get("description"):
            desc = t["description"]
            break
    if desc:
        lines.append(f"# {repo_name}\n\n> {desc}\n")
    else:
        lines.append(f"# {repo_name}\n")

    # Tech Stack
    if tech_stack:
        lines.append("## Tech Stack\n")
        for t in tech_stack:
            ttype = t["type"]
            if ttype == "helm":
                lines.append(f"- Helm chart: **{t.get('name', '?')}** v{t.get('version', '?')} (apiVersion: {t.get('api_version', '?')})")
                if t.get("dependencies"):
                    lines.append(f"  - Dependencies: {', '.join(t['dependencies'])}")
            elif ttype == "helm-subchart":
                lines.append(f"- Subchart: **{t.get('name', '?')}** v{t.get('version', '?')}")
                if t.get("description"):
                    lines.append(f"  - {t['description']}")
            elif ttype == "docker":
                lines.append(f"- Docker (base: `{t.get('base_image', '?')}`)")
                if t.get("exposed_ports"):
                    lines.append(f"  - Ports: {', '.join(str(p) for p in t['exposed_ports'])}")
            elif ttype == "jenkins":
                lines.append(f"- Jenkins CI")
                if t.get("stages"):
                    lines.append(f"  - Stages: {', '.join(t['stages'])}")
            elif ttype == "nodejs":
                lines.append(f"- Node.js: **{t.get('name', '?')}** v{t.get('version', '?')}")
                if t.get("scripts"):
                    lines.append(f"  - Scripts: {', '.join(t['scripts'][:8])}")
                if t.get("dependencies"):
                    lines.append(f"  - Deps: {', '.join(t['dependencies'][:10])}")
            elif ttype == "python":
                lines.append(f"- Python ({t.get('build', '?')})")
                if t.get("dependencies"):
                    lines.append(f"  - Deps: {', '.join(t['dependencies'][:10])}")
            elif ttype == "java":
                lines.append(f"- Java ({t.get('build', '?')})")
            elif ttype == "go":
                lines.append(f"- Go (module: `{t.get('module', '?')}`)")
            elif ttype == "terraform":
                lines.append(f"- Terraform ({t.get('files', 0)} .tf files)")
            elif ttype == "make":
                lines.append(f"- Makefile")
                if t.get("targets"):
                    lines.append(f"  - Targets: {', '.join(t['targets'][:8])}")
            elif ttype == "kustomize":
                lines.append(f"- Kustomize")
            elif ttype == "renovate":
                lines.append(f"- Renovate (automated dependency updates)")
            elif ttype == "dependabot":
                lines.append(f"- Dependabot (automated dependency updates)")
            elif ttype == "ansible":
                lines.append(f"- Ansible")
            else:
                lines.append(f"- {ttype}")
        lines.append("")

    # Structure
    if structure.get("tree"):
        lines.append("## Structure\n")
        lines.append("```")
        for entry in structure["tree"]:
            name = entry["name"]
            ann = entry.get("annotation", "")
            if entry.get("children") is not None:
                # Directory
                count = entry.get("files", 0)
                ann_str = f"  # {ann}" if ann else ""
                if count:
                    ann_str = f"  # {ann} ({count} files)" if ann else f"  # {count} files"
                lines.append(f"{name}{ann_str}")
                for child in entry.get("children", []):
                    lines.append(f"  {child}")
            else:
                # File
                ann_str = f"  # {ann}" if ann else ""
                lines.append(f"{name}{ann_str}")
        lines.append("```\n")

        # Stats
        stats = structure.get("stats", {})
        if stats.get("total_files"):
            lines.append(f"**{stats['total_files']} files total**")
            if stats.get("by_extension"):
                ext_str = ", ".join(f"{ext}: {c}" for ext, c in list(stats["by_extension"].items())[:6])
                lines.append(f"({ext_str})\n")

    # Key Files
    if key_files:
        lines.append("## Key Files\n")
        for kf in key_files:
            lines.append(f"- **{kf['path']}** -- {kf['description']}")
        lines.append("")

    # Cross-Repo Dependencies
    refs = cross_deps.get("references", [])
    ref_by = cross_deps.get("referenced_by", [])
    if refs or ref_by:
        lines.append("## Cross-Repo Dependencies\n")
        if refs:
            lines.append(f"- References: {', '.join(refs)}")
        if ref_by:
            lines.append(f"- Referenced by: {', '.join(ref_by)}")
        lines.append("")

    # Guardrails
    if guardrails:
        lines.append("## Guardrails\n")
        lines.append(f"- Readonly: {'yes' if guardrails.get('readonly') else 'no'}")
        no_touch = guardrails.get("no_touch_paths", [])
        if no_touch:
            lines.append(f"- No-touch paths: {', '.join(f'`{p}`' for p in no_touch)}")
        lines.append("")

    # Recent Activity
    if activity.get("branch") or activity.get("last_commit"):
        lines.append("## Recent Activity\n")
        if activity.get("last_commit"):
            lines.append(f"- Last commit: {activity['last_commit']}")
        if activity.get("branch"):
            lines.append(f"- Branch: `{activity['branch']}`")
        if activity.get("uncommitted_files"):
            lines.append(f"- Uncommitted changes: {activity['uncommitted_files']} files")
        if activity.get("recent_commits"):
            lines.append("\nRecent commits:")
            for c in activity["recent_commits"]:
                lines.append(f"  - {c}")
        lines.append("")

    # Footer
    lines.append(f"---\n*Generated by amof profile on {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Profile a single repo
# ---------------------------------------------------------------------------

def profile_repo(
    repo_path: Path,
    repo_name: str,
    manifest: Dict[str, Any],
) -> str:
    """Generate a complete profile for a single repository.

    Returns the profile markdown content and writes .amof/profile.md.
    """
    tech_stack = detect_tech_stack(repo_path)
    structure = analyze_structure(repo_path, tech_stack)
    key_files = find_key_files(repo_path, tech_stack)
    cross_deps = detect_cross_repo_deps(repo_path, repo_name, manifest)
    guardrails_info = get_guardrails(repo_name, manifest)
    activity = get_recent_activity(repo_path)

    content = render_profile(
        repo_name=repo_name,
        tech_stack=tech_stack,
        structure=structure,
        key_files=key_files,
        cross_deps=cross_deps,
        guardrails=guardrails_info,
        activity=activity,
    )

    # Write to .amof/profile.md inside the repo
    profile_path = repo_path / ".amof" / "profile.md"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(content, encoding="utf-8")

    return content


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def cmd_profile(
    manifest: Dict[str, Any],
    repo_name: Optional[str] = None,
    all_repos: bool = False,
) -> int:
    """Generate repo profiles.

    If repo_name given, profile that repo.
    If --all, profile all repos.
    If neither, profile all repos.
    """
    repos = get_effective_repos(manifest)

    if repo_name:
        # Single repo
        repo_config = find_repo(manifest, repo_name)
        if not repo_config:
            sys.stderr.write(f"[profile] Repo not found: {repo_name}\n")
            return 1
        repo_path = Path(repo_config["path"])
        if not repo_path.is_dir():
            sys.stderr.write(f"[profile] Repo path does not exist: {repo_path}\n")
            return 1
        print(f"[profile] Profiling {repo_name}...")
        content = profile_repo(repo_path, repo_name, manifest)
        print(f"[profile] Wrote {repo_path / '.amof' / 'profile.md'} ({len(content)} bytes)")
        return 0

    # All repos
    profiled = 0
    for repo_config in repos:
        name = repo_config["name"]
        repo_path = Path(repo_config["path"])
        if not repo_path.is_dir():
            sys.stderr.write(f"[profile] Skipping {name} (path not found: {repo_path})\n")
            continue
        print(f"[profile] Profiling {name}...")
        content = profile_repo(repo_path, name, manifest)
        print(f"[profile] Wrote {repo_path / '.amof' / 'profile.md'} ({len(content)} bytes)")
        profiled += 1

    # Cross-reference: detect "referenced_by" across all repos
    _cross_reference_repos(repos, manifest)

    print(f"[profile] Profiled {profiled} repos")
    return 0


def _cross_reference_repos(repos: List[Dict[str, Any]], manifest: Dict[str, Any]) -> None:
    """Update 'referenced_by' in each repo's profile by checking all other repos' references."""
    # Build reference map
    references_map: Dict[str, List[str]] = {}  # repo -> list of repos it references
    for repo_config in repos:
        name = repo_config["name"]
        repo_path = Path(repo_config["path"])
        if not repo_path.is_dir():
            continue
        deps = detect_cross_repo_deps(repo_path, name, manifest)
        references_map[name] = deps.get("references", [])

    # Build reverse map
    referenced_by: Dict[str, List[str]] = defaultdict(list)
    for repo_name, refs in references_map.items():
        for ref in refs:
            referenced_by[ref].append(repo_name)

    # Update profiles with referenced_by info
    for repo_config in repos:
        name = repo_config["name"]
        repo_path = Path(repo_config["path"])
        profile_path = repo_path / ".amof" / "profile.md"
        if not profile_path.exists():
            continue
        ref_by = referenced_by.get(name, [])
        if ref_by:
            content = profile_path.read_text(encoding="utf-8")
            # Insert or update referenced_by line
            if "- Referenced by:" in content:
                content = re.sub(
                    r"- Referenced by:.*",
                    f"- Referenced by: {', '.join(ref_by)}",
                    content,
                )
            elif "## Cross-Repo Dependencies" in content:
                content = content.replace(
                    "## Cross-Repo Dependencies\n",
                    f"## Cross-Repo Dependencies\n\n- Referenced by: {', '.join(ref_by)}\n",
                )
            else:
                # Add section before guardrails or at end
                insert_point = content.find("## Guardrails")
                if insert_point == -1:
                    insert_point = content.find("## Recent Activity")
                if insert_point == -1:
                    insert_point = content.find("---\n*Generated")
                if insert_point > 0:
                    content = (
                        content[:insert_point]
                        + f"## Cross-Repo Dependencies\n\n- Referenced by: {', '.join(ref_by)}\n\n"
                        + content[insert_point:]
                    )
            profile_path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Helper parsers -- simple, no external deps
# ---------------------------------------------------------------------------

def _parse_yaml_simple(path: Path) -> Dict[str, Any]:
    """Parse YAML file using PyYAML if available, else simple line parsing."""
    content = path.read_text(encoding="utf-8", errors="ignore")
    try:
        import yaml
        return yaml.safe_load(content) or {}
    except ImportError:
        pass

    # Fallback: simple key-value extraction
    result: Dict[str, Any] = {}
    for line in content.splitlines():
        line = line.strip()
        if ":" in line and not line.startswith("#") and not line.startswith("-"):
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if value:
                result[key] = value
    return result


def _parse_json_safe(path: Path) -> Dict[str, Any]:
    """Parse JSON file safely."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _parse_dockerfile(path: Path) -> Tuple[str, List[int]]:
    """Extract base image and exposed ports from Dockerfile."""
    base_image = "unknown"
    ports: List[int] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.upper().startswith("FROM "):
                parts = line.split()
                if len(parts) >= 2:
                    base_image = parts[1]
            elif line.upper().startswith("EXPOSE "):
                for token in line.split()[1:]:
                    try:
                        ports.append(int(token.split("/")[0]))
                    except ValueError:
                        pass
    except Exception:
        pass
    return base_image, ports


def _parse_jenkinsfile(path: Path) -> List[str]:
    """Extract stage names from Jenkinsfile."""
    stages: List[str] = []
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
        for match in re.finditer(r"stage\s*\(\s*['\"]([^'\"]+)['\"]", content):
            stages.append(match.group(1))
    except Exception:
        pass
    return stages


def _read_requirements(path: Path) -> List[str]:
    """Read package names from requirements.txt."""
    deps: List[str] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("-"):
                name = re.split(r"[>=<!\[]", line)[0].strip()
                if name:
                    deps.append(name)
    except Exception:
        pass
    return deps


def _parse_go_mod(path: Path) -> str:
    """Extract module name from go.mod."""
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("module "):
                return line.split(None, 1)[1].strip()
    except Exception:
        pass
    return "unknown"


def _parse_makefile_targets(path: Path) -> List[str]:
    """Extract target names from Makefile."""
    targets: List[str] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            match = re.match(r"^([a-zA-Z_][\w-]*)\s*:", line)
            if match:
                targets.append(match.group(1))
    except Exception:
        pass
    return targets


def _annotate_dir(dir_path: Path, tech_types: set) -> str:
    """Generate a brief annotation for a directory based on tech stack."""
    name = dir_path.name.lower()

    # Check for specific indicators inside
    if (dir_path / "Chart.yaml").exists():
        info = _parse_yaml_simple(dir_path / "Chart.yaml")
        return info.get("description", "Helm chart")

    if (dir_path / "values.yaml").exists() and "helm" in tech_types:
        return "Helm values"

    annotations = {
        "templates": "Helm/K8s templates" if "helm" in tech_types else "templates",
        "charts": "Helm subcharts",
        "src": "source code",
        "test": "tests",
        "tests": "tests",
        "docs": "documentation",
        "scripts": "scripts",
        "config": "configuration",
        "deploy": "deployment",
        "k8s": "Kubernetes manifests",
        "terraform": "Terraform configs",
        "ansible": "Ansible playbooks",
        "rancher-values": "Rancher value overrides",
    }
    return annotations.get(name, "")


def _describe_file(file_path: Path) -> str:
    """Generate a 1-line description for a file based on its content."""
    name = file_path.name

    # Known files
    known = {
        "Chart.yaml": None,  # parse dynamically
        "Chart.lock": "locked chart dependency versions",
        "values.yaml": None,  # parse dynamically
        "Jenkinsfile": "CI/CD pipeline",
        "Dockerfile": None,  # parse dynamically
        "Makefile": "build targets",
        "README.md": "project documentation",
        "CODEOWNERS": "code review ownership",
        "renovate.json": "automated dependency update config",
        ".helmignore": "Helm packaging ignore patterns",
        ".gitignore": "Git ignore patterns",
        "PIPELINE.md": "pipeline documentation",
    }

    if name in known:
        if known[name] is not None:
            return known[name]

        # Dynamic descriptions
        try:
            if name == "Chart.yaml":
                info = _parse_yaml_simple(file_path)
                n = info.get("name", "?")
                v = info.get("version", "?")
                return f"chart {n} v{v}"
            elif name == "values.yaml":
                content = file_path.read_text(encoding="utf-8", errors="ignore")
                top_keys = [
                    line.split(":")[0].strip()
                    for line in content.splitlines()
                    if ":" in line and not line.startswith(" ") and not line.startswith("#")
                ][:8]
                if top_keys:
                    return f"configurable params: {', '.join(top_keys)}"
                return "Helm values"
            elif name == "Dockerfile":
                base, ports = _parse_dockerfile(file_path)
                desc = f"base: {base}"
                if ports:
                    desc += f", ports: {', '.join(str(p) for p in ports)}"
                return desc
        except Exception:
            pass

    # Extension-based fallback
    ext_desc = {
        ".yaml": "YAML config",
        ".yml": "YAML config",
        ".json": "JSON config",
        ".toml": "TOML config",
        ".md": "documentation",
        ".sh": "shell script",
        ".bat": "Windows script",
        ".py": "Python script",
        ".js": "JavaScript",
        ".ts": "TypeScript",
        ".go": "Go source",
        ".java": "Java source",
        ".tf": "Terraform config",
        ".tgz": "compressed archive",
        ".sql": "SQL script",
    }
    return ext_desc.get(file_path.suffix.lower(), "")

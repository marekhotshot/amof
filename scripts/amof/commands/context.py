"""Context command - generate AI context for services."""

from __future__ import annotations

import fnmatch
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

from ..manifest import find_repo
from ..state import get_effective_repos
from ..utils import matches_any, prepare_patterns, run_command


def gather_files(
    repo_path: Path,
    include: List[str],
    exclude: List[str],
    max_files: int,
    max_bytes: int,
) -> List[Dict[str, Any]]:
    """Gather files matching include/exclude patterns."""
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
        files.append({
            "path": rel_path,
            "size": size,
            "type": file_path.suffix.lstrip(".") or "unknown",
        })
        total_bytes += size
    return files


def find_todos(repo_path: Path, indexed_files: List[Dict[str, Any]]) -> List[str]:
    """Find TODO/FIXME markers in code."""
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
    """Generate basic summary of repository."""
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


def extract_api_surface(repo_path: Path, indexed_files: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract API endpoints, events, and service contracts."""
    api_data = {
        "rest_endpoints": [],
        "grpc_services": [],
        "events_published": [],
        "events_consumed": [],
        "graphql_types": [],
    }
    
    patterns = {
        "flask_route": re.compile(r'@(?:app|router|bp)\.(?:route|get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)["\']'),
        "fastapi_route": re.compile(r'@(?:app|router)\.(?:get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)["\']'),
        "spring_mapping": re.compile(r'@(?:Get|Post|Put|Delete|Patch|Request)Mapping\s*\(\s*(?:value\s*=\s*)?["\']([^"\']+)["\']'),
        "express_route": re.compile(r'(?:app|router)\.(?:get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)["\']'),
        "event_publish": re.compile(r'(?:publish|emit|send|dispatch)\s*\(\s*["\']([a-zA-Z0-9_\.\-]+)["\']'),
        "event_subscribe": re.compile(r'(?:subscribe|on|listen|consume)\s*\(\s*["\']([a-zA-Z0-9_\.\-]+)["\']'),
        "grpc_service": re.compile(r'service\s+(\w+)\s*\{'),
    }
    
    for entry in indexed_files:
        rel_path = entry["path"]
        file_path = repo_path / rel_path
        ext = file_path.suffix.lower()
        
        if ext not in (".py", ".js", ".ts", ".java", ".go", ".proto", ".graphql"):
            continue
            
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
            lines = content.split("\n")
            
            for line_num, line in enumerate(lines, 1):
                for pattern_name in ["flask_route", "fastapi_route", "spring_mapping", "express_route"]:
                    for match in patterns[pattern_name].finditer(line):
                        method = "GET"
                        if "post" in line.lower():
                            method = "POST"
                        elif "put" in line.lower():
                            method = "PUT"
                        elif "delete" in line.lower():
                            method = "DELETE"
                        elif "patch" in line.lower():
                            method = "PATCH"
                        api_data["rest_endpoints"].append({
                            "method": method,
                            "path": match.group(1),
                            "file": f"{rel_path}:{line_num}",
                        })
                
                for match in patterns["event_publish"].finditer(line):
                    event_name = match.group(1)
                    if event_name not in [e["name"] for e in api_data["events_published"]]:
                        api_data["events_published"].append({
                            "name": event_name,
                            "file": f"{rel_path}:{line_num}",
                        })
                
                for match in patterns["event_subscribe"].finditer(line):
                    event_name = match.group(1)
                    if event_name not in [e["name"] for e in api_data["events_consumed"]]:
                        api_data["events_consumed"].append({
                            "name": event_name,
                            "file": f"{rel_path}:{line_num}",
                        })
                
                if ext == ".proto":
                    for match in patterns["grpc_service"].finditer(line):
                        api_data["grpc_services"].append({
                            "name": match.group(1),
                            "file": rel_path,
                        })
        except Exception:
            continue
    
    return api_data


def extract_config_map(repo_path: Path, indexed_files: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract configuration and environment variable references."""
    config_data = {
        "env_vars": [],
        "config_files": [],
        "secrets_references": [],
        "feature_flags": [],
    }
    
    env_var_patterns = [
        re.compile(r'os\.(?:environ|getenv)\s*\[\s*["\'](\w+)["\']'),
        re.compile(r'os\.getenv\s*\(\s*["\'](\w+)["\']'),
        re.compile(r'process\.env\.(\w+)'),
        re.compile(r'\$\{(\w+)\}'),
        re.compile(r'\$(\w+)'),
        re.compile(r'env\s*\(\s*["\'](\w+)["\']'),
    ]
    
    secret_patterns = [
        re.compile(r'(?:secret|password|token|key|credential)["\']?\s*[:=]\s*["\']?([^"\'}\s]+)', re.I),
        re.compile(r'aws[_-]?secret[_-]?(?:name|arn)?["\']?\s*[:=]\s*["\']([^"\']+)["\']', re.I),
        re.compile(r'vault[_-]?(?:path|secret)["\']?\s*[:=]\s*["\']([^"\']+)["\']', re.I),
    ]
    
    feature_flag_patterns = [
        re.compile(r'(?:feature[_-]?flag|ff|toggle)[_\.]?([A-Z_]+)', re.I),
        re.compile(r'isFeatureEnabled\s*\(\s*["\']([^"\']+)["\']'),
    ]
    
    config_file_patterns = [
        "*.yaml", "*.yml", "*.json", "*.toml", "*.ini", "*.conf", "*.properties",
        ".env*", "config.*", "settings.*",
    ]
    
    env_vars_seen = set()
    secrets_seen = set()
    flags_seen = set()
    
    for entry in indexed_files:
        rel_path = entry["path"]
        file_path = repo_path / rel_path
        
        for pattern in config_file_patterns:
            if fnmatch.fnmatch(file_path.name.lower(), pattern.lower()):
                if rel_path not in config_data["config_files"]:
                    config_data["config_files"].append(rel_path)
                break
        
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
            
            for pattern in env_var_patterns:
                for match in pattern.finditer(content):
                    var_name = match.group(1)
                    if var_name not in env_vars_seen and len(var_name) > 2:
                        env_vars_seen.add(var_name)
                        config_data["env_vars"].append(var_name)
            
            for pattern in secret_patterns:
                for match in pattern.finditer(content):
                    secret_ref = match.group(1)
                    if secret_ref not in secrets_seen and not secret_ref.startswith("$"):
                        secrets_seen.add(secret_ref)
                        config_data["secrets_references"].append({
                            "reference": secret_ref,
                            "file": rel_path,
                        })
            
            for pattern in feature_flag_patterns:
                for match in pattern.finditer(content):
                    flag_name = match.group(1)
                    if flag_name not in flags_seen:
                        flags_seen.add(flag_name)
                        config_data["feature_flags"].append(flag_name)
        except Exception:
            continue
    
    config_data["env_vars"] = sorted(config_data["env_vars"])
    config_data["config_files"] = sorted(config_data["config_files"])
    config_data["feature_flags"] = sorted(config_data["feature_flags"])
    
    return config_data


def extract_code_structure(repo_path: Path, indexed_files: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract code structure: modules, classes, functions, imports."""
    structure = {
        "modules": [],
        "entry_points": [],
        "dependencies": {"internal": [], "external": []},
        "classes": [],
        "functions": [],
    }
    
    class_patterns = {
        ".py": re.compile(r'^class\s+(\w+)(?:\(([^)]*)\))?:', re.MULTILINE),
        ".java": re.compile(r'(?:public|private|protected)?\s*class\s+(\w+)(?:\s+extends\s+(\w+))?'),
        ".ts": re.compile(r'(?:export\s+)?class\s+(\w+)(?:\s+extends\s+(\w+))?'),
        ".js": re.compile(r'class\s+(\w+)(?:\s+extends\s+(\w+))?'),
        ".go": re.compile(r'type\s+(\w+)\s+struct\s*\{'),
    }
    
    function_patterns = {
        ".py": re.compile(r'^def\s+(\w+)\s*\(([^)]*)\)', re.MULTILINE),
        ".java": re.compile(r'(?:public|private|protected)\s+\w+\s+(\w+)\s*\(([^)]*)\)'),
        ".ts": re.compile(r'(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)'),
        ".js": re.compile(r'(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)'),
        ".go": re.compile(r'func\s+(?:\([^)]+\)\s+)?(\w+)\s*\(([^)]*)\)'),
    }
    
    import_patterns = {
        ".py": [
            re.compile(r'^import\s+([\w\.]+)', re.MULTILINE),
            re.compile(r'^from\s+([\w\.]+)\s+import', re.MULTILINE),
        ],
        ".java": [re.compile(r'^import\s+([\w\.]+);', re.MULTILINE)],
        ".ts": [re.compile(r'import\s+.*?from\s+["\']([^"\']+)["\']')],
        ".js": [re.compile(r'(?:import|require)\s*\(?["\']([^"\']+)["\']')],
        ".go": [re.compile(r'"([^"]+)"')],
    }
    
    entry_point_files = [
        "main.py", "app.py", "__main__.py", "index.js", "index.ts",
        "main.go", "Main.java", "server.py", "server.js", "server.ts",
    ]
    
    internal_imports = set()
    external_imports = set()
    modules_seen = set()
    
    for entry in indexed_files:
        rel_path = entry["path"]
        file_path = repo_path / rel_path
        ext = file_path.suffix.lower()
        
        parts = Path(rel_path).parts
        if len(parts) > 1:
            module_name = parts[0]
            if module_name not in modules_seen and not module_name.startswith("."):
                modules_seen.add(module_name)
                structure["modules"].append({"name": module_name, "path": module_name})
        
        if file_path.name in entry_point_files:
            structure["entry_points"].append(rel_path)
        
        if ext not in class_patterns:
            continue
        
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
            
            if ext in class_patterns:
                for match in class_patterns[ext].finditer(content):
                    class_name = match.group(1)
                    parent = match.group(2) if match.lastindex >= 2 else None
                    methods = []
                    if ext == ".py":
                        class_start = match.start()
                        class_content = content[class_start:class_start + 2000]
                        method_pattern = re.compile(r'^\s{4}def\s+(\w+)', re.MULTILINE)
                        methods = [m.group(1) for m in method_pattern.finditer(class_content)]
                    
                    structure["classes"].append({
                        "name": class_name,
                        "file": rel_path,
                        "extends": parent,
                        "methods": methods[:10],
                    })
            
            if ext in function_patterns:
                for match in function_patterns[ext].finditer(content):
                    func_name = match.group(1)
                    params = match.group(2) if match.lastindex >= 2 else ""
                    if ext == ".py" and func_name.startswith("_"):
                        continue
                    structure["functions"].append({
                        "name": func_name,
                        "file": rel_path,
                        "params": params[:100],
                    })
            
            if ext in import_patterns:
                for pattern in import_patterns[ext]:
                    for match in pattern.finditer(content):
                        imp = match.group(1)
                        if imp.startswith(".") or imp.split(".")[0] in modules_seen:
                            internal_imports.add(imp)
                        else:
                            external_imports.add(imp.split(".")[0])
        except Exception:
            continue
    
    structure["dependencies"]["internal"] = sorted(internal_imports)
    structure["dependencies"]["external"] = sorted(external_imports)[:50]
    structure["classes"] = structure["classes"][:100]
    structure["functions"] = structure["functions"][:200]
    
    return structure


def extract_change_impact(repo_path: Path, indexed_files: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Identify high-risk files and change impact hints."""
    impact = {
        "high_risk_files": [],
        "frequently_changed": [],
        "test_coverage_hints": [],
        "critical_paths": [],
    }
    
    high_risk_patterns = [
        (r"migrations?/", "Database schema changes"),
        (r"helm[_-]?values/", "Production configuration"),
        (r"\.env\.", "Environment configuration"),
        (r"secrets?/", "Secrets configuration"),
        (r"terraform/", "Infrastructure changes"),
        (r"k8s/|kubernetes/", "Kubernetes manifests"),
        (r"Dockerfile", "Container build changes"),
        (r"ci/|\.github/|\.gitlab-ci", "CI/CD pipeline"),
        (r"security/|auth/", "Security-sensitive code"),
    ]
    
    test_patterns = [r"test[s_]?/", r"_test\.", r"\.test\.", r"spec[s]?/", r"\.spec\."]
    critical_patterns = [r"api/|routes?/", r"models?/|schemas?/", r"services?/", r"core/|lib/"]
    
    files_with_tests = set()
    critical_files = []
    
    for entry in indexed_files:
        rel_path = entry["path"]
        
        for pattern, reason in high_risk_patterns:
            if re.search(pattern, rel_path, re.I):
                impact["high_risk_files"].append({"path": rel_path, "reason": reason})
                break
        
        for pattern in test_patterns:
            if re.search(pattern, rel_path, re.I):
                source_name = re.sub(r"[_\.]?test[s]?|[_\.]?spec[s]?", "", Path(rel_path).stem)
                files_with_tests.add(source_name)
                break
        
        for pattern in critical_patterns:
            if re.search(pattern, rel_path, re.I):
                critical_files.append(rel_path)
                break
    
    for entry in indexed_files:
        rel_path = entry["path"]
        file_stem = Path(rel_path).stem
        ext = Path(rel_path).suffix
        
        if ext in [".py", ".js", ".ts", ".java", ".go"]:
            if file_stem not in files_with_tests:
                if any(re.search(p, rel_path, re.I) for p in critical_patterns):
                    impact["test_coverage_hints"].append({
                        "path": rel_path,
                        "note": "No apparent test coverage",
                    })
    
    impact["critical_paths"] = critical_files[:30]
    impact["test_coverage_hints"] = impact["test_coverage_hints"][:20]
    impact["high_risk_files"] = impact["high_risk_files"][:20]
    
    try:
        code, out = run_command(
            ["git", "-C", str(repo_path), "log", "--pretty=format:", "--name-only", "-n", "100"],
        )
        if code == 0 and out.strip():
            file_counts = defaultdict(int)
            for line in out.strip().split("\n"):
                if line.strip():
                    file_counts[line.strip()] += 1
            top_files = sorted(file_counts.items(), key=lambda x: x[1], reverse=True)[:10]
            impact["frequently_changed"] = [{"path": f, "changes": c} for f, c in top_files]
    except Exception:
        pass
    
    return impact


def extract_semantic_chunks(repo_path: Path, indexed_files: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract semantic code chunks with docstrings."""
    chunks = {"classes": [], "functions": [], "types": [], "constants": []}
    
    class_with_doc_py = re.compile(
        r'^class\s+(\w+)(?:\(([^)]*)\))?:\s*\n\s*(?:"""([^"]*(?:""")?))?',
        re.MULTILINE
    )
    
    func_with_doc_py = re.compile(
        r'^def\s+(\w+)\s*\(([^)]*)\)(?:\s*->\s*([^:]+))?:\s*\n\s*(?:"""([^"]*(?:""")?))?',
        re.MULTILINE
    )
    
    type_patterns = {
        ".py": re.compile(r'^(\w+)\s*=\s*(?:TypeVar|NewType|Union|Optional|List|Dict|Tuple)\[', re.MULTILINE),
        ".ts": re.compile(r'^(?:export\s+)?(?:type|interface)\s+(\w+)'),
        ".go": re.compile(r'^type\s+(\w+)\s+(?:interface|struct)'),
    }
    
    const_patterns = {
        ".py": re.compile(r'^([A-Z][A-Z0-9_]+)\s*=\s*(.{1,50})', re.MULTILINE),
        ".ts": re.compile(r'^(?:export\s+)?const\s+([A-Z][A-Z0-9_]+)\s*=\s*(.{1,50})'),
        ".js": re.compile(r'^(?:export\s+)?const\s+([A-Z][A-Z0-9_]+)\s*=\s*(.{1,50})'),
    }
    
    for entry in indexed_files:
        rel_path = entry["path"]
        file_path = repo_path / rel_path
        ext = file_path.suffix.lower()
        
        if ext not in [".py", ".ts", ".js", ".go", ".java"]:
            continue
        
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
            
            if ext == ".py":
                for match in class_with_doc_py.finditer(content):
                    line_num = content[:match.start()].count("\n") + 1
                    docstring = match.group(3) or ""
                    docstring = docstring.replace('"""', "").strip()[:200]
                    chunks["classes"].append({
                        "name": match.group(1),
                        "file": rel_path,
                        "line": line_num,
                        "extends": match.group(2),
                        "docstring": docstring,
                    })
                
                for match in func_with_doc_py.finditer(content):
                    func_name = match.group(1)
                    if func_name.startswith("_"):
                        continue
                    line_num = content[:match.start()].count("\n") + 1
                    docstring = match.group(4) or ""
                    docstring = docstring.replace('"""', "").strip()[:200]
                    chunks["functions"].append({
                        "name": func_name,
                        "file": rel_path,
                        "line": line_num,
                        "params": match.group(2)[:100],
                        "returns": match.group(3),
                        "docstring": docstring,
                    })
            
            if ext in type_patterns:
                for match in type_patterns[ext].finditer(content):
                    line_num = content[:match.start()].count("\n") + 1
                    chunks["types"].append({
                        "name": match.group(1),
                        "file": rel_path,
                        "line": line_num,
                    })
            
            if ext in const_patterns:
                for match in const_patterns[ext].finditer(content):
                    line_num = content[:match.start()].count("\n") + 1
                    chunks["constants"].append({
                        "name": match.group(1),
                        "file": rel_path,
                        "line": line_num,
                        "value": match.group(2).strip()[:50],
                    })
        except Exception:
            continue
    
    chunks["classes"] = chunks["classes"][:100]
    chunks["functions"] = chunks["functions"][:200]
    chunks["types"] = chunks["types"][:50]
    chunks["constants"] = chunks["constants"][:50]
    
    return chunks


def extract_cross_repo_relationships(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Analyze relationships between repos in the workspace."""
    relationships = {"repos": {}, "dependency_graph": [], "shared_patterns": []}
    
    repos = get_effective_repos(manifest)
    repo_names = {r.get("name") for r in repos}
    
    for repo in repos:
        name = repo.get("name")
        repo_path = Path(repo.get("path", f"repos/{name}"))
        
        if not repo_path.exists():
            continue
        
        repo_info = {"depends_on": [], "references": {}, "shared_files": []}
        
        try:
            for file_path in repo_path.rglob("*"):
                if not file_path.is_file():
                    continue
                if file_path.suffix not in [".yaml", ".yml", ".json", ".py", ".ts", ".js", ".tf"]:
                    continue
                
                try:
                    content = file_path.read_text(encoding="utf-8", errors="ignore")
                    rel_path = file_path.relative_to(repo_path).as_posix()
                    
                    for other_name in repo_names:
                        if other_name == name:
                            continue
                        if other_name in content or other_name.replace("-", "_") in content:
                            if other_name not in repo_info["depends_on"]:
                                repo_info["depends_on"].append(other_name)
                            if other_name not in repo_info["references"]:
                                repo_info["references"][other_name] = []
                            if rel_path not in repo_info["references"][other_name]:
                                repo_info["references"][other_name].append(rel_path)
                except Exception:
                    continue
        except Exception:
            pass
        
        relationships["repos"][name] = repo_info
        
        for dep in repo_info["depends_on"]:
            relationships["dependency_graph"].append({"from": name, "to": dep})
    
    return relationships


def generate_context_markdown(
    service: str,
    api_data: Dict[str, Any],
    config_data: Dict[str, Any],
    structure: Dict[str, Any],
    impact: Dict[str, Any],
) -> str:
    """Generate human-readable markdown context summary."""
    lines = [f"# Context: {service}", ""]
    
    lines.append("## API Surface")
    if api_data["rest_endpoints"]:
        lines.append("\n### REST Endpoints")
        for ep in api_data["rest_endpoints"][:20]:
            lines.append(f"- `{ep['method']} {ep['path']}` ({ep['file']})")
    if api_data["events_published"]:
        lines.append("\n### Events Published")
        for ev in api_data["events_published"]:
            lines.append(f"- `{ev['name']}` ({ev['file']})")
    lines.append("")
    
    lines.append("## Configuration")
    if config_data["env_vars"]:
        lines.append("\n### Environment Variables")
        lines.append(", ".join(f"`{v}`" for v in config_data["env_vars"][:30]))
    if config_data["config_files"]:
        lines.append("\n### Config Files")
        for cf in config_data["config_files"][:10]:
            lines.append(f"- {cf}")
    lines.append("")
    
    lines.append("## Code Structure")
    if structure["entry_points"]:
        lines.append("\n### Entry Points")
        for ep in structure["entry_points"]:
            lines.append(f"- {ep}")
    if structure["classes"]:
        lines.append("\n### Key Classes")
        for cls in structure["classes"][:15]:
            extends = f" extends {cls['extends']}" if cls.get("extends") else ""
            lines.append(f"- `{cls['name']}`{extends} ({cls['file']})")
    lines.append("")
    
    lines.append("## Change Impact")
    if impact["high_risk_files"]:
        lines.append("\n### High-Risk Files")
        for hrf in impact["high_risk_files"]:
            lines.append(f"- `{hrf['path']}` - {hrf['reason']}")
    lines.append("")
    
    lines.append("## Dependencies")
    if structure["dependencies"]["external"]:
        lines.append("\n### External")
        lines.append(", ".join(f"`{d}`" for d in structure["dependencies"]["external"][:20]))
    
    return "\n".join(lines)


def _resolve_service_repo(manifest: Dict[str, Any], service: str) -> Dict[str, Any] | None:
    repo = find_repo(manifest, service)
    if repo:
        return repo
    repos = manifest.get("repos", [])
    if len(repos) != 1:
        return None
    aliases = {
        str(manifest.get("ecosystem") or "").strip(),
        str(manifest.get("name") or "").strip(),
    }
    aliases.discard("")
    if service in aliases:
        return repos[0]
    return None


def cmd_context(
    manifest: Dict[str, Any],
    service: str | None = None,
    context_types: str = "all",
    output_format: str = "json",
    incremental: bool = False,
) -> int:
    """Generate context for a service or workspace-wide analysis."""
    
    if context_types == "all":
        types_to_generate = {"api", "config", "structure", "impact", "chunks"}
    else:
        types_to_generate = set(context_types.split(","))
    
    # Workspace-wide mode
    if not service:
        output_dir = Path("context") / "_workspace"
        output_dir.mkdir(parents=True, exist_ok=True)
        
        relationships = extract_cross_repo_relationships(manifest)
        
        if output_format == "json":
            with (output_dir / "relationships.json").open("w", encoding="utf-8") as f:
                json.dump(relationships, f, indent=2)
        else:
            lines = ["# Workspace Relationships", ""]
            for repo_name, info in relationships.get("repos", {}).items():
                lines.append(f"## {repo_name}")
                if info.get("depends_on"):
                    lines.append(f"Depends on: {', '.join(info['depends_on'])}")
                lines.append("")
            (output_dir / "relationships.md").write_text("\n".join(lines), encoding="utf-8")
        
        print(f"Workspace context generated under {output_dir}")
        return 0
    
    # Service-specific mode
    repo = _resolve_service_repo(manifest, service)
    if not repo:
        sys.stderr.write(f"Service '{service}' not found in manifest.\n")
        return 1

    repo_path = Path(repo.get("path", service))
    if not repo_path.exists():
        sys.stderr.write(f"Repository path missing: {repo_path}\n")
        return 1

    ctx_config = manifest.get("context", {})
    max_files = int(ctx_config.get("max_files", 500))
    max_bytes = int(ctx_config.get("summary_tokens", 50000))

    include = prepare_patterns(repo.get("include", ["**"]))
    exclude = prepare_patterns(repo.get("exclude", []))

    indexed_files = gather_files(repo_path, include, exclude, max_files, max_bytes)

    output_dir = Path("context") / service
    output_dir.mkdir(parents=True, exist_ok=True)

    with (output_dir / "index.json").open("w", encoding="utf-8") as f:
        json.dump(indexed_files, f, indent=2)

    summary_content = summarize_repo(repo_path, indexed_files)
    (output_dir / "summary.md").write_text(summary_content, encoding="utf-8")

    api_data = config_data = structure = impact = chunks = None
    
    if "api" in types_to_generate:
        api_data = extract_api_surface(repo_path, indexed_files)
        with (output_dir / "api.json").open("w", encoding="utf-8") as f:
            json.dump(api_data, f, indent=2)
        print(f"  - API: {len(api_data.get('rest_endpoints', []))} endpoints")
    
    if "config" in types_to_generate:
        config_data = extract_config_map(repo_path, indexed_files)
        with (output_dir / "config.json").open("w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=2)
        print(f"  - Config: {len(config_data.get('env_vars', []))} env vars")
    
    if "structure" in types_to_generate:
        structure = extract_code_structure(repo_path, indexed_files)
        with (output_dir / "structure.json").open("w", encoding="utf-8") as f:
            json.dump(structure, f, indent=2)
        print(f"  - Structure: {len(structure.get('classes', []))} classes")
    
    if "impact" in types_to_generate:
        impact = extract_change_impact(repo_path, indexed_files)
        with (output_dir / "impact.json").open("w", encoding="utf-8") as f:
            json.dump(impact, f, indent=2)
        print(f"  - Impact: {len(impact.get('high_risk_files', []))} high-risk")
    
    if "chunks" in types_to_generate:
        chunks = extract_semantic_chunks(repo_path, indexed_files)
        with (output_dir / "chunks.json").open("w", encoding="utf-8") as f:
            json.dump(chunks, f, indent=2)
        print(f"  - Chunks: {len(chunks.get('classes', []))} class chunks")
    
    if output_format == "markdown" and all([api_data, config_data, structure, impact]):
        md_content = generate_context_markdown(service, api_data, config_data, structure, impact)
        (output_dir / "context.md").write_text(md_content, encoding="utf-8")

    print(f"\nContext generated under {output_dir}")
    print(f"Indexed files: {len(indexed_files)}")
    return 0


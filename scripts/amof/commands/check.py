"""Check command - verify environment prerequisites."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..utils import run_command


def check_command_exists(cmd: str) -> Tuple[bool, str]:
    """Check if a command exists and return version if found."""
    path = shutil.which(cmd)
    if not path:
        return False, "not found"
    
    # Try common version flags
    for flag in ["--version", "-v", "version"]:
        code, out = run_command([cmd, flag])
        if code == 0 and out:
            # Extract first line of version output
            version = out.split("\n")[0].strip()[:50]
            return True, version
    
    return True, "installed (version unknown)"


def check_git_config() -> List[str]:
    """Check git configuration."""
    issues = []
    
    # Check git user.name
    code, out = run_command(["git", "config", "user.name"])
    if code != 0 or not out.strip():
        issues.append("Git user.name not configured")
    
    # Check git user.email
    code, out = run_command(["git", "config", "user.email"])
    if code != 0 or not out.strip():
        issues.append("Git user.email not configured")
    
    return issues


def check_ssh_key() -> Tuple[bool, str]:
    """Check if SSH key exists for git operations."""
    ssh_dir = Path.home() / ".ssh"
    if not ssh_dir.exists():
        return False, "~/.ssh directory not found"
    
    key_files = ["id_rsa", "id_ed25519", "id_ecdsa"]
    for key in key_files:
        if (ssh_dir / key).exists():
            return True, f"Found {key}"
    
    return False, "No SSH key found (id_rsa, id_ed25519, id_ecdsa)"


def check_env_file() -> Tuple[bool, str]:
    """Summarize optional local environment overrides for public AMOF."""
    env_file = Path(".env")
    if not env_file.exists():
        return True, "No optional .env overrides configured"

    content = env_file.read_text()
    recommended = ["GIT_TOKEN"]
    present = [v for v in recommended if v in content]

    if present:
        return True, f".env configured with optional variables: {', '.join(present)}"

    return True, ".env present with no recommended optional variables"


def cmd_check(manifest: Dict[str, Any]) -> int:
    """Check environment prerequisites for AMOF."""
    print("AMOF Environment Check")
    print("=" * 50)
    
    required_ok = True
    optional_warning_count = 0
    
    # Required tools
    print("\n📦 Required Tools:")
    required_tools = [
        ("git", "Version control"),
        ("python3", "AMOF CLI"),
    ]
    
    for tool, description in required_tools:
        found, version = check_command_exists(tool)
        status = "✓" if found else "✗"
        color_version = version if found else f"MISSING - {description}"
        print(f"  {status} {tool:<12} {color_version}")
        if not found:
            required_ok = False
    
    # Optional tools
    print("\n📦 Optional Tools:")
    optional_tools = [
        ("docker", "Container operations"),
        ("helm", "Helm chart management"),
        ("aws", "AWS CLI for ECR"),
        ("kubectl", "Kubernetes operations"),
        ("cursor", "IDE integration"),
    ]
    
    for tool, description in optional_tools:
        found, version = check_command_exists(tool)
        status = "✓" if found else "○"
        color_version = version if found else f"not found ({description})"
        print(f"  {status} {tool:<12} {color_version}")
    
    # Git configuration
    print("\n🔧 Git Configuration:")
    git_issues = check_git_config()
    if git_issues:
        for issue in git_issues:
            print(f"  ○ {issue} (optional for public install/basic checks)")
            optional_warning_count += 1
    else:
        print("  ✓ Git user configured")
    
    # SSH key
    print("\n🔑 SSH Key:")
    ssh_ok, ssh_status = check_ssh_key()
    print(f"  {'✓' if ssh_ok else '○'} {ssh_status}")
    if not ssh_ok:
        optional_warning_count += 1
    
    # Environment file
    print("\n📄 Environment:")
    env_ok, env_status = check_env_file()
    print(f"  {'✓' if env_ok else '○'} {env_status}")
    
    # Manifest
    print("\n📋 Manifest:")
    repos = manifest.get("repos", [])
    print(f"  ✓ {len(repos)} repositories configured")
    readonly = sum(1 for r in repos if r.get("readonly"))
    if readonly:
        print(f"  ○ {readonly} readonly, {len(repos) - readonly} read-write")
    
    # Summary
    print("\n" + "=" * 50)
    if not required_ok:
        print("❌ Required prerequisites missing. Fix the issues above.")
        return 1
    if optional_warning_count:
        print("✅ Required prerequisites satisfied with optional warnings.")
        return 0
    if required_ok:
        print("✅ All prerequisites satisfied!")
        return 0
    print("❌ Required prerequisites missing. Fix the issues above.")
    return 1


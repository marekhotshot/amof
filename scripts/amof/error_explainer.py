"""Error explanation and recovery guidance for AMOF."""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import List, Optional


class ErrorExplainer:
    """Provides context-aware error explanations and recovery suggestions."""
    
    @staticmethod
    def file_not_found(
        path: str,
        repo_path: Optional[Path] = None,
        search_similar: bool = True,
    ) -> str:
        """Explain file not found error with suggestions."""
        lines = [
            f"✗ File not found: {path}",
            "",
            "Context:",
        ]
        
        if repo_path and repo_path.exists():
            lines.append(f"  • Looking in: {repo_path / path}")
            lines.append(f"  • File doesn't exist at this location")
            
            # Find similar files
            if search_similar:
                similar = ErrorExplainer._find_similar_files(path, repo_path)
                if similar:
                    lines.append("")
                    lines.append("Did you mean one of these?")
                    for sim_path in similar[:5]:
                        lines.append(f"  • {sim_path}")
        else:
            lines.append(f"  • Path: {path}")
            lines.append(f"  • File doesn't exist")
        
        lines.extend([
            "",
            "Suggestions:",
            "  1. Check if file is in a different location",
            "  2. Verify the file path is correct",
            "  3. Check if the file needs to be created",
            "",
            "Helpful commands:",
            "  • List files:        amof ls <directory>",
            "  • Search for files:  amof glob '**/*<pattern>*'",
            "  • Check repo status: amof status --repo <name>",
        ])
        
        return "\n".join(lines)
    
    @staticmethod
    def _find_similar_files(target: str, repo_path: Path, max_results: int = 5) -> List[str]:
        """Find files with similar names in the repository."""
        target_name = Path(target).name
        similar = []
        
        try:
            for file_path in repo_path.rglob("*"):
                if file_path.is_file():
                    rel_path = file_path.relative_to(repo_path).as_posix()
                    file_name = file_path.name
                    
                    # Check for exact name match in different location
                    if file_name == target_name:
                        similar.append((1.0, rel_path))
                    # Check for similar names
                    elif difflib.SequenceMatcher(None, target_name.lower(), file_name.lower()).ratio() > 0.6:
                        ratio = difflib.SequenceMatcher(None, target_name.lower(), file_name.lower()).ratio()
                        similar.append((ratio, rel_path))
        except Exception:
            pass
        
        # Sort by similarity and return top results
        similar.sort(reverse=True, key=lambda x: x[0])
        return [path for _, path in similar[:max_results]]
    
    @staticmethod
    def permission_denied(path: str, operation: str = "access") -> str:
        """Explain permission denied error."""
        return "\n".join([
            f"✗ Permission denied: {path}",
            "",
            "Context:",
            f"  • Operation: {operation}",
            f"  • Path: {path}",
            "  • You don't have permission to access this file/directory",
            "",
            "Suggestions:",
            "  1. Check file permissions: ls -la <path>",
            "  2. Verify you own the file: ls -l <path>",
            "  3. Check if file is in a protected directory",
            "  4. Run with appropriate permissions if needed",
            "",
            "Common causes:",
            "  • File owned by different user",
            "  • Directory not readable/writable",
            "  • File in protected system directory",
        ])
    
    @staticmethod
    def guardrail_violation(path: str, pattern: str, action: str = "modify") -> str:
        """Explain guardrail violation."""
        return "\n".join([
            f"✗ Guardrail violation: Cannot {action} {path}",
            "",
            "Context:",
            f"  • Path: {path}",
            f"  • Blocked by pattern: {pattern}",
            f"  • Action: {action}",
            "",
            "Why this is blocked:",
            "  • This path is protected by guardrails",
            "  • Guardrails prevent accidental changes to sensitive files",
            "  • Configured in .amof/rules/guardrails.yaml",
            "",
            "What you can do:",
            "  1. If this is intentional, update guardrails.yaml",
            "  2. Check if you're targeting the right file",
            "  3. Use a different approach that doesn't modify protected files",
            "",
            "Protected paths include:",
            "  • .git/ directories",
            "  • .env files",
            "  • secrets/ directories",
            "  • Custom patterns in guardrails.yaml",
        ])
    
    @staticmethod
    def git_error(error_message: str, repo_path: Optional[Path] = None) -> str:
        """Explain git-related errors."""
        lines = [
            "✗ Git operation failed",
            "",
            f"Error: {error_message}",
            "",
        ]
        
        if "not a git repository" in error_message.lower():
            lines.extend([
                "Cause: Not in a git repository",
                "",
                "Suggestions:",
                "  1. Check if you're in the right directory",
                "  2. Initialize git: git init",
                "  3. Clone the repository if needed",
            ])
        elif "could not resolve host" in error_message.lower():
            lines.extend([
                "Cause: Network connectivity issue",
                "",
                "Suggestions:",
                "  1. Check your internet connection",
                "  2. Verify the repository URL is correct",
                "  3. Check if you need VPN access",
                "  4. Try again in a few moments",
            ])
        elif "authentication failed" in error_message.lower():
            lines.extend([
                "Cause: Authentication failed",
                "",
                "Suggestions:",
                "  1. Check your git credentials",
                "  2. Verify SSH key is configured: ssh -T git@github.com",
                "  3. Check if token/password is correct",
                "  4. Ensure .env has correct GIT_TOKEN",
            ])
        elif "merge conflict" in error_message.lower():
            lines.extend([
                "Cause: Merge conflict detected",
                "",
                "Suggestions:",
                "  1. Resolve conflicts manually",
                "  2. Use: git status (to see conflicted files)",
                "  3. Edit files to resolve conflicts",
                "  4. Stage resolved files: git add <file>",
                "  5. Complete merge: git commit",
            ])
        else:
            lines.extend([
                "Suggestions:",
                "  1. Check git status: git status",
                "  2. View git log: git log --oneline -5",
                "  3. Check remote: git remote -v",
            ])
        
        if repo_path:
            lines.extend([
                "",
                f"Repository: {repo_path}",
            ])
        
        return "\n".join(lines)
    
    @staticmethod
    def command_not_found(command: str) -> str:
        """Explain command not found error."""
        suggestions = {
            "git": "Install git: https://git-scm.com/downloads",
            "helm": "Install helm: https://helm.sh/docs/intro/install/",
            "kubectl": "Install kubectl: https://kubernetes.io/docs/tasks/tools/",
            "docker": "Install docker: https://docs.docker.com/get-docker/",
            "aws": "Install AWS CLI: https://aws.amazon.com/cli/",
        }
        
        lines = [
            f"✗ Command not found: {command}",
            "",
            "Context:",
            f"  • Command: {command}",
            "  • This command is not installed or not in PATH",
            "",
        ]
        
        if command in suggestions:
            lines.extend([
                "Installation:",
                f"  {suggestions[command]}",
                "",
            ])
        
        lines.extend([
            "Suggestions:",
            "  1. Install the required tool",
            "  2. Check if it's in your PATH: echo $PATH",
            "  3. Verify installation: which <command>",
        ])
        
        return "\n".join(lines)
    
    @staticmethod
    def invalid_yaml(error_message: str, file_path: Optional[str] = None) -> str:
        """Explain YAML parsing errors."""
        lines = [
            "✗ Invalid YAML syntax",
            "",
        ]
        
        if file_path:
            lines.append(f"File: {file_path}")
            lines.append("")
        
        lines.extend([
            f"Error: {error_message}",
            "",
            "Common YAML mistakes:",
            "  • Using tabs instead of spaces for indentation",
            "  • Missing colon after key name",
            "  • Incorrect indentation (must be consistent)",
            "  • Unquoted special characters (: # { } [ ] @ !)",
            "  • Missing space after colon",
            "",
            "Example of correct YAML:",
            "  repos:",
            "    - name: my-repo",
            "      url: git@github.com:org/repo.git",
            "      branch: main",
            "",
            "Helpful tools:",
            "  • Validate YAML: amof manifest validate",
            "  • Online validator: https://www.yamllint.com/",
        ])
        
        return "\n".join(lines)
    
    @staticmethod
    def wrap_error(error: Exception, context: Optional[str] = None) -> str:
        """Wrap any exception with helpful context."""
        error_type = type(error).__name__
        error_msg = str(error)
        
        lines = [
            f"✗ {error_type}: {error_msg}",
            "",
        ]
        
        if context:
            lines.extend([
                "Context:",
                f"  {context}",
                "",
            ])
        
        lines.extend([
            "What happened:",
            f"  • An error occurred: {error_type}",
            f"  • Message: {error_msg}",
            "",
            "Next steps:",
            "  1. Check the error message above",
            "  2. Verify your inputs are correct",
            "  3. Try running with --verbose for more details",
            "  4. Check logs if available",
        ])
        
        return "\n".join(lines)

import re
from html import unescape
from pathlib import Path
from typing import Any, List

from amof.manifest import get_journal_dir


class ContextBuilder:
    def __init__(
        self,
        workspace_root: Path,
        manifest: Any,
        base_prompt_path: Path,
        codebase_index: Any = None,
    ):
        self.workspace_root = workspace_root
        self.manifest = manifest
        self.base_prompt_path = base_prompt_path
        self.codebase_index = codebase_index

    def build(self, mode: str = "agent") -> str:
        prompt = ""
        if self.base_prompt_path.exists():
            prompt = self.base_prompt_path.read_text(encoding="utf-8")
            prompt = prompt.replace("{{MODE}}", mode)

        prompt += f"\n\nWorkspace Root: {self.workspace_root}\n"
        if self.manifest:
            prompt += f"Ecosystem: {self.manifest.get('name', 'unknown')}\n"
            repo_inventory = self._build_manifest_repo_inventory()
            if repo_inventory:
                prompt += "\n# Ecosystem Repositories\n"
                prompt += repo_inventory
            repo_snapshots = self._build_manifest_repo_snapshots()
            if repo_snapshots:
                prompt += "\n# Ecosystem Repo Entrypoints\n"
                prompt += repo_snapshots

        if self.codebase_index:
            prompt += "\n# Codebase Index\n"
            if hasattr(self.codebase_index, "to_context_string"):
                prompt += self.codebase_index.to_context_string()
            else:
                prompt += str(self.codebase_index)

        local_repo_snapshot = self._build_local_repo_snapshot()
        if local_repo_snapshot:
            prompt += "\n# Local Repository Snapshot\n"
            prompt += local_repo_snapshot

        return prompt

    def _build_local_repo_snapshot(self, max_files: int = 160) -> str:
        if (self.workspace_root / "repos").exists():
            return ""

        ecosystem_name = "unknown"
        if self.manifest:
            ecosystem_name = str(
                self.manifest.get("ecosystem")
                or self.manifest.get("name")
                or "unknown"
            )

        candidate_dirs = [
            "scripts/amof/commands",
            "scripts/amof/orchestrator",
            "scripts/amof/api",
            "scripts/amof/queue",
            "tests",
            "prompts",
            f"ecosystems/{ecosystem_name}/plans",
            f"ecosystems/{ecosystem_name}/journal",
        ]
        allowed_suffixes = {".py", ".md", ".json", ".yaml", ".yml", ".ts", ".tsx"}

        files = []
        for rel_dir in candidate_dirs:
            base = self.workspace_root / rel_dir
            if not base.exists():
                continue
            for path in sorted(base.rglob("*")):
                if not path.is_file():
                    continue
                if path.suffix and path.suffix not in allowed_suffixes:
                    continue
                files.append(path.relative_to(self.workspace_root).as_posix())
                if len(files) >= max_files:
                    break
            if len(files) >= max_files:
                break

        if not files:
            return ""

        lines = [
            "Current repo root does not contain a top-level `repos/` directory.",
            "Use these live canonical file paths from the current checkout:",
            "",
        ]
        canonical_journal_dir = get_journal_dir(ecosystem_name)
        try:
            canonical_journal_display = canonical_journal_dir.relative_to(self.workspace_root).as_posix()
        except ValueError:
            canonical_journal_display = canonical_journal_dir.as_posix()
        lines.append(f"- Canonical journal: `{canonical_journal_display}`")
        for file_path in files:
            lines.append(f"- `{file_path}`")
        lines.append("")
        return "\n".join(lines)

    def _build_manifest_repo_snapshots(self, max_content_chars: int = 280) -> str:
        repos = []
        if self.manifest:
            repos = self.manifest.get("repos", []) or []
        if not repos:
            return ""

        lines = [
            "Ground planning against these real files from the current repo checkouts.",
            "Prefer these paths over guessed framework defaults.",
            "",
        ]
        added = 0
        for repo in repos:
            if not isinstance(repo, dict) or not repo.get("enabled", True):
                continue
            repo_name = str(repo.get("name") or "unknown")
            repo_path = str(repo.get("path") or "")
            if not repo_path:
                continue
            repo_root = self.workspace_root / repo_path
            if not repo_root.exists() or not repo_root.is_dir():
                continue

            lines.append(f"## `{repo_name}`")
            lines.append(f"- Root: `{repo_path}`")
            top_level = self._top_level_entries(repo_root)
            if top_level:
                lines.append(f"- Top level: {', '.join(f'`{entry}`' for entry in top_level)}")

            for rel_path in self._candidate_entrypoints(repo_root):
                full_path = repo_root / rel_path
                descriptor = f"- Candidate: `{repo_path}/{rel_path.as_posix()}`"
                if full_path.is_dir():
                    lines.append(descriptor + " (directory)")
                    continue
                preview = self._content_preview(full_path, max_content_chars=max_content_chars)
                if preview:
                    lines.append(f"{descriptor} -> `{preview}`")
                else:
                    lines.append(descriptor)
            lines.append("")
            added += 1

        if added == 0:
            return ""
        return "\n".join(lines)

    def _top_level_entries(self, repo_root: Path, limit: int = 8) -> List[str]:
        entries = []
        for child in sorted(repo_root.iterdir(), key=lambda path: path.name):
            suffix = "/" if child.is_dir() else ""
            entries.append(f"{child.name}{suffix}")
            if len(entries) >= limit:
                break
        return entries

    def _candidate_entrypoints(self, repo_root: Path) -> List[Path]:
        candidates = [
            Path("README.md"),
            Path("package.json"),
            Path("index.html"),
            Path("src"),
            Path("app"),
            Path("pages"),
            Path("main.py"),
            Path("app.py"),
            Path("server.py"),
            Path("Chart.yaml"),
            Path("values.yaml"),
            Path("templates"),
        ]
        return [candidate for candidate in candidates if (repo_root / candidate).exists()]

    def _content_preview(self, path: Path, max_content_chars: int = 280) -> str:
        text = path.read_text(encoding="utf-8", errors="ignore").strip()
        if path.suffix.lower() in {".html", ".htm"}:
            visible = self._html_visible_text(text)
            if visible:
                return self._truncate_preview(visible, max_content_chars)
        collapsed = " ".join(text.split())
        return self._truncate_preview(collapsed, max_content_chars)

    def _html_visible_text(self, text: str) -> str:
        without_scripts = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.IGNORECASE | re.DOTALL)
        without_styles = re.sub(r"<style\b[^>]*>.*?</style>", " ", without_scripts, flags=re.IGNORECASE | re.DOTALL)
        no_tags = re.sub(r"<[^>]+>", " ", without_styles)
        collapsed = " ".join(unescape(no_tags).split())
        return collapsed

    def _truncate_preview(self, text: str, max_content_chars: int) -> str:
        if len(text) > max_content_chars:
            return text[: max_content_chars - 3] + "..."
        return text

    def _build_manifest_repo_inventory(self) -> str:
        repos = []
        if self.manifest:
            repos = self.manifest.get("repos", []) or []
        if not repos:
            return ""

        lines = [
            "Use only these canonical repo checkout paths when planning reads or writes.",
            "Do not invent alternate repo names or synthetic paths.",
            "",
        ]
        for repo in repos:
            if not isinstance(repo, dict) or not repo.get("enabled", True):
                continue
            name = str(repo.get("name") or "unknown")
            path = str(repo.get("path") or "")
            readonly = "readonly" if repo.get("readonly") else "writable"
            if path:
                lines.append(f"- `{name}` -> `{path}` ({readonly})")
            else:
                lines.append(f"- `{name}` ({readonly})")
        lines.append("")
        return "\n".join(lines)

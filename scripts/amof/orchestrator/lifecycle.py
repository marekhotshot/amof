"""Ecosystem Lifecycle Manager."""

from __future__ import annotations

from pathlib import Path
from datetime import datetime

from amof.manifest import get_ecosystems_dir

class EcosystemManager:
    def __init__(self, base_dir: str | Path | None = None):
        self.base_dir = Path(base_dir) if base_dir is not None else get_ecosystems_dir()

    def create_ecosystem(self, name: str, repos: list[str], description: str | None = None) -> dict:
        """Scaffold the folder, create ecosystem.yaml, and init kb/."""
        ecosystem_dir = self.base_dir / name
        if ecosystem_dir.exists():
            raise ValueError(f"Ecosystem {name} already exists.")
        
        ecosystem_dir.mkdir(parents=True)
        
        # Create ecosystem.yaml
        manifest_path = ecosystem_dir / "ecosystem.yaml"
        desc = (description or "Generated ecosystem").strip() or "Generated ecosystem"
        manifest_content = f"name: {name}\ndescription: {repr(desc)}\nrepos:\n"
        for repo in repos:
            if isinstance(repo, str):
                repo_name = repo
                repo_url = ''
            elif isinstance(repo, dict):
                repo_name = repo.get('name', 'unnamed')
                repo_url = repo.get('url', '')
            else:
                repo_name = getattr(repo, 'name', 'unnamed')
                repo_url = getattr(repo, 'url', '')
            manifest_content += f"  - name: {repo_name}\n"
            manifest_content += f"    url: '{repo_url}'\n"
        manifest_path.write_text(manifest_content)
        
        # Init kb/
        kb_dir = ecosystem_dir / "kb"
        kb_dir.mkdir(exist_ok=True)
        (kb_dir / "README.md").write_text(f"# {name} Knowledge Base\n")
        
        # Init journal/
        journal_dir = ecosystem_dir / "journal"
        journal_dir.mkdir(exist_ok=True)
        
        return {"status": "success", "ecosystem": name, "path": str(ecosystem_dir)}

    def add_ticket(self, ecosystem: str, title: str, description: str) -> dict:
        """Create a formatted markdown file in journal/ and mark it for indexing."""
        ecosystem_dir = self.base_dir / ecosystem
        if not ecosystem_dir.exists():
            raise ValueError(f"Ecosystem {ecosystem} does not exist.")
            
        journal_dir = ecosystem_dir / "journal"
        journal_dir.mkdir(exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_title = "".join(c if c.isalnum() else "_" for c in title).lower()
        file_name = f"{timestamp}_{safe_title}.md"
        file_path = journal_dir / file_name
        
        content = f"# {title}\n\nDate: {datetime.now().isoformat()}\n\n## Description\n{description}\n\nTags: #ticket #indexed\n"
        file_path.write_text(content)
        
        return {"status": "success", "ticket_file": str(file_path)}

    def delete_ecosystem(self, ecosystem: str) -> dict:
        """Delete an ecosystem directory and all its contents."""
        ecosystem_dir = self.base_dir / ecosystem
        if not ecosystem_dir.exists():
            raise ValueError(f"Ecosystem {ecosystem} does not exist.")
            
        import shutil
        shutil.rmtree(ecosystem_dir)
        
        return {"status": "success", "ecosystem": ecosystem}

    def get_ecosystem_summary(self, ecosystem: str) -> dict:
        """Return stats about files, last index time, and active tasks."""
        ecosystem_dir = self.base_dir / ecosystem
        if not ecosystem_dir.exists():
            raise ValueError(f"Ecosystem {ecosystem} does not exist.")
            
        kb_files = len(list((ecosystem_dir / "kb").glob("*.md"))) if (ecosystem_dir / "kb").exists() else 0
        journal_files = len(list((ecosystem_dir / "journal").glob("*.md"))) if (ecosystem_dir / "journal").exists() else 0
        
        # Parse manifest for repos
        repos = []
        manifest_path = ecosystem_dir / "ecosystem.yaml"
        if manifest_path.exists():
            try:
                import yaml
                with open(manifest_path, 'r') as f:
                    manifest = yaml.safe_load(f)
                if manifest and "repos" in manifest:
                    repos = manifest["repos"]
            except ImportError:
                # Fallback to simple parsing if yaml not available
                pass

        return {
            "name": ecosystem,
            "path": str(ecosystem_dir),
            "repos": repos,
            "kb_files_count": kb_files,
            "journal_files_count": journal_files,
            "last_index_time": "N/A",
            "active_tasks": 0
        }

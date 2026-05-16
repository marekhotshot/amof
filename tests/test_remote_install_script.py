import os
import shutil
import subprocess
import json
from pathlib import Path
from tempfile import TemporaryDirectory


REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SCRIPT = REPO_ROOT / "install.sh"


def _run_remote_install(
    cwd: Path,
    *args: str,
    home: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if home is not None:
        env["HOME"] = str(home)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(cwd / "install.sh"), *args],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _workspace_pollution_state() -> dict[str, bool]:
    return {
        ".amof": (REPO_ROOT / ".amof").exists(),
        ".amof-worktrees": (REPO_ROOT / ".amof-worktrees").exists(),
    }


def test_remote_install_help_mentions_safer_review_flow() -> None:
    result = _run_remote_install(REPO_ROOT, "--help")

    assert result.returncode == 0, result.stderr
    assert "Safer review flow" in result.stdout
    assert "--channel <name>" in result.stdout


def test_remote_install_dry_run_delegates_in_repo_mode_without_writes() -> None:
    with TemporaryDirectory(prefix="amof-remote-install-dry-run-") as tmp_dir:
        tmp_root = Path(tmp_dir)
        install_dir = tmp_root / "bin"
        amof_home = tmp_root / "appdata"

        result = _run_remote_install(
            REPO_ROOT,
            "--dry-run",
            "--channel",
            "stable",
            "--version",
            "dev-checkout",
            "--install-dir",
            str(install_dir),
            "--amof-home",
            str(amof_home),
            "--context",
            "local",
            "--no-shell-profile",
            home=tmp_root / "home",
        )

        assert result.returncode == 0, result.stderr
        assert "repo checkout detected" in result.stdout
        assert "dry-run: would run" in result.stdout
        assert not install_dir.exists()
        assert not amof_home.exists()


def test_remote_install_real_repo_mode_is_idempotent() -> None:
    with TemporaryDirectory(prefix="amof-remote-install-real-") as tmp_dir:
        tmp_root = Path(tmp_dir)
        install_dir = tmp_root / "bin"
        amof_home = tmp_root / "appdata"
        bashrc = tmp_root / "home" / ".bashrc"
        bashrc.parent.mkdir(parents=True, exist_ok=True)
        bashrc.write_text("# remote installer profile\n", encoding="utf-8")
        pollution_before = _workspace_pollution_state()

        first_result = _run_remote_install(
            REPO_ROOT,
            "--install-dir",
            str(install_dir),
            "--amof-home",
            str(amof_home),
            "--context",
            "local",
            home=tmp_root / "home",
        )
        second_result = _run_remote_install(
            REPO_ROOT,
            "--install-dir",
            str(install_dir),
            "--amof-home",
            str(amof_home),
            "--context",
            "local",
            home=tmp_root / "home",
        )

        assert first_result.returncode == 0, first_result.stderr
        assert second_result.returncode == 0, second_result.stderr
        assert bashrc.read_text(encoding="utf-8") == "# remote installer profile\n"

        wrapper = install_dir / "amof"
        assert wrapper.exists()
        paths_result = subprocess.run(
            [str(wrapper), "paths", "--json"],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert paths_result.returncode == 0, paths_result.stderr
        payload = json.loads(paths_result.stdout)
        assert payload["install_metadata"]["channel"] == "stable"
        assert payload["install_metadata"]["install_method"] == "remote-installer-skeleton"

        current_result = subprocess.run(
            [str(wrapper), "context", "current"],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert current_result.returncode == 0, current_result.stderr
        assert current_result.stdout.strip() == "local"
        assert _workspace_pollution_state() == pollution_before


def test_remote_install_outside_repo_fails_until_artifacts_exist() -> None:
    with TemporaryDirectory(prefix="amof-remote-install-nonrepo-") as tmp_dir:
        tmp_root = Path(tmp_dir)
        script_copy = tmp_root / "install.sh"
        shutil.copy2(INSTALL_SCRIPT, script_copy)

        result = _run_remote_install(
            tmp_root,
            "--install-dir",
            str(tmp_root / "bin"),
            "--context",
            "local",
            home=tmp_root / "home",
        )

        assert result.returncode == 1
        assert "Release artifact download not implemented yet outside a repo checkout" in result.stderr


def test_remote_install_can_register_workspace_in_repo_mode() -> None:
    with TemporaryDirectory(prefix="amof-remote-install-register-") as tmp_dir:
        tmp_root = Path(tmp_dir)
        install_dir = tmp_root / "bin"
        amof_home = tmp_root / "appdata"
        repo_dir = tmp_root / "workspace-repo"
        repo_dir.mkdir()
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True, text=True)

        result = _run_remote_install(
            REPO_ROOT,
            "--install-dir",
            str(install_dir),
            "--amof-home",
            str(amof_home),
            "--context",
            "local",
            "--register-workspace",
            "smoke",
            "--workspace-repo",
            str(repo_dir),
            "--no-shell-profile",
            home=tmp_root / "home",
        )

        assert result.returncode == 0, result.stderr

        wrapper = install_dir / "amof"
        show_result = subprocess.run(
            [str(wrapper), "workspace", "show", "smoke"],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert show_result.returncode == 0, show_result.stderr
        assert str(repo_dir.resolve()) in show_result.stdout

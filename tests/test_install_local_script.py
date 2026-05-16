import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install-local.sh"


def _run_install(
    tmp_root: Path,
    *args: str,
    home_name: str = "home",
    extra_env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(tmp_root / home_name)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(INSTALL_SCRIPT), *args],
        cwd=cwd or REPO_ROOT,
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


def _run_installed_command(wrapper: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(wrapper), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_install_local_dry_run_writes_nothing() -> None:
    with TemporaryDirectory(prefix="amof-install-local-dry-run-") as tmp_dir:
        tmp_root = Path(tmp_dir)
        install_dir = tmp_root / "bin"
        amof_home = tmp_root / "appdata"
        pollution_before = _workspace_pollution_state()

        result = _run_install(
            tmp_root,
            "--dry-run",
            "--install-dir",
            str(install_dir),
            "--amof-home",
            str(amof_home),
            "--context",
            "local",
            "--no-shell-profile",
        )

        assert result.returncode == 0, result.stderr
        assert "dry-run: would create" in result.stdout
        assert not install_dir.exists()
        assert not amof_home.exists()
        assert _workspace_pollution_state() == pollution_before


def test_install_local_creates_wrapper_and_bootstraps_appdata() -> None:
    with TemporaryDirectory(prefix="amof-install-local-real-") as tmp_dir:
        tmp_root = Path(tmp_dir)
        install_dir = tmp_root / "bin"
        amof_home = tmp_root / "appdata"
        pollution_before = _workspace_pollution_state()

        install_result = _run_install(
            tmp_root,
            "--install-dir",
            str(install_dir),
            "--amof-home",
            str(amof_home),
            "--context",
            "local",
            "--no-shell-profile",
        )

        assert install_result.returncode == 0, install_result.stderr

        wrapper = install_dir / "amof"
        assert wrapper.exists()
        assert os.access(wrapper, os.X_OK)

        paths_result = _run_installed_command(wrapper, "paths", "--json")
        assert paths_result.returncode == 0, paths_result.stderr
        payload = json.loads(paths_result.stdout)
        assert payload["config_root"] == str((amof_home / "config").resolve())
        assert payload["data_root"] == str((amof_home / "share").resolve())
        assert payload["cache_root"] == str((amof_home / "cache").resolve())
        assert payload["state_root"] == str((amof_home / "state").resolve())
        assert payload["install_metadata_file"] == str((amof_home / "config" / "install-metadata.json").resolve())
        assert payload["install_metadata"]["channel"] == "dev"
        assert payload["install_metadata"]["install_method"] == "local-dev-wrapper"

        current_result = _run_installed_command(wrapper, "context", "current")
        assert current_result.returncode == 0, current_result.stderr
        assert current_result.stdout.strip() == "local"

        reinstall_result = _run_install(
            tmp_root,
            "--install-dir",
            str(install_dir),
            "--amof-home",
            str(amof_home),
            "--context",
            "local",
            "--no-shell-profile",
        )
        assert reinstall_result.returncode == 0, reinstall_result.stderr

        assert _workspace_pollution_state() == pollution_before


def test_install_local_repeated_runs_preserve_config_and_shell_profile() -> None:
    with TemporaryDirectory(prefix="amof-install-local-repeat-") as tmp_dir:
        tmp_root = Path(tmp_dir)
        install_dir = tmp_root / "bin"
        amof_home = tmp_root / "appdata"
        bashrc = tmp_root / "home" / ".bashrc"
        bashrc.parent.mkdir(parents=True, exist_ok=True)
        bashrc.write_text("# existing shell profile\n", encoding="utf-8")
        (amof_home / "config").mkdir(parents=True, exist_ok=True)
        (amof_home / "config" / "contexts.yaml").write_text(
            yaml.safe_dump(
                {
                    "contexts": {
                        "custom": {
                            "controlplane": {"mode": "remote-api", "url": "https://example.invalid", "deployment_variant": "dev"},
                            "execution": {"backend": "local"},
                            "workspace": {"backend": "local-appdata", "default_registry_entry": None},
                            "evidence": {"backend": "local-appdata"},
                            "credentials": {"provider_profile_refs": [], "kubeconfig_ref": None},
                            "safety": {"protected": False, "require_confirmation": False, "no_push_default": True, "dry_run_default": True},
                            "promotion": {"default_policy": "evidence-gated-dry-run"},
                        }
                    }
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        (amof_home / "config" / "config.yaml").write_text("current_context: custom\n", encoding="utf-8")
        pollution_before = _workspace_pollution_state()

        first_result = _run_install(
            tmp_root,
            "--install-dir",
            str(install_dir),
            "--amof-home",
            str(amof_home),
            "--context",
            "local",
        )
        second_result = _run_install(
            tmp_root,
            "--install-dir",
            str(install_dir),
            "--amof-home",
            str(amof_home),
            "--context",
            "local",
        )

        assert first_result.returncode == 0, first_result.stderr
        assert second_result.returncode == 0, second_result.stderr
        assert bashrc.read_text(encoding="utf-8") == "# existing shell profile\n"

        wrapper = install_dir / "amof"
        current_result = _run_installed_command(wrapper, "context", "current")
        assert current_result.returncode == 0, current_result.stderr
        assert current_result.stdout.strip() == "local"

        contexts_payload = yaml.safe_load((amof_home / "config" / "contexts.yaml").read_text(encoding="utf-8"))
        assert "custom" in contexts_payload["contexts"]
        assert "local" in contexts_payload["contexts"]

        config_payload = yaml.safe_load((amof_home / "config" / "config.yaml").read_text(encoding="utf-8"))
        assert config_payload["current_context"] == "local"
        metadata_payload = json.loads((amof_home / "config" / "install-metadata.json").read_text(encoding="utf-8"))
        assert metadata_payload["channel"] == "dev"
        assert metadata_payload["install_method"] == "local-dev-wrapper"
        assert _workspace_pollution_state() == pollution_before


def test_install_local_uses_xdg_layout_without_amof_home() -> None:
    with TemporaryDirectory(prefix="amof-install-local-xdg-") as tmp_dir:
        tmp_root = Path(tmp_dir)
        install_dir = tmp_root / "bin"
        xdg_root = tmp_root / "xdg"

        result = _run_install(
            tmp_root,
            "--install-dir",
            str(install_dir),
            "--context",
            "local",
            "--no-shell-profile",
            extra_env={
                "XDG_CONFIG_HOME": str(xdg_root / "config"),
                "XDG_DATA_HOME": str(xdg_root / "data"),
                "XDG_CACHE_HOME": str(xdg_root / "cache"),
                "XDG_STATE_HOME": str(xdg_root / "state"),
            },
        )

        assert result.returncode == 0, result.stderr

        wrapper = install_dir / "amof"
        paths_result = subprocess.run(
            [str(wrapper), "paths", "--json"],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
            env={
                **os.environ,
                "HOME": str(tmp_root / "home"),
                "XDG_CONFIG_HOME": str(xdg_root / "config"),
                "XDG_DATA_HOME": str(xdg_root / "data"),
                "XDG_CACHE_HOME": str(xdg_root / "cache"),
                "XDG_STATE_HOME": str(xdg_root / "state"),
            },
        )
        assert paths_result.returncode == 0, paths_result.stderr
        payload = json.loads(paths_result.stdout)
        assert payload["config_root"] == str((xdg_root / "config" / "amof").resolve())
        assert payload["data_root"] == str((xdg_root / "data" / "amof").resolve())
        assert payload["cache_root"] == str((xdg_root / "cache" / "amof").resolve())
        assert payload["state_root"] == str((xdg_root / "state" / "amof").resolve())


def test_install_local_registers_workspace_when_explicit_repo_is_provided() -> None:
    with TemporaryDirectory(prefix="amof-install-local-register-") as tmp_dir:
        tmp_root = Path(tmp_dir)
        install_dir = tmp_root / "bin"
        amof_home = tmp_root / "appdata"
        repo_dir = tmp_root / "workspace-repo"
        repo_dir.mkdir()
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True, text=True)

        result = _run_install(
            tmp_root,
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
        )

        assert result.returncode == 0, result.stderr

        wrapper = install_dir / "amof"
        show_result = _run_installed_command(wrapper, "workspace", "show", "smoke")
        assert show_result.returncode == 0, show_result.stderr
        assert str(repo_dir.resolve()) in show_result.stdout


def test_install_local_register_workspace_requires_git_repo_or_explicit_path() -> None:
    with TemporaryDirectory(prefix="amof-install-local-register-fail-") as tmp_dir:
        tmp_root = Path(tmp_dir)
        result = _run_install(
            tmp_root,
            "--install-dir",
            str(tmp_root / "bin"),
            "--amof-home",
            str(tmp_root / "appdata"),
            "--context",
            "local",
            "--register-workspace",
            "missing-repo",
            cwd=tmp_root,
        )

        assert result.returncode == 1
        assert "--register-workspace requires --workspace-repo or a current working directory inside a git repo" in result.stderr

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from amof.app_paths import get_app_paths, studio_dir


def test_amof_home_uses_flat_app_root(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AMOF_HOME", str(tmp_path / ".amof"))

    paths = get_app_paths()

    assert paths.config_root == (tmp_path / ".amof" / "config").resolve()
    assert paths.data_root == (tmp_path / ".amof" / "share").resolve()
    assert paths.cache_root == (tmp_path / ".amof" / "cache").resolve()
    assert paths.state_root == (tmp_path / ".amof" / "state").resolve()


def test_xdg_resolution_uses_amof_suffix(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("AMOF_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    paths = get_app_paths()

    assert paths.config_root == (tmp_path / "cfg" / "amof").resolve()
    assert paths.data_root == (tmp_path / "data" / "amof").resolve()
    assert paths.cache_root == (tmp_path / "cache" / "amof").resolve()
    assert paths.state_root == (tmp_path / "state" / "amof").resolve()


def test_studio_dir_resolves_under_data_root(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AMOF_HOME", str(tmp_path / ".amof"))

    assert studio_dir() == (tmp_path / ".amof" / "share" / "studio").resolve()

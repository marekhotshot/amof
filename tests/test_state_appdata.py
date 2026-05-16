import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import amof.state as state_module


def test_save_state_writes_to_appdata_by_default(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AMOF_HOME", str(tmp_path / ".amof-home"))
    monkeypatch.setattr(state_module, "STATE_DIR", tmp_path / ".amof-home" / "config")
    monkeypatch.setattr(state_module, "STATE_FILE", tmp_path / ".amof-home" / "config" / "state.json")

    state_module.save_state({"version": 3, "ecosystem": "demo", "tickets": {}})

    saved_path = tmp_path / ".amof-home" / "config" / "state.json"
    assert saved_path.exists()
    assert json.loads(saved_path.read_text(encoding="utf-8"))["ecosystem"] == "demo"


def test_get_state_falls_back_to_legacy_workspace_state(tmp_path, monkeypatch) -> None:
    workspace_root = tmp_path / "workspace"
    (workspace_root / "ecosystems").mkdir(parents=True)
    legacy_state = workspace_root / ".amof" / "state.json"
    legacy_state.parent.mkdir(parents=True, exist_ok=True)
    legacy_state.write_text(
        json.dumps({"version": 3, "ecosystem": "legacy-demo", "tickets": {}}),
        encoding="utf-8",
    )

    monkeypatch.setenv("AMOF_WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setattr(state_module, "STATE_DIR", tmp_path / ".amof-home" / "config")
    monkeypatch.setattr(state_module, "STATE_FILE", tmp_path / ".amof-home" / "config" / "state.json")

    loaded = state_module.get_state()

    assert loaded["ecosystem"] == "legacy-demo"

"""Generated-build detector, renderer, and build-proof helpers.

`detect_runtime(repo_root)` is still read-only: it inspects file
presence and parses lightweight manifest fields, but never writes a
Dockerfile and never runs `docker build`.

UP9-2 adds two explicit follow-up steps:

* `render_dockerfile(...)` materialises the selected first-wave
  Dockerfile template under `.amof/generated-builds/...` and keeps the
  artifact at `status: proposed`.
* `run_build_proof(...)` runs local `docker build`, records the image
  digest, and advances the artifact to `status: build_proven`.

Scope:

* python, node, and go families are first-wave supported.
* polyglot, deferred families, missing signals, existing Dockerfile —
  all handled via the matching refusal_reason from
  `contracts/generated-build-runtime-detector-matrix.md`.
* Runtime proof remains out of scope. This module never starts the
  generated image as a container.
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# Family identifiers as used by the contract schema's runtime_family enum.
FAMILY_PYTHON = "python"
FAMILY_NODE = "node"
FAMILY_GO = "go"
FAMILY_UNKNOWN = "unknown"

SUPPORTED_FAMILIES = (FAMILY_PYTHON, FAMILY_NODE, FAMILY_GO)

# Existing-build pre-emption: any of these at the repo root means the
# existing-build lane wins and the generated lane refuses.
EXISTING_BUILD_FILES = (
    "Dockerfile",
    "Dockerfile.controlplane",
)

# Hard signals per family. Order matters only for stable iteration.
PYTHON_HARD_SIGNAL_FILES = (
    "pyproject.toml",
    "requirements.txt",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "poetry.lock",
)
NODE_HARD_SIGNAL_FILES = (
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
)
GO_HARD_SIGNAL_FILES = (
    "go.mod",
    "go.sum",
)

# Python soft signal: common entrypoint filenames.
PYTHON_SOFT_ENTRYPOINTS = (
    "app.py",
    "main.py",
    "wsgi.py",
    "asgi.py",
)

# Python soft signal: framework / runtime hints found in requirements.
PYTHON_FRAMEWORK_HINTS = (
    "fastapi",
    "flask",
    "django",
    "uvicorn",
    "gunicorn",
)

NODE_SOFT_ENTRYPOINTS = (
    "index.js",
    "server.js",
    "app.js",
)
NODE_FRAMEWORK_HINTS = (
    "express",
    "fastify",
    "koa",
    "next",
)

GO_SOFT_ENTRYPOINTS = (
    "main.go",
)

# Template ids pinned in the design tranche (CP5).
PYTHON_TEMPLATE_ID = "python-uvicorn-distroless-v1"
PYTHON_TEMPLATE_VERSION = "1.0.0"
NODE_TEMPLATE_ID = "node-express-distroless-v1"
NODE_TEMPLATE_VERSION = "1.0.0"
GO_TEMPLATE_ID = "go-stdlib-distroless-v1"
GO_TEMPLATE_VERSION = "1.0.0"


@dataclass
class _DetectedSignals:
    python_hard: List[Dict[str, str]]
    node_hard: List[Dict[str, str]]
    go_hard: List[Dict[str, str]]
    python_soft: List[Dict[str, str]]
    node_soft: List[Dict[str, str]]
    go_soft: List[Dict[str, str]]


def detect_runtime(repo_root: Path) -> Dict:
    """Detect the runtime family of `repo_root` and return a generated-build artifact.

    The returned dict conforms to
    `contracts/generated-build-contract.schema.json`. The status will
    be `proposed` (with `template_not_yet_rendered` in `risk_flags`)
    only when detection succeeds with `confidence == accepted`.
    Otherwise it will be `refused` with a `refusal_reason` taken from
    `generated-build-runtime-detector-matrix.md`.
    """
    repo_root = Path(repo_root)
    if not repo_root.exists():
        return _refused(
            repo_root,
            refusal_reason="no_runtime_signal",
            extra_risk=[f"repo_root_missing: {repo_root}"],
        )
    if not repo_root.is_dir():
        return _refused(
            repo_root,
            refusal_reason="no_runtime_signal",
            extra_risk=[f"repo_root_not_a_directory: {repo_root}"],
        )

    # 1. Existing-build pre-emption. The existing lane wins; the
    #    generated lane refuses cleanly so the audit trail is symmetric.
    existing = _existing_build_files_present(repo_root)
    if existing:
        return _refused(
            repo_root,
            refusal_reason="existing_build_contract_present",
            extra_risk=[f"existing_build_files: {sorted(existing)}"],
        )

    # 2. Detect hard and soft signals across all in-wave families.
    signals = _detect_signals(repo_root)

    families_with_hard_signals = [
        family
        for family, hard in (
            (FAMILY_PYTHON, signals.python_hard),
            (FAMILY_NODE, signals.node_hard),
            (FAMILY_GO, signals.go_hard),
        )
        if hard
    ]

    # 3. Polyglot conflict refusal: hard signals from two or more
    #    in-wave families.
    if len(families_with_hard_signals) >= 2:
        return _refused(
            repo_root,
            refusal_reason="polyglot_repo_no_per_service_map",
            extra_risk=_polyglot_risk_flags(signals),
        )

    # 4. No signals at all → no_runtime_signal.
    if not families_with_hard_signals:
        return _refused(
            repo_root,
            refusal_reason="no_runtime_signal",
            extra_risk=[],
        )

    only_family = families_with_hard_signals[0]

    # 5. First-wave family paths.
    if only_family == FAMILY_PYTHON:
        return _python_artifact(repo_root, signals)
    if only_family == FAMILY_NODE:
        return _node_artifact(repo_root, signals)
    if only_family == FAMILY_GO:
        return _go_artifact(repo_root, signals)

    return _refused(
        repo_root,
        refusal_reason="runtime_family_not_in_first_wave",
        extra_risk=[f"detected_family: {only_family}"],
    )


def _python_artifact(repo_root: Path, signals: _DetectedSignals) -> Dict:
    """Build the python artifact. Refuses if soft signals are absent."""
    if not signals.python_soft:
        return _refused(
            repo_root,
            refusal_reason="template_inputs_missing",
            extra_risk=[
                "python_hard_signals_present_but_no_soft_signal",
                "no_app_py_main_py_wsgi_py_asgi_py_or_known_framework_dep",
            ],
        )

    confidence = "accepted"  # at least 1 hard + 1 soft + no conflict (asserted above)

    inferred_port, entrypoint = _python_entrypoint(signals)

    risk_flags = ["template_not_yet_rendered"]

    return {
        "build_contract_kind": "generated",
        "source_repo": {"host_path": str(repo_root)},
        "runtime_family": FAMILY_PYTHON,
        "confidence": confidence,
        "signals": {
            "hard": signals.python_hard,
            "soft": signals.python_soft,
        },
        "dockerfile_template": {
            "id": PYTHON_TEMPLATE_ID,
            "version": PYTHON_TEMPLATE_VERSION,
        },
        "image_outputs": [
            {
                "push_image": "(unrendered; image name not yet produced by detector slice)",
                "pull_image": "(unrendered; image name not yet produced by detector slice)",
            }
        ],
        "inferred_port": inferred_port,
        "entrypoint": entrypoint,
        "risk_flags": risk_flags,
        "status": "proposed",
    }


def _node_artifact(repo_root: Path, signals: _DetectedSignals) -> Dict:
    if not signals.node_soft:
        return _refused(
            repo_root,
            refusal_reason="template_inputs_missing",
            extra_risk=[
                "node_hard_signals_present_but_no_soft_signal",
                "no_package_json_start_main_or_known_framework_dep",
            ],
        )

    inferred_port, entrypoint = _node_entrypoint(repo_root, signals)
    return {
        "build_contract_kind": "generated",
        "source_repo": {"host_path": str(repo_root)},
        "runtime_family": FAMILY_NODE,
        "confidence": "accepted",
        "signals": {
            "hard": signals.node_hard,
            "soft": signals.node_soft,
        },
        "dockerfile_template": {
            "id": NODE_TEMPLATE_ID,
            "version": NODE_TEMPLATE_VERSION,
        },
        "image_outputs": [
            {
                "push_image": "(unrendered; image name not yet produced by detector slice)",
                "pull_image": "(unrendered; image name not yet produced by detector slice)",
            }
        ],
        "inferred_port": inferred_port,
        "entrypoint": entrypoint,
        "risk_flags": ["template_not_yet_rendered"],
        "status": "proposed",
    }


def _go_artifact(repo_root: Path, signals: _DetectedSignals) -> Dict:
    if not signals.go_soft:
        return _refused(
            repo_root,
            refusal_reason="template_inputs_missing",
            extra_risk=[
                "go_hard_signals_present_but_no_main_go",
            ],
        )

    return {
        "build_contract_kind": "generated",
        "source_repo": {"host_path": str(repo_root)},
        "runtime_family": FAMILY_GO,
        "confidence": "accepted",
        "signals": {
            "hard": signals.go_hard,
            "soft": signals.go_soft,
        },
        "dockerfile_template": {
            "id": GO_TEMPLATE_ID,
            "version": GO_TEMPLATE_VERSION,
        },
        "image_outputs": [
            {
                "push_image": "(unrendered; image name not yet produced by detector slice)",
                "pull_image": "(unrendered; image name not yet produced by detector slice)",
            }
        ],
        "inferred_port": 8080,
        "entrypoint": "/server",
        "risk_flags": ["template_not_yet_rendered"],
        "status": "proposed",
    }


def _python_entrypoint(signals: _DetectedSignals) -> Tuple[int, str]:
    """Pick a sensible (port, entrypoint) for the python template.

    Prefers ASGI (`uvicorn` + `app.py` or `asgi.py`) over WSGI. Falls
    back to a generic uvicorn invocation when no clear entrypoint
    file was matched. Port is conservative: 8000 for ASGI, 8000 for
    gunicorn WSGI.
    """
    soft_values = {sig["value"] for sig in signals.python_soft}
    has_uvicorn = "uvicorn" in soft_values
    has_gunicorn = "gunicorn" in soft_values
    has_fastapi = "fastapi" in soft_values
    has_flask = "flask" in soft_values
    has_django = "django" in soft_values

    entrypoint_files = {sig["value"] for sig in signals.python_soft if sig["kind"] == "entrypoint_filename"}

    if has_uvicorn or has_fastapi:
        target = "app:app"
        if "asgi.py" in entrypoint_files:
            target = "asgi:application"
        elif "main.py" in entrypoint_files and "app.py" not in entrypoint_files:
            target = "main:app"
        return 8000, f"uvicorn {target} --host 0.0.0.0 --port 8000"

    if has_gunicorn or has_flask:
        target = "app:app"
        if "wsgi.py" in entrypoint_files:
            target = "wsgi:application"
        elif "main.py" in entrypoint_files and "app.py" not in entrypoint_files:
            target = "main:app"
        return 8000, f"gunicorn {target} --bind 0.0.0.0:8000"

    if has_django:
        return 8000, "gunicorn wsgi:application --bind 0.0.0.0:8000"

    # Pure entrypoint-file soft signal (no framework hint): default
    # to uvicorn on app:app — matches the template choice.
    return 8000, "uvicorn app:app --host 0.0.0.0 --port 8000"


def _node_entrypoint(repo_root: Path, signals: _DetectedSignals) -> Tuple[int, str]:
    start_script = _package_json_start_script(repo_root)
    if start_script:
        return 3000, start_script
    soft_values = {sig["value"] for sig in signals.node_soft}
    for candidate in NODE_SOFT_ENTRYPOINTS:
        if candidate in soft_values:
            return 3000, f"node {candidate}"
    return 3000, "npm start"


def _existing_build_files_present(repo_root: Path) -> List[str]:
    """Return the list of existing-build files at repo root, sorted."""
    found = []
    for name in EXISTING_BUILD_FILES:
        if (repo_root / name).is_file():
            found.append(name)
    return found


def _detect_signals(repo_root: Path) -> _DetectedSignals:
    python_hard = _hard_signals(repo_root, PYTHON_HARD_SIGNAL_FILES)
    node_hard = _hard_signals(repo_root, NODE_HARD_SIGNAL_FILES)
    go_hard = _hard_signals(repo_root, GO_HARD_SIGNAL_FILES)

    python_soft: List[Dict[str, str]] = []
    if python_hard:
        python_soft.extend(_python_entrypoint_soft_signals(repo_root))
        python_soft.extend(_python_framework_soft_signals(repo_root))

    node_soft: List[Dict[str, str]] = []
    if node_hard:
        node_soft.extend(_node_entrypoint_soft_signals(repo_root))
        node_soft.extend(_node_framework_soft_signals(repo_root))

    go_soft: List[Dict[str, str]] = []
    if go_hard:
        go_soft.extend(_go_entrypoint_soft_signals(repo_root))

    return _DetectedSignals(
        python_hard=python_hard,
        node_hard=node_hard,
        go_hard=go_hard,
        python_soft=python_soft,
        node_soft=node_soft,
        go_soft=go_soft,
    )


def _hard_signals(repo_root: Path, filenames: Tuple[str, ...]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for name in filenames:
        if (repo_root / name).is_file():
            out.append({"kind": _kind_for_filename(name), "path": name})
    return out


def _kind_for_filename(name: str) -> str:
    # Manifest-shaped files: pyproject.toml, package.json, setup.py
    # (executable manifest), Pipfile, etc. The schema accepts file/dir/
    # script/manifest; we keep this loose since it's mostly for human
    # readers.
    manifest_like = {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "Pipfile",
        "package.json",
        "go.mod",
    }
    return "manifest" if name in manifest_like else "file"


def _python_entrypoint_soft_signals(repo_root: Path) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for name in PYTHON_SOFT_ENTRYPOINTS:
        if (repo_root / name).is_file():
            out.append({"kind": "entrypoint_filename", "value": name})
    return out


def _python_framework_soft_signals(repo_root: Path) -> List[Dict[str, str]]:
    """Parse requirements.txt and pyproject.toml lightly for framework hints."""
    seen: List[str] = []
    requirements_path = repo_root / "requirements.txt"
    if requirements_path.is_file():
        seen.extend(_parse_requirements_txt_for_hints(requirements_path))

    pyproject_path = repo_root / "pyproject.toml"
    if pyproject_path.is_file():
        seen.extend(_parse_pyproject_toml_for_hints(pyproject_path))

    deduped: List[Dict[str, str]] = []
    seen_set: set[str] = set()
    for hint in seen:
        if hint not in seen_set:
            deduped.append({"kind": "framework_dependency", "value": hint})
            seen_set.add(hint)
    return deduped


def _node_entrypoint_soft_signals(repo_root: Path) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for name in NODE_SOFT_ENTRYPOINTS:
        if (repo_root / name).is_file():
            out.append({"kind": "entrypoint_filename", "value": name})

    start_script = _package_json_start_script(repo_root)
    if start_script:
        out.append({"kind": "package_json_scripts_start", "value": start_script})
    return out


def _node_framework_soft_signals(repo_root: Path) -> List[Dict[str, str]]:
    package_json = repo_root / "package.json"
    if not package_json.is_file():
        return []
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    deps = {}
    for key in ("dependencies", "devDependencies"):
        value = data.get(key)
        if isinstance(value, dict):
            deps.update({str(k).lower(): str(v) for k, v in value.items()})

    out: List[Dict[str, str]] = []
    for hint in NODE_FRAMEWORK_HINTS:
        if hint in deps:
            out.append({"kind": "framework_dependency", "value": hint})
    return out


def _package_json_start_script(repo_root: Path) -> Optional[str]:
    package_json = repo_root / "package.json"
    if not package_json.is_file():
        return None
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    scripts = data.get("scripts")
    if isinstance(scripts, dict):
        start = scripts.get("start")
        if isinstance(start, str) and start.strip():
            return start.strip()
    main = data.get("main")
    if isinstance(main, str) and main.strip():
        return f"node {main.strip()}"
    return None


def _go_entrypoint_soft_signals(repo_root: Path) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if (repo_root / "main.go").is_file():
        out.append({"kind": "entrypoint_filename", "value": "main.go"})
    cmd_dir = repo_root / "cmd"
    if cmd_dir.is_dir():
        for main_file in sorted(cmd_dir.glob("*/main.go")):
            out.append({"kind": "entrypoint_filename", "value": str(main_file.relative_to(repo_root))})
            break
    module_name = _go_module_name(repo_root)
    if module_name:
        out.append({"kind": "module_name", "value": module_name})
    return out


def _go_module_name(repo_root: Path) -> Optional[str]:
    go_mod = repo_root / "go.mod"
    if not go_mod.is_file():
        return None
    try:
        for line in go_mod.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("module "):
                return line.split(None, 1)[1].strip()
    except OSError:
        return None
    return None


_REQ_LINE_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)")


def _parse_requirements_txt_for_hints(path: Path) -> List[str]:
    hints: List[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return hints
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        match = _REQ_LINE_RE.match(line)
        if not match:
            continue
        name = match.group(1).lower()
        if name in PYTHON_FRAMEWORK_HINTS:
            hints.append(name)
    return hints


def _parse_pyproject_toml_for_hints(path: Path) -> List[str]:
    """Lightweight hint scan: looks for framework names in known
    dependency table positions. Avoids a strict TOML parser dep so the
    detector has zero non-stdlib dependencies in this slice.
    """
    hints: List[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return hints
    lower = text.lower()
    for hint in PYTHON_FRAMEWORK_HINTS:
        # Look for the hint as a quoted dependency entry (PEP 621
        # `dependencies = [...]`, Poetry `[tool.poetry.dependencies]`,
        # etc.). The `\b` boundary keeps matches honest.
        if re.search(rf'(?<![A-Za-z0-9_]){re.escape(hint)}(?![A-Za-z0-9_-])', lower):
            hints.append(hint)
    # Preserve order of PYTHON_FRAMEWORK_HINTS, dedup.
    deduped: List[str] = []
    for hint in PYTHON_FRAMEWORK_HINTS:
        if hint in hints and hint not in deduped:
            deduped.append(hint)
    return deduped


def _polyglot_risk_flags(signals: _DetectedSignals) -> List[str]:
    parts: List[str] = []
    if signals.python_hard:
        parts.append(f"python_hard_signals: {[s['path'] for s in signals.python_hard]}")
    if signals.node_hard:
        parts.append(f"node_hard_signals: {[s['path'] for s in signals.node_hard]}")
    if signals.go_hard:
        parts.append(f"go_hard_signals: {[s['path'] for s in signals.go_hard]}")
    return parts


def render_dockerfile(
    artifact: Dict,
    *,
    output_root: Optional[Path] = None,
    service: Optional[str] = None,
) -> Dict:
    """Render the Dockerfile template selected by `artifact`.

    Rendering is deterministic: by default the Dockerfile lands under
    `<repo_root>/.amof/generated-builds/<service-or-app>/Dockerfile`.
    The artifact remains `status: proposed`; rendering is not a build
    proof.
    """
    if artifact.get("status") == "refused":
        raise ValueError("Cannot render Dockerfile for refused generated-build artifact")
    repo_root = Path(artifact.get("source_repo", {}).get("host_path", ""))
    if not repo_root:
        raise ValueError("artifact.source_repo.host_path is required")
    family = artifact.get("runtime_family")
    template_id = artifact.get("dockerfile_template", {}).get("id")
    expected_template = {
        FAMILY_PYTHON: PYTHON_TEMPLATE_ID,
        FAMILY_NODE: NODE_TEMPLATE_ID,
        FAMILY_GO: GO_TEMPLATE_ID,
    }.get(family)
    if template_id != expected_template:
        raise ValueError(f"Unsupported template for family {family!r}: {template_id!r}")

    target_dir = output_root or repo_root / ".amof" / "generated-builds" / (service or artifact.get("service") or "app")
    target_dir.mkdir(parents=True, exist_ok=True)
    dockerfile_path = target_dir / "Dockerfile"
    dockerfile_path.write_text(_render_template(artifact), encoding="utf-8")

    rendered = dict(artifact)
    rendered_template = dict(rendered.get("dockerfile_template", {}))
    rendered_template["rendered_path"] = str(dockerfile_path)
    rendered["dockerfile_template"] = rendered_template
    rendered["risk_flags"] = [
        flag for flag in list(rendered.get("risk_flags") or []) if flag != "template_not_yet_rendered"
    ]
    return rendered


def run_build_proof(
    artifact: Dict,
    *,
    image: str,
    substrate: str = "host-docker",
) -> Dict:
    """Run local docker build and advance `artifact` to build_proven.

    This function only proves the build. It never starts the image and
    never emits runtime_proof.
    """
    if artifact.get("status") == "refused":
        raise ValueError("Cannot build-proof a refused generated-build artifact")
    rendered_path = artifact.get("dockerfile_template", {}).get("rendered_path")
    if not rendered_path:
        raise ValueError("artifact.dockerfile_template.rendered_path is required")
    repo_root = Path(artifact.get("source_repo", {}).get("host_path", ""))
    if not repo_root:
        raise ValueError("artifact.source_repo.host_path is required")

    subprocess.run(
        ["docker", "build", "-f", rendered_path, "-t", image, str(repo_root)],
        check=True,
    )
    digest = _docker_image_digest(image)

    proven = dict(artifact)
    proven["status"] = "build_proven"
    proven["image_outputs"] = [{"push_image": image, "pull_image": image}]
    proven["build_proof"] = {
        "docker_build_exit_code": 0,
        "image_digest": digest,
        "built_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "built_in_substrate": substrate,
    }
    return proven


def run_runtime_proof(
    artifact: Dict,
    *,
    image: str,
    timeout_seconds: int = 20,
    substrate: str = "local-docker-run",
) -> Dict:
    """Run local container liveness proof and advance to runtime_proven.

    Runtime proof is deliberately narrow in UP9-3: start the already
    built image with Docker, publish the artifact's inferred port to a
    loopback-only ephemeral host port, and poll for `port_open`.

    On failure this function returns the input artifact unchanged. That
    preserves truth: a `build_proven` artifact remains merely
    `build_proven`; a `proposed` artifact remains `proposed`. No
    `runtime_proof` key is emitted unless bounded liveness is actually
    observed.
    """
    if artifact.get("status") == "refused":
        raise ValueError("Cannot runtime-proof a refused generated-build artifact")
    if artifact.get("status") != "build_proven":
        return dict(artifact)

    container_port = int(artifact.get("inferred_port") or 0)
    if container_port <= 0:
        return dict(artifact)

    host_port = _free_loopback_port()
    container_id = ""
    try:
        container_id = subprocess.check_output(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "-p",
                f"127.0.0.1:{host_port}:{container_port}",
                image,
            ],
            text=True,
        ).strip()
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if _port_open("127.0.0.1", host_port):
                proven = dict(artifact)
                proven["status"] = "runtime_proven"
                proven["runtime_proof"] = {
                    "liveness_signal": "port_open",
                    "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "checked_via": substrate,
                }
                return proven
            time.sleep(0.25)
        return dict(artifact)
    finally:
        if container_id:
            subprocess.run(
                ["docker", "rm", "-f", container_id],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )


def _docker_image_digest(image: str) -> str:
    raw = subprocess.check_output(["docker", "image", "inspect", image], text=True)
    data = json.loads(raw)
    image_id = str(data[0].get("Id") or "")
    if image_id.startswith("sha256:"):
        return image_id
    raise RuntimeError(f"docker image inspect did not return a sha256 image id for {image}")


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


def _render_template(artifact: Dict) -> str:
    family = artifact.get("runtime_family")
    if family == FAMILY_PYTHON:
        return _render_python_template(artifact)
    if family == FAMILY_NODE:
        return _render_node_template(artifact)
    if family == FAMILY_GO:
        return _render_go_template(artifact)
    raise ValueError(f"No template renderer implemented for runtime family {family!r}")


def _json_array_command(command: str) -> str:
    return json.dumps(["sh", "-c", command])


def _render_python_template(artifact: Dict) -> str:
    entrypoint = artifact.get("entrypoint") or "uvicorn app:app --host 0.0.0.0 --port 8000"
    port = artifact.get("inferred_port") or 8000
    return f"""FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt* ./
RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi
COPY . .
EXPOSE {port}
CMD {_json_array_command(entrypoint)}
"""


def _render_node_template(artifact: Dict) -> str:
    entrypoint = artifact.get("entrypoint") or "npm start"
    port = artifact.get("inferred_port") or 3000
    return f"""FROM node:20-alpine
WORKDIR /app
COPY package*.json ./
RUN if [ -f package-lock.json ]; then npm ci --omit=dev; else npm install --omit=dev; fi
COPY . .
EXPOSE {port}
CMD {_json_array_command(entrypoint)}
"""


def _render_go_template(artifact: Dict) -> str:
    port = artifact.get("inferred_port") or 8080
    return f"""FROM golang:1.23-alpine AS builder
WORKDIR /src
COPY go.mod go.sum* ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -o /out/server .

FROM gcr.io/distroless/static
COPY --from=builder /out/server /server
EXPOSE {port}
ENTRYPOINT ["/server"]
"""


def _refused(repo_root: Path, *, refusal_reason: str, extra_risk: Optional[List[str]] = None) -> Dict:
    """Build a `status: refused` artifact that conforms to the schema."""
    return {
        "build_contract_kind": "generated",
        "source_repo": {"host_path": str(repo_root)},
        "runtime_family": FAMILY_UNKNOWN,
        "confidence": "refused",
        "signals": {"hard": [], "soft": []},
        "dockerfile_template": {"id": "n/a", "version": "n/a"},
        "image_outputs": [
            {
                "push_image": "(refused; no image would be produced)",
                "pull_image": "(refused; no image would be produced)",
            }
        ],
        "risk_flags": list(extra_risk or []),
        "status": "refused",
        "refusal_reason": refusal_reason,
    }

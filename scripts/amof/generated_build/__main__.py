"""Tiny CLI for generated-build detector/render/build-proof flows.

Usage:
    python -m amof.generated_build detect <repo_path>
    python -m amof.generated_build render <repo_path>
    python -m amof.generated_build build-proof <repo_path> --image <image>
    python -m amof.generated_build runtime-proof <repo_path> --image <image>
    python -m amof.generated_build list
    python -m amof.generated_build show <repo_path> [--service <name>]
    python -m amof.generated_build admission-preview <repo_path> [--service <name>]

`detect` is read-only. `render` writes the generated Dockerfile under
`.amof/generated-builds/...` and leaves status at `proposed`.
`build-proof` renders if needed, runs `docker build`, and advances to
`build_proven`. `runtime-proof` starts the built image locally and
advances to `runtime_proven` only when bounded liveness is observed.
None of these commands deploy.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .detector import detect_runtime, render_dockerfile, run_build_proof, run_runtime_proof
from .store import artifact_path_for, load_artifact, load_index, persist_artifact
from .admission import evaluate_admission


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m amof.generated_build",
        description="Generated-build first-wave detector/render/build-proof CLI.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    detect_parser = sub.add_parser("detect", help="Detect runtime family for a repo path.")
    detect_parser.add_argument("repo_path", help="Filesystem path to the repository root to inspect.")
    detect_parser.add_argument("--output", help="Optional path to write the artifact JSON.")

    render_parser = sub.add_parser("render", help="Render the selected generated Dockerfile template.")
    render_parser.add_argument("repo_path", help="Filesystem path to the repository root to inspect.")
    render_parser.add_argument("--output", help="Optional path to write the artifact JSON.")
    render_parser.add_argument("--service", help="Optional service name used in the deterministic output path.")

    build_parser = sub.add_parser("build-proof", help="Render and run docker build proof.")
    build_parser.add_argument("repo_path", help="Filesystem path to the repository root to inspect.")
    build_parser.add_argument("--image", required=True, help="Image reference to tag with docker build.")
    build_parser.add_argument("--output", help="Optional path to write the artifact JSON.")
    build_parser.add_argument("--service", help="Optional service name used in the deterministic output path.")

    runtime_parser = sub.add_parser("runtime-proof", help="Run local Docker liveness proof; does not deploy.")
    runtime_parser.add_argument("repo_path", help="Filesystem path to the repository root to inspect.")
    runtime_parser.add_argument("--image", required=True, help="Image reference that was already build-proven.")
    runtime_parser.add_argument("--output", help="Optional path to write the artifact JSON.")
    runtime_parser.add_argument("--service", help="Optional service name used in the deterministic output path.")
    runtime_parser.add_argument("--timeout", type=int, default=20, help="Seconds to wait for liveness (default: 20).")

    list_parser = sub.add_parser("list", help="List locally persisted generated-build artifacts.")
    list_parser.add_argument("--output", help="Optional path to write index JSON.")

    show_parser = sub.add_parser("show", help="Show one locally persisted generated-build artifact.")
    show_parser.add_argument("repo_path", help="Repository root path used when the artifact was persisted.")
    show_parser.add_argument("--service", help="Optional service name (defaults to root).")
    show_parser.add_argument("--output", help="Optional path to write artifact JSON.")

    admission_parser = sub.add_parser("admission-preview", help="Return the public generated-build admission contract.")
    admission_parser.add_argument("repo_path", help="Repository root path used when the artifact was persisted.")
    admission_parser.add_argument("--service", help="Optional service name (defaults to root).")
    admission_parser.add_argument("--output", help="Optional path to write contract result JSON.")

    args = parser.parse_args(argv)

    if args.command == "detect":
        artifact = detect_runtime(Path(args.repo_path))
        persist_artifact(artifact)
        _emit_artifact(artifact, args.output)
        if artifact.get("status") == "refused":
            return 2
        return 0
    if args.command == "render":
        artifact = render_dockerfile(
            detect_runtime(Path(args.repo_path)),
            service=getattr(args, "service", None),
        )
        persist_artifact(artifact, service=getattr(args, "service", None))
        _emit_artifact(artifact, args.output)
        if artifact.get("status") == "refused":
            return 2
        return 0
    if args.command == "build-proof":
        artifact = render_dockerfile(
            detect_runtime(Path(args.repo_path)),
            service=getattr(args, "service", None),
        )
        artifact = run_build_proof(artifact, image=args.image)
        persist_artifact(artifact, service=getattr(args, "service", None))
        _emit_artifact(artifact, args.output)
        return 0
    if args.command == "runtime-proof":
        artifact = render_dockerfile(
            detect_runtime(Path(args.repo_path)),
            service=getattr(args, "service", None),
        )
        artifact = run_build_proof(artifact, image=args.image)
        before_status = artifact.get("status")
        artifact = run_runtime_proof(
            artifact,
            image=args.image,
            timeout_seconds=getattr(args, "timeout", 20),
        )
        persist_artifact(artifact, service=getattr(args, "service", None))
        _emit_artifact(artifact, args.output)
        if before_status == "build_proven" and artifact.get("status") != "runtime_proven":
            return 3
        return 0
    if args.command == "list":
        _emit_artifact(load_index(), args.output)
        return 0
    if args.command == "show":
        artifact = load_artifact(Path(args.repo_path), service=getattr(args, "service", None))
        _emit_artifact(artifact, args.output)
        return 0
    if args.command == "admission-preview":
        artifact_path = artifact_path_for(Path(args.repo_path), service=getattr(args, "service", None))
        artifact = load_artifact(Path(args.repo_path), service=getattr(args, "service", None))
        admission_result = evaluate_admission(artifact, artifact_path=artifact_path)
        _emit_artifact(admission_result, args.output)
        return 2 if admission_result.get("admission_status") == "refused" else 0

    parser.error(f"Unknown command: {args.command}")
    return 1  # unreachable


def _emit_artifact(artifact: dict, output: str | None) -> None:
    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(json.dumps(artifact, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        return
    json.dump(artifact, sys.stdout, indent=2, sort_keys=False)
    sys.stdout.write("\n")


if __name__ == "__main__":  # pragma: no cover - exercised via `python -m`
    raise SystemExit(_main())

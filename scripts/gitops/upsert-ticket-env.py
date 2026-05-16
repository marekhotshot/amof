#!/usr/bin/env python3
"""Create or update one ticket GitOps environment file deterministically.

Examples:
  python3 scripts/gitops/upsert-ticket-env.py \
    --ticket-id AMOF-123 \
    --branch feat/AMOF-123 \
    --commit-sha 787568272b3b4e8d \
    --owner-id operator@amof.dev \
    --owner-slug operator-amof-dev \
    --host-mode local \
    --target-revision reconstruct-dev-runtime-argocd

  python3 scripts/gitops/upsert-ticket-env.py \
    --ticket-id AMOF-123 \
    --branch feat/AMOF-123 \
    --commit-sha 787568272b3b4e8d \
    --owner-id operator@amof.dev \
    --owner-slug operator-amof-dev \
    --host-mode local \
    --target-revision reconstruct-dev-runtime-argocd \
    --output envs/tickets/example-ticket.yaml
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_BASE_DOMAINS = {
    "local": "local.amof.test",
    "cloud": "amof.dev",
}

DEFAULT_REGISTRY_BASES = {
    "local": "k3d-amof-registry:5000",
    "cloud": "ghcr.io/marekhotshot",
}

IMAGE_NAMES = {
    "controlplane": "amof-controlplane",
    "dashboard": "amof-dashboard",
    "agent": "amof-agent",
    "assistant": "amof-assistant",
}


def default_repositories_for(registry_base: str) -> dict[str, str]:
    return {
        image_name: f"{registry_base}/{repository_name}"
        for image_name, repository_name in IMAGE_NAMES.items()
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create or update one TicketEnvironment file under envs/tickets/."
    )
    parser.add_argument("--ticket-id", required=True, help="Ticket identifier, e.g. AMOF-123")
    parser.add_argument("--branch", required=True, help="Git branch for the ticket environment")
    parser.add_argument("--commit-sha", required=True, help="Commit SHA/tag used for image tags")
    parser.add_argument("--owner-id", required=True, help="Raw owner identity, e.g. operator@amof.dev")
    parser.add_argument("--owner-slug", required=True, help="Label-safe owner slug")
    parser.add_argument("--owner-type", default="team", help="Owner type stored in the contract")
    parser.add_argument(
        "--host-mode",
        choices=("local", "cloud"),
        required=True,
        help="Hostname mode for the generated environment hostname",
    )
    parser.add_argument(
        "--base-domain",
        help="Hostname suffix. Defaults to local.amof.test or platform.amof.dev based on host mode.",
    )
    parser.add_argument(
        "--project",
        default="default",
        help="Argo project name stored under gitops.project",
    )
    parser.add_argument(
        "--repo-url",
        default="https://github.com/marekhotshot/amof.git",
        help="GitOps repository URL stored under gitops.repoURL",
    )
    parser.add_argument(
        "--target-revision",
        default="main",
        help="GitOps target revision stored under gitops.targetRevision",
    )
    parser.add_argument(
        "--chart-path",
        default="infrastructure/helm/amof-stack",
        help="GitOps chart path stored under gitops.chartPath",
    )
    parser.add_argument(
        "--registry-base",
        help="Image registry base used for generated repositories. Defaults to local k3d registry for local mode and GHCR for cloud mode.",
    )
    parser.add_argument(
        "--output",
        help="Output path relative to the repo root. Defaults to envs/tickets/<ticket-slug>.yaml",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the generated YAML instead of writing the file",
    )
    parser.add_argument(
        "--summary-json",
        action="store_true",
        help="Print a machine-readable summary after write/update",
    )
    return parser.parse_args()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    if not slug:
        raise SystemExit("ticket id must contain at least one alphanumeric character")
    return slug


def yaml_quote(value: str) -> str:
    return json.dumps(value)


def resolve_output_path(repo_root: Path, output: str | None, ticket_slug: str) -> Path:
    relative = Path(output) if output else Path("envs/tickets") / f"{ticket_slug}.yaml"
    resolved = (repo_root / relative).resolve()
    tickets_root = (repo_root / "envs/tickets").resolve()
    if tickets_root not in (resolved, *resolved.parents):
        raise SystemExit("output must stay under envs/tickets/")
    return resolved


def render_yaml(args: argparse.Namespace, ticket_slug: str, hostname: str) -> str:
    release_name = f"amof-ticket-{ticket_slug}"
    repositories = default_repositories_for(args.registry_base)
    lines = [
        "schemaVersion: v1alpha1",
        "kind: TicketEnvironment",
        "",
        "ticket:",
        f"  id: {yaml_quote(args.ticket_id)}",
        f"  branch: {yaml_quote(args.branch)}",
        f"  commitSha: {yaml_quote(args.commit_sha)}",
        "",
        "environment:",
        f"  namespace: {yaml_quote(release_name)}",
        f"  appName: {yaml_quote(release_name)}",
        f"  releaseName: {yaml_quote(release_name)}",
        f"  hostname: {yaml_quote(hostname)}",
        "",
        "owner:",
        f"  id: {yaml_quote(args.owner_id)}",
        f"  slug: {yaml_quote(args.owner_slug)}",
        f"  type: {yaml_quote(args.owner_type)}",
        "",
        "images:",
    ]
    for image_name in ("controlplane", "dashboard", "agent", "assistant"):
        lines.extend(
            [
                f"  {image_name}:",
                f"    repository: {yaml_quote(repositories[image_name])}",
                f"    tag: {yaml_quote(args.commit_sha)}",
            ]
        )
    lines.extend(
        [
            "",
            "enabledModules:",
            "  controlplane: true",
            "  dashboard: true",
            "  agent: true",
            "  assistant: false",
            "",
            "lifecycle:",
            f"  ttl: {yaml_quote('168h')}",
            f"  cleanupMode: {yaml_quote('destroy_on_branch_delete')}",
            "",
            "syncPolicy:",
            f"  mode: {yaml_quote('automated')}",
            "  prune: true",
            "  selfHeal: true",
            "  createNamespace: true",
            "",
            "gitops:",
            f"  project: {yaml_quote(args.project)}",
            f"  repoURL: {yaml_quote(args.repo_url)}",
            f"  targetRevision: {yaml_quote(args.target_revision)}",
            f"  chartPath: {yaml_quote(args.chart_path)}",
            "",
        ]
    )
    return "\n".join(lines)


def build_summary(output_path: Path, repo_root: Path, ticket_slug: str, hostname: str, changed: bool) -> dict[str, object]:
    release_name = f"amof-ticket-{ticket_slug}"
    return {
        "output_path": str(output_path.relative_to(repo_root)),
        "namespace": release_name,
        "app_name": release_name,
        "release_name": release_name,
        "hostname": hostname,
        "changed": changed,
    }


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[2]
    ticket_slug = slugify(args.ticket_id)
    base_domain = args.base_domain or DEFAULT_BASE_DOMAINS[args.host_mode]
    args.registry_base = args.registry_base or DEFAULT_REGISTRY_BASES[args.host_mode]
    hostname = f"{ticket_slug}.{base_domain}"
    output_path = resolve_output_path(repo_root, args.output, ticket_slug)
    content = render_yaml(args, ticket_slug, hostname)
    previous = output_path.read_text(encoding="utf-8") if output_path.exists() else None
    changed = previous != content
    summary = build_summary(output_path, repo_root, ticket_slug, hostname, changed)

    if args.dry_run:
        if args.summary_json:
            print(json.dumps({**summary, "dry_run": True}, sort_keys=True))
        else:
            sys.stdout.write(content)
        return 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")

    if args.summary_json:
        print(json.dumps(summary, sort_keys=True))
    else:
        relative_path = output_path.relative_to(repo_root)
        if changed:
            print(f"Wrote {relative_path}")
        else:
            print(f"Unchanged {relative_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

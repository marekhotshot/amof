#!/usr/bin/env python3
"""Sync demo-microsaas through Argo CD instead of runtime kubectl.

RETIRED (clean-start slice 7). This entrypoint is preserved for
legacy recovery of the demo-microsaas lane. New ecosystems use the
universal Argo CD pattern at infrastructure/gitops/<ecosystem>/
documented in infrastructure/gitops/README.md. The canonical
example is the gmd ecosystem at infrastructure/gitops/gmd/.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from amof.api.routers.release import DEMO_MICROSAAS_IMAGE_REPOSITORY
from amof.api.services.argocd import (
    ArgoCdClient,
    ArgoCdClientError,
    ArgoCdNotConfigured,
    build_demo_microsaas_application,
    demo_microsaas_app_name,
    extract_demo_microsaas_application_status,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update and sync the bounded demo-microsaas Argo CD application."
    )
    parser.add_argument("--environment-id", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--image-tag", required=True)
    parser.add_argument("--image-digest")
    parser.add_argument("--image-repository", default=DEMO_MICROSAAS_IMAGE_REPOSITORY)
    parser.add_argument("--public-base-url")
    parser.add_argument("--host")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        client = ArgoCdClient()
    except ArgoCdNotConfigured as exc:
        sys.stderr.write(f"{exc}\n")
        return 2

    app_name = demo_microsaas_app_name(args.environment_id, client.settings)
    application = build_demo_microsaas_application(
        client.settings,
        environment_id=args.environment_id,
        namespace=args.namespace,
        release_name=args.release,
        image_repository=args.image_repository,
        image_tag=args.image_tag,
        image_digest=args.image_digest,
        public_base_url=args.public_base_url,
        host=args.host,
    )
    sys.stdout.write(
        f"[argocd] Upserting {app_name} with image {args.image_repository}:{args.image_tag}\n"
    )
    try:
        client.upsert_application(application)
        sys.stdout.write(f"[argocd] Triggering sync for {app_name}\n")
        client.sync_application(app_name)
        final_app = client.wait_for_application(app_name)
        status = extract_demo_microsaas_application_status(final_app)
    except ArgoCdClientError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1

    sys.stdout.write(json.dumps(status, indent=2))
    sys.stdout.write("\n")
    return 0 if status.get("ready") else 1


if __name__ == "__main__":
    raise SystemExit(main())

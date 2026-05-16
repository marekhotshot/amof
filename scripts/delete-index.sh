#!/usr/bin/env bash
# Delete codebase index and Merkle tree to force a fresh rebuild.
# Use after indexer changes or when hitting token limits.
# Run from platform root.
set -e
ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$ROOT"
for dir in "ecosystems/amof-platform/index" ".amof/index"; do
  if [[ -d "$dir" ]]; then
    rm -rf "$dir"/codebase-index.json "$dir"/merkle-tree.json 2>/dev/null || true
    echo "Deleted index in $dir"
  fi
done
echo "Done. Next agent run will create a fresh index."

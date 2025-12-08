# AMOF CLI SPEC

## amof sync
- Clone repos if missing
- Pull latest changes
- Checkout branches from manifest
- Validate structure
- Optional: `--repo <name>` (repeatable) to sync only selected repositories

## amof status
- Missing repos
- Dirty worktrees
- Branch mismatches
- Manifest drift
- Optional: `--repo <name>` (repeatable) to check only selected repositories

## amof context <service>
- Apply include/exclude rules
- Generate index.json
- Generate summary.md
- Store under context/<service>

## amof add-repo <name> <url>
- Append a repository entry to amof.yaml
- Optional overrides: --branch, --path, repeated --include/--exclude globs
- --replace updates an existing entry if the name already exists
- --sync clones/fetches only the new/updated repo after writing the manifest

## amof doctor
- Validate environment
- Check git
- Verify paths

## amof upgrade
- Update manifest format

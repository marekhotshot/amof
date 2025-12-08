# AMOF – Agentic Multirepo Operating Framework

AMOF defines a reproducible structure for human–AI collaboration across multiple repositories.
It ensures that AI agents can read, understand, modify, and extend codebases safely and consistently.

## Using the amof CLI (v0.1)

The CLI lives in `scripts/amof.py` and uses only the Python standard library. Run commands from the
repository root:

```bash
python scripts/amof.py sync [--repo name --repo other]
python scripts/amof.py status [--repo name]
python scripts/amof.py add-repo <name> <git-url> [--branch develop --path repos/<name> --include src/]
python scripts/amof.py context <service-name>
```

- `sync` clones or updates all repositories listed in `amof.yaml` and checks out the configured
  branches. Use `--repo` (repeatable) to target a subset.
- `status` reports whether each repository exists locally, is on the expected branch, and whether the
  working tree is dirty. Use `--repo` to filter output to selected entries.
- `add-repo <name> <git-url>` appends (or replaces with `--replace`) a repository entry in
  `amof.yaml`, with optional `--branch`, `--path`, repeated `--include`/`--exclude`, and `--sync` to
  immediately clone/fetch just that repo.
- `context <service-name>` builds a lightweight index and summary under `context/<service-name>/`
  using the include/exclude patterns from the manifest.

### Adding repositories on the fly

To grow an existing multi-repo workspace without hand-editing YAML:

1. Run `python scripts/amof.py add-repo <name> <git-url> [--branch BRANCH --path PATH --include ... --exclude ... --replace --sync]`.
2. The manifest is rewritten with the new entry (or replaced if `--replace` is set).
3. If `--sync` is provided, the tool clones/fetches only the new repo; otherwise rerun `python
   scripts/amof.py sync` later to bring it into the workspace.

## Cursor IDE + agent workflow

### Chat-driven install flow

This flow assumes the user updates `amof.yaml` first, then either runs the install themselves or asks
an agent to do it:

1. Confirm with the user that `amof.yaml` already lists the repositories to manage (name, Git URL,
   branch, path, include/exclude patterns). If anything is missing, request the details so the user
   can update the manifest before proceeding.
2. Run `python scripts/amof.py sync` to clone/fetch, checkout manifest branches, and pull the latest
   changes for every entry. Surface any git errors back to the user.
3. Run `python scripts/amof.py status` to confirm each repo exists, is on the expected branch, and is
   clean.
4. For the service the user wants to work on, run `python scripts/amof.py context <service>` and pass
   `context/<service>/summary.md` and `context/<service>/index.json` into the chat.
5. If the user prefers to run commands themselves, share the sequence above; otherwise execute the
   steps and report results.

### Provider tips

Use tested providers like OpenAI (GPT-5.1), Codex-HI, or Gemini 3 Pro; all work with the text-based
context bundle above. If a provider supports file attachments, attach the context directory for
richer responses.

## Enterprise system design

The full enterprise architecture—including orchestrator, subagents, sandboxing, memory layer, and
UI/API surfaces—is documented in `AMOF_SYSTEM_DESIGN.md`. Review it for how tasks move from chat to
planner plans, sandboxed subagents, structured reports, and PR generation with SpacetimeDB-backed
memory.

## Merkle integrity and Cursor compatibility

AMOF relies on git’s native Merkle-DAG object model for integrity. Sandbox worktrees share the same
git object database, so Cursor’s Merkle-based change detection aligns naturally—no extra checksum
layer is required for v0.1. If you need cache-friendly snapshots later, add hashed manifests on top
of the git state without changing the workflow above.

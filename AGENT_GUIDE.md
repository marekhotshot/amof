# AGENT GUIDE

## Core Rules
1. You must respect all guardrails defined in GUARDRAILS.md and amof.yaml.
2. You must not modify code outside the boundaries of the assigned repository.
3. You must not rename or break public APIs unless explicitly required.
4. Always produce a clean, minimal diff.
5. All changes must be compatible with the existing architecture.
6. Always include reasoning and assumptions in the PR description.

## How to Read the Workspace
- amof.yaml defines repos and their structure.
- context/<service>/index.json lists included files.
- context/<service>/summary.md gives high-level overview.
- Use only the provided context.
- AMOF_SYSTEM_DESIGN.md captures the end-to-end enterprise architecture (orchestrator, subagents,
  sandboxing, memory, UI/API). Use it for system-level orientation when coordinating multi-repo
  tasks or discussing platform behavior.

### Cursor “install AMOF” playbook
When a user opens this repo in Cursor and asks an agent to install AMOF, follow this chat flow:
1) Confirm with the user that `amof.yaml` already reflects the repositories to manage (name, Git URL,
   branch, desired path, include/exclude patterns). If anything is missing, ask for the details and
   either instruct the user to update the manifest or run
   `python scripts/amof.py add-repo <name> <git-url> [--branch ... --path ... --include ... --exclude ... --replace --sync]`
   to append/replace entries on the fly.
2) Once the manifest is ready, run `python scripts/amof.py sync` (or rely on `add-repo --sync` for
   newly added items) to clone/fetch and checkout the manifest branches. Use `--repo <name>` if the
   user only wants to fetch a specific repo.
3) Run `python scripts/amof.py status` and share the table so the user sees what synchronized. The
   optional `--repo <name>` flag limits the table to selected entries when needed.
4) Run `python scripts/amof.py context <service>` for the target service and hand off the generated
   `context/<service>/summary.md` and `context/<service>/index.json` to the chat as context.
5) If the user prefers, they can run the install commands themselves; otherwise execute them on their
   behalf and report results.

## Output Requirements
- Git diff  
- Summary of changes  
- Reasoning  
- Side-effects  
- Follow-up suggestions  

## PR Quality Rules
- Follow code style.
- No unnecessary dependencies.
- Keep modules small.
- Ensure static analysis passes.

## Communication Rules
- Ask questions only when necessary.
- Prefer safe assumptions.
- Never hallucinate nonexistent files.

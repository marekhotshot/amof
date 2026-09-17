# Public CLI

First-run commands for AMOF 3.5. Maintainer mutation commands are not
quickstart.

```bash
pipx install "git+https://github.com/marekhotshot/amof.git@v3.5.0"
amof --version
amof check
amof doctor
amof demo
amof demo migration --non-interactive
amof proof list
amof proof show AMOF-15MIN-RUNTIME-AUTHORITY-DEMO-001
```

From a source checkout, `python3 scripts/amof.py` is the same CLI.

## Runtime Authority first-run

| Command | Role |
|---|---|
| `amof demo [scenario]` | One scenario to a verified receipt |
| `amof proof list` | Published public proofs |
| `amof proof show <ticket-or-sha>` | Bind a promoted SHA |
| `amof scope …` | Write-Scope and Kubernetes capability authority |
| `amof check` | Public prerequisites |
| `amof doctor` | Bootstrap readiness |
| `amof init --adopt .` | Adopt a Git repo without polluting it |
| `amof setup provider --list` | Provider profile references, not raw keys |

## Flags on `amof demo`

- `--non-interactive` — no prompts; default scenario is migration
- `--live` — Kubernetes only; disposable local cluster
- `--show-receipt` — print receipt JSON
- `--json` — machine-readable result

## Not on this page

Hidden or maintainer commands such as `promote-main` are not public
publishing commands. Full taxonomy lives in the repository:
[docs/operations/public-surface-taxonomy.md](https://github.com/marekhotshot/amof/blob/v3.5.0/docs/operations/public-surface-taxonomy.md).

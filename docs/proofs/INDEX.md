# Public promotion proofs

These files let a stranger bind a promoted AMOF revision to public-safe
verification evidence. They project governed `promote-main` identity.
They do not contain app-data, kubeconfigs, or secrets.

```bash
python3 scripts/amof.py proof list
python3 scripts/amof.py proof show AMOF-15MIN-RUNTIME-AUTHORITY-DEMO-001
python3 scripts/amof.py demo migration --non-interactive
```

`GitOps-SHA` is optional env-commit identity (`envs/tickets/…`). Current
public promotions in this campaign are code-only, so it is `null`.

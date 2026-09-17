# AMOF Runtime Authority 3.5

This site is a curated projection of public AMOF on GitHub. It is not a
mirror of the whole `docs/` tree. Git remains source of truth.

Current release: **v3.5.0**.

## Start

1. [Install](install.md)
2. [Try it in practice](in-practice.md)
3. [Understand Runtime Authority](runtime-authority.md)
4. [Verify a public proof](evidence.md)

```bash
git clone https://github.com/marekhotshot/amof.git
cd amof
python3 scripts/amof.py demo migration --non-interactive
python3 scripts/amof.py proof show AMOF-15MIN-RUNTIME-AUTHORITY-DEMO-001
```

## Contents

| Page | What it is |
|---|---|
| [Runtime Authority](runtime-authority.md) | Current product lifecycle and capability kinds |
| [Architecture](architecture.md) | Public/private boundary |
| [In practice](in-practice.md) | Executable scenarios |
| [Evidence](evidence.md) | Public promotion proofs |
| [CLI](cli.md) | Public first-run commands |
| [Install](install.md) | Supported install paths |
| [Releases](releases.md) | v3.5.0 notes |

Engineering process, ADRs, historical roadmaps, Remote IAL narratives, and
the unpublished Trust Model draft are **not** on this site. The full
repository map is [`docs/INDEX.md`](https://github.com/marekhotshot/amof/blob/v3.5.0/docs/INDEX.md).

## Non-claims

- Not Predator, Workforce, or an operator console in OSS
- Not a Kubernetes platform or RBAC replacement
- Not a bank / hospital / insurer / IAM / migration product
- Not a published Trust Model
- Not autonomous approval or auto-promotion
- Not “AI does more” — AI gets explicit bounded authority and must prove what happened

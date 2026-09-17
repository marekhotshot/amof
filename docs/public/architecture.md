# Architecture

Public AMOF is the installable governed runtime. Private AMOF is
orchestration intelligence. This page does not add a second architecture.

```text
Workers propose.
Runtime Authority binds, enforces, and records.
Operators approve and promote.
```

Public materials may explain installability, receipts, promotion semantics,
and capability kinds. They must not teach private routing, scoring, or
topology.

Capability kinds on public `v3.5.0`:

```text
Authority
 ├── git.write            Write-Scope Authority
 ├── kubernetes.read      Kubernetes sibling
 │   kubernetes.mutate
 └── reference.action     local reference-system sibling (`amof demo`)
```

Product-site diagrams on [amof.dev](https://amof.dev/#architecture) are
HTML-native. The public repo does not ship a separate architecture-diagram
pack.

Canonical pages in Git (not mirrored here):

- [Runtime Authority](runtime-authority.md)
- [public-private-boundary.md](https://github.com/marekhotshot/amof/blob/v3.5.0/docs/architecture/public-private-boundary.md)
- [capability-authority.md](https://github.com/marekhotshot/amof/blob/v3.5.0/docs/capability-authority.md)
- [write-scope-authority.md](https://github.com/marekhotshot/amof/blob/v3.5.0/docs/write-scope-authority.md)

## Boundary Classification

Check the highest category touched by this PR:

- [ ] `PUBLIC_RUNTIME`: install/runtime/receipts/worktree/promotion/trust surface only.
- [ ] `SHARED_CONTRACT`: schemas, interfaces, result shapes, or vocabulary only.
- [ ] `PRIVATE_INTELLIGENCE`: strategic heuristics, routing, scoring, eval, or coordination touched.
- [ ] `RESTRICTED_STRATEGIC_LOGIC`: sensitive planning, orchestration, routing, scoring, evaluation, or autonomy logic touched.

## Public Safety Checklist

- [ ] This PR does not publish private routing strategy, fallback behavior, model/provider priority, or threshold policy.
- [ ] This PR does not publish planning prompts, decomposition logic, task-splitting strategy, or autonomous coordination heuristics.
- [ ] This PR does not publish eval goldens, scoring rubrics, benchmark tasks, expected outputs, or noise-rejection datasets.
- [ ] This PR does not publish internal topology, live provider paths, secrets, kubeconfigs, private keys, or organization-specific runbooks.
- [ ] Any public contract added here is an interface/result shape, not the private strategy that produces the result.

If any item is false, stop and link the private-boundary review before merge.

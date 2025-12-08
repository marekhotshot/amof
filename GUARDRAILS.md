# GUARDRAILS

## Do Not Touch
- Any .github/ folder
- deploy/
- k8s/prod/
- GitHub workflows
- Production configuration
- Secrets or credentials

## Forbidden Actions
- Changing public API interfaces
- Renaming modules without instruction
- Generating large subsystems without approval
- Moving files across repo boundaries
- Editing amof.yaml unless requested

## Sensitive Actions
- Dependency upgrades
- Introducing new frameworks
- Large refactors
- Rewriting CI/CD logic

## Allowed Actions
- Implement features in assigned repo
- Bugfixes
- Tests
- Documentation
- Config improvements

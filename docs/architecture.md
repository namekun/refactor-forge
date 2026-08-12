# Refactor Forge Architecture

## MVP boundary

The core is deliberately model-agnostic. A transformation definition contains deterministic steps and verification commands. AI may author or refine definitions later, but it is not trusted as the execution engine.

## Safety properties

- `plan` copies the target into an isolated temporary directory.
- `watch` fingerprints repository content and only evaluates after a change.
- monitor-only watch mode writes a validated patch report without mutating the target.
- automatic watch application is explicit opt-in and is blocked by tracked working-tree changes.
- external command steps require `--allow-command`.
- commands are argument arrays and never run through a shell.
- `apply` requires a Git repository and rejects dirty working trees by default.
- verification failure makes the run fail.
- the core does not create branches, commits, pushes, or pull requests.

## Extension points

Implement `refactor_forge.sdk.Transformation` for an in-process engine, or use a command step to invoke OpenRewrite, ast-grep, Codemod, or an internal tool.

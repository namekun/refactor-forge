# Refactor Forge Architecture

## MVP boundary

The core is deliberately model-agnostic. A transformation definition contains deterministic steps and verification commands. AI may author or refine definitions later, but it is not trusted as the execution engine.

## Safety properties

- For a Git target with a committed `HEAD`, `plan` creates a throwaway clone with an independent object database, refs, local config, and no remotes. It does not register a source worktree. Raw tree blobs are materialized instead of checkout, so checkout hooks and smudge filters do not run.
- The default Git-plan input is committed tracked `HEAD`. Dirty/staged, untracked, ignored, and submodule content is excluded and reported. A target absent from `HEAD` falls back to an isolated copy of the current target with no Git context; non-Git and unborn targets use the same fallback. Watch opts into a current working-tree clone overlay, so its fingerprint and plan input are identical and include those files.
- Transformations and snapshots reject symlinks and verify resolved containment before reading or writing. A committed outward symlink therefore cannot write outside the sandbox.
- Command and verification subprocesses in plan use the disposable clone cwd plus a scrubbed Git environment. They can alter only the disposable clone's refs/config; source refs/config/remotes are not exposed through that Git context. Commands still require `--allow-command` and are argument arrays, never shell strings.
- Every clone exit path removes its temporary directory. A cleanup-only warning is attached to a completed report when no sandbox remains; a remaining sandbox is fatal. Watch catches per-tick exceptions and continues running.
- `watch` fingerprints the current working tree using target-relative exclusions and evaluates the same `working-tree` snapshot. Monitor-only mode writes a report without mutating the target; automatic application is explicit and blocked by tracked dirty state.
- `apply` resolves the containing Git repository, accepts nested targets, and rejects dirty working trees by default.
- Verification failure makes the run fail. The core does not create branches, commits, pushes, or pull requests.

## Extension points

Implement `refactor_forge.sdk.Transformation` for an in-process engine, or use a command step to invoke OpenRewrite, ast-grep, Codemod, or an internal tool.

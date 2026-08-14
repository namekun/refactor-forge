# Refactor Forge

**English** | [한국어](README.ko.md)

A safe, extensible code-transformation runner for planning, validating, applying, and continuously monitoring repeatable repository migrations.

> **Status:** early MVP. The local CLI, isolated planning, validation, and repository watcher are functional. MCP, agent skills, GitHub pull requests, and dedicated Claude Code/Codex adapters are planned but not implemented yet.

## Why Refactor Forge?

Large migrations should not rely on an LLM rewriting every file independently. Refactor Forge separates responsibilities:

- deterministic engines perform repeatable changes;
- an isolated planner produces a diff without touching the source repository;
- build and test commands validate the result;
- policy gates control whether a patch is reported or applied;
- LLM CLIs can later use the same engine through a shared MCP interface.

The intended long-term architecture is:

```text
Claude Code / Codex CLI / other agents
                 |
          Agent Skill + MCP
                 |
          Refactor Forge Core
          /       |        \
 OpenRewrite   ast-grep   custom engines
                 |
       build / test / report / PR
```

Refactor Forge remains useful without an LLM. The current MVP is model-agnostic and has no runtime Python dependencies.

## Current features

- JSON transformation specifications
- deterministic regular-expression transformations
- a command adapter for OpenRewrite, ast-grep, codemods, or internal tools
- isolated `plan` runs in a throwaway Git clone of committed `HEAD` for Git repositories (with an isolated-copy fallback for non-Git, unborn, or targets whose directory is not yet in `HEAD`)
- unified diff generation
- safe `apply` restricted to Git repositories
- clean-working-tree enforcement by default
- post-transformation build and test commands
- continuous repository monitoring
- validated patch reports without mutating the repository
- explicit opt-in automatic application
- a Python `Transformation` extension interface

## Safety model

- `plan` never modifies the target repository. For a Git target with a committed `HEAD`, it creates a throwaway clone with an independent object database, refs, local config, and no remotes. The clone is removed on every exit path; it is not registered as a source worktree.
- The default Git `plan` evaluates the committed, tracked `HEAD` snapshot. Dirty, staged, untracked, ignored, and submodule contents are excluded and called out in the report. If the target is not present in `HEAD`, `plan` falls back to an isolated copy of the current target and explicitly reports that Git context is unavailable. Non-Git and unborn repositories use the same isolated-copy behavior.
- Git snapshots are materialized from raw blobs rather than checkout, so repository checkout hooks and smudge filters are not executed. File and directory symlinks are never followed by transformations or snapshots, which prevents writes outside the sandbox.
- External command steps require `--allow-command`; commands and verification use the disposable clone's Git context and a scrubbed Git environment, so refs, config, and remotes in the target repository cannot be changed by that context.
- Commands are argument arrays and are never executed through a shell.
- Watch mode fingerprints the current working tree and plans that same working-tree snapshot, including dirty, untracked, ignored, and populated submodule files (subject to normal excluded-directory rules (including watcher reports) and symlink safety rules). It is report-only unless `--auto-apply` is explicitly supplied.
- `apply` accepts a repository root or nested target, checks the containing repository, and rejects a dirty working tree by default. Auto-apply is blocked when tracked working-tree changes exist.
- Verification failure fails the run; a cleanup-only warning is attached to a completed report, while a leaked sandbox remains fatal.
- The current core does not create commits, push branches, or open pull requests.

## Requirements

- Python 3.9 or newer
- Git for Git-backed `plan`, `apply`, and automatic watch application
- Any build tools referenced by your transformation specification

OpenRewrite and ast-grep are optional external tools; neither is bundled.

## Installation

### Recommended: install as an isolated CLI tool

Install the latest version directly from GitHub with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install "refactor-forge @ git+https://github.com/namekun/refactor-forge.git"
```

If this is your first `uv tool` installation, make sure its executable directory is on `PATH`:

```bash
uv tool update-shell
```

Upgrade later with:

```bash
uv tool upgrade refactor-forge
```

Once releases are published to PyPI, the recommended command will become:

```bash
uv tool install refactor-forge
```

### Development installation

Use an editable environment only when contributing to Refactor Forge itself:

```bash
git clone https://github.com/namekun/refactor-forge.git
cd refactor-forge
uv venv
. .venv/bin/activate
uv pip install -e .
```

The standard `venv` and `pip` workflow also works, but it is intentionally not the primary end-user installation path.

## Quick start

Download the example transformation specification if you installed the CLI without cloning the repository:

```bash
curl -fsSLO https://raw.githubusercontent.com/namekun/refactor-forge/main/examples/javax-to-jakarta.json
```

Preview a transformation in an isolated Git clone of committed `HEAD` (or an isolated copy for a non-Git/unborn/target-not-present-in-`HEAD`):

```bash
refactor-forge plan \
  --spec javax-to-jakarta.json \
  --target /path/to/repository
```

Apply it to a clean Git working tree:

```bash
refactor-forge apply \
  --spec javax-to-jakarta.json \
  --target /path/to/repository
```

A successful plan prints a unified diff such as:

```diff
-import javax.annotation.PostConstruct;
+import jakarta.annotation.PostConstruct;
```

The bundled example uses a small deterministic replacement to demonstrate the execution model. Type-aware Java migrations should use an OpenRewrite recipe through the command adapter.

## Transformation specification

```json
{
  "schema_version": 1,
  "name": "javax-annotation-to-jakarta",
  "description": "Migrate javax.annotation imports to jakarta.annotation",
  "steps": [
    {
      "type": "regex",
      "name": "replace-annotation-imports",
      "includes": ["**/*.java"],
      "pattern": "\\bjavax\\.annotation\\.",
      "replacement": "jakarta.annotation."
    }
  ],
  "verify": [["./mvnw", "test"]]
}
```

### External transformation engines

Command steps can invoke OpenRewrite, ast-grep, or another deterministic engine:

```json
{
  "schema_version": 1,
  "name": "openrewrite-external-recipe",
  "steps": [
    {
      "type": "command",
      "name": "run-openrewrite",
      "command": [
        "./mvnw",
        "rewrite:run",
        "-Drewrite.activeRecipes=com.example.MyRecipe"
      ]
    }
  ],
  "verify": [["./mvnw", "test"]]
}
```

External commands are disabled unless explicitly allowed:

```bash
refactor-forge plan \
  --spec examples/openrewrite-command.json \
  --target /path/to/repository \
  --allow-command
```

For committed Git targets, command steps and verification commands run with a throwaway clone as their current directory, so Git-aware tools see a valid repository whose refs, config, and object database are independent and whose source remote is removed. The default plan uses committed `HEAD`, not current dirty, staged, or untracked files; source state remains unchanged. Watch mode explicitly uses the matching current working-tree snapshot.

## Continuous monitoring

Start a report-only watcher:

```bash
refactor-forge watch \
  --spec examples/javax-to-jakarta.json \
  --target /path/to/repository \
  --interval 30
```

Run one monitoring tick for CI, cron, `launchd`, or `systemd`:

```bash
refactor-forge watch \
  --spec examples/javax-to-jakarta.json \
  --target /path/to/repository \
  --once
```

State and reports are stored under `.refactor-forge/` by default. Watch events include:

- `baseline` — the initial repository state was recorded;
- `unchanged` — no repository change was detected;
- `no_patch` — the repository changed, but the transformation is not needed;
- `patch_available` — a validated patch report was created;
- `blocked_dirty` — automatic application was blocked by tracked changes;
- `applied` — the patch was applied and verified.

Automatic application must be enabled explicitly:

```bash
refactor-forge watch \
  --spec examples/javax-to-jakarta.json \
  --target /path/to/repository \
  --auto-apply
```

## Testing

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The test suite covers isolated-clone planning and cleanup, symlink containment, source refs/config/remote preservation, raw-snapshot hook/filter safety, nested targets (including excluded-name components), untracked/ignored/submodule handling, Git-context verification, non-Git and unborn fallback planning, nested and dirty-tree application, working-tree drift monitoring, dirty-tree blocking, and opt-in automatic application.

## Roadmap

- OpenRewrite-specific adapter and recipe catalog
- MCP server shared by Claude Code, Codex CLI, and other MCP clients
- portable Agent Skill describing safe migration workflows
- dependency and security-advisory monitors
- risk-based approval policies
- branch and pull-request creation
- bounded LLM-assisted failure analysis
- multi-repository orchestration

## License and third-party tools

Refactor Forge is licensed under the [MIT License](LICENSE).

The current Python package does not bundle third-party transformation engines and has no runtime Python dependencies. Optional tools keep their own licenses:

- [OpenRewrite](https://github.com/openrewrite/rewrite) — Apache License 2.0
- [ast-grep](https://github.com/ast-grep/ast-grep) — MIT License

Claude Code, Codex CLI, Maven, and Gradle are optional external programs and are not distributed as part of this repository. Users are responsible for complying with their respective terms and licenses.

## Contributing

Issues and focused pull requests are welcome. Please include tests for behavioral changes and keep transformations deterministic whenever possible.

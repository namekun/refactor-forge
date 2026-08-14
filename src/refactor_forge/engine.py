from __future__ import annotations

import contextlib
import difflib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, Iterator, List, Optional, Tuple

from .sdk import TransformationContext
from .spec import TransformationSpec


EXCLUDED = {".git", ".gradle", ".idea", "build", "target", "node_modules", ".venv", "reports"}


@dataclass
class RunReport:
    transformation: str
    mode: str
    changed_files: List[str] = field(default_factory=list)
    messages: List[str] = field(default_factory=list)
    verification: List[str] = field(default_factory=list)
    diff: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)


def _ignored(_directory: str, names: List[str]) -> List[str]:
    return [name for name in names if name in EXCLUDED]


def _ignore_git_metadata(_directory: str, names: List[str]) -> List[str]:
    # A source worktree can contain a .git file for a linked worktree or a
    # submodule.  Never copy that metadata into the isolated clone.
    return [name for name in names if name == ".git"]


def _git_environment() -> Dict[str, str]:
    """Return a Git environment that cannot inherit an ambient repository."""
    environment = dict(os.environ)
    for key in list(environment):
        if key.startswith("GIT_"):
            del environment[key]
    # Do not let a user's global/system config install hooks, aliases, or
    # filters into the disposable repository.  The clone's local config is
    # still read normally.
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_PAGER"] = "cat"
    return environment


def _git_repository_root(target: Path) -> Optional[Path]:
    """Return the containing Git worktree root, or ``None`` when not Git."""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=target,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            env=_git_environment(),
        )
    except OSError:
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    try:
        return Path(completed.stdout.strip()).resolve()
    except (OSError, RuntimeError):
        return None


def _git_has_head(repository: Path) -> bool:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=repository,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            env=_git_environment(),
        )
    except OSError:
        return False
    return completed.returncode == 0


def _git_head_commit(repository: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=repository,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env=_git_environment(),
        )
    except OSError as exc:
        raise RuntimeError(f"Unable to read Git HEAD: {exc}") from exc
    if completed.returncode != 0 or not completed.stdout.strip():
        raise RuntimeError(completed.stderr.strip() or "Unable to read Git HEAD")
    return completed.stdout.strip()


def _git_target_in_head(repository: Path, relative_target: Path) -> bool:
    if not relative_target.parts:
        return True
    expression = "HEAD:" + relative_target.as_posix()
    try:
        completed = subprocess.run(
            ["git", "cat-file", "-e", expression],
            cwd=repository,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            env=_git_environment(),
        )
    except OSError:
        return False
    return completed.returncode == 0


def _git_target_is_submodule(repository: Path, relative_target: Path) -> bool:
    if not relative_target.parts:
        return False
    path_args = ["--", relative_target.as_posix()]
    try:
        listed = subprocess.run(
            ["git", "ls-files", "--stage", *path_args],
            cwd=repository,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            env=_git_environment(),
        )
    except OSError:
        return False
    target_name = relative_target.as_posix()
    for line in listed.stdout.splitlines() if listed.returncode == 0 else []:
        fields = line.split(None, 3)
        if len(fields) == 4 and fields[0] == "160000" and fields[3] == target_name:
            return True
    return False


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _remove_path(path: Path) -> None:
    if not _path_exists(path):
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _safe_relative(root: Path, path: Path) -> Optional[Path]:
    """Return a safe relative path, rejecting symlink-based escapes."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    if path.is_symlink():
        return None
    try:
        resolved_root = root.resolve()
        resolved_path = path.resolve()
        if not resolved_path.is_relative_to(resolved_root):
            return None
    except (OSError, RuntimeError):
        return None
    return relative


def _text_snapshot(root: Path) -> Dict[str, str]:
    snapshot: Dict[str, str] = {}
    for path in root.rglob("*"):
        relative = _safe_relative(root, path)
        if relative is None or not path.is_file():
            continue
        if any(part in EXCLUDED for part in relative.parts):
            continue
        try:
            snapshot[relative.as_posix()] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return snapshot


def _diff(before: Dict[str, str], after: Dict[str, str]) -> Tuple[str, List[str]]:
    chunks: List[str] = []
    changed: List[str] = []
    for name in sorted(set(before) | set(after)):
        old = before.get(name, "").splitlines(keepends=True)
        new = after.get(name, "").splitlines(keepends=True)
        if old == new:
            continue
        changed.append(name)
        chunks.extend(difflib.unified_diff(old, new, fromfile=f"a/{name}", tofile=f"b/{name}"))
    return "".join(chunks), changed


def _run_verify(root: Path, commands: List[List[str]], environment: Optional[Dict[str, str]] = None) -> List[str]:
    messages: List[str] = []
    for command in commands:
        completed = subprocess.run(
            command,
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            env=environment,
        )
        messages.append(f"{' '.join(command)}: {'PASS' if completed.returncode == 0 else 'FAIL'}")
        if completed.returncode != 0:
            raise RuntimeError(f"Verification failed: {' '.join(command)}\n{completed.stdout.strip()}")
    return messages


def _execute(
    spec: TransformationSpec,
    root: Path,
    mode: str,
    allow_commands: bool,
    environment: Optional[Dict[str, str]] = None,
) -> RunReport:
    before = _text_snapshot(root)
    report = RunReport(transformation=spec.name, mode=mode)
    context = TransformationContext(
        root=root,
        dry_run=(mode == "plan"),
        allow_commands=allow_commands,
        environment=environment,
    )
    for step in spec.steps:
        result = step.apply(context)
        report.messages.extend(f"[{result.name}] {message}" for message in result.messages)
    after = _text_snapshot(root)
    report.diff, report.changed_files = _diff(before, after)
    report.verification = _run_verify(root, spec.verify, environment)
    return report


@dataclass
class _SandboxState:
    cleanup_messages: List[str] = field(default_factory=list)


@dataclass
class _Sandbox:
    root: Path
    environment: Dict[str, str]
    state: _SandboxState


def _cleanup_temporary_directory(temporary: tempfile.TemporaryDirectory, root: Path) -> List[str]:
    """Clean a temporary sandbox and return non-fatal cleanup diagnostics."""
    errors: List[str] = []
    try:
        temporary.cleanup()
    except BaseException as exc:
        errors.append(f"temporary sandbox cleanup failed: {exc}")
    if _path_exists(root):
        try:
            _remove_path(root)
        except BaseException as exc:
            errors.append(f"unable to remove temporary sandbox directory: {exc}")
    if _path_exists(root):
        errors.append("temporary sandbox directory remains")
    return errors


def _git_tree_path(destination: Path, raw_name: bytes) -> Optional[Path]:
    """Validate a raw Git tree path before materialising it."""
    name = os.fsdecode(raw_name)
    pure = PurePosixPath(name)
    if pure.is_absolute():
        raise RuntimeError(f"Unsafe Git tree path: {name}")
    parts = tuple(part for part in pure.parts if part not in ("", "."))
    if not parts or any(part == ".." for part in parts) or ".git" in parts:
        if any(part == ".." for part in parts):
            raise RuntimeError(f"Unsafe Git tree path: {name}")
        return None
    path = destination.joinpath(*parts)
    try:
        contained = path.parent.resolve().is_relative_to(destination.resolve())
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f"Unsafe Git tree path: {name}") from exc
    if not contained:
        raise RuntimeError(f"Unsafe Git tree path: {name}")
    return path


def _materialize_git_tree(clone: Path, commit: str, environment: Dict[str, str]) -> None:
    """Materialise raw tree entries, avoiding checkout filters and hooks."""
    listed = subprocess.run(
        ["git", "ls-tree", "-r", "-z", commit],
        cwd=clone,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=environment,
    )
    if listed.returncode != 0:
        raise RuntimeError(listed.stderr.decode(errors="replace").strip() or "Unable to list temporary Git snapshot")
    for record in listed.stdout.split(b"\0"):
        if not record:
            continue
        try:
            header, raw_name = record.split(b"\t", 1)
            mode_raw, entry_type, object_id = header.split(b" ", 2)
        except ValueError as exc:
            raise RuntimeError("Malformed Git tree entry") from exc
        path = _git_tree_path(clone, raw_name)
        if path is None:
            continue
        mode = mode_raw.decode("ascii")
        if entry_type == b"commit" and mode == "160000":
            # Gitlinks are intentionally not fetched.  Working-tree watch
            # mode may overlay an already-populated submodule from the source.
            continue
        if entry_type != b"blob" or mode not in {"100644", "100755", "120000"}:
            raise RuntimeError(f"Unsupported Git tree entry: {os.fsdecode(raw_name)}")
        blob = subprocess.run(
            ["git", "cat-file", "blob", object_id.decode("ascii")],
            cwd=clone,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env=environment,
        )
        if blob.returncode != 0:
            raise RuntimeError(blob.stderr.decode(errors="replace").strip() or "Unable to read temporary Git blob")
        path.parent.mkdir(parents=True, exist_ok=True)
        if mode == "120000":
            os.symlink(os.fsdecode(blob.stdout), path)
        else:
            path.write_bytes(blob.stdout)
            try:
                os.chmod(path, 0o755 if mode == "100755" else 0o644)
            except OSError:
                pass


def _copy_working_tree(source: Path, destination: Path) -> None:
    """Overlay a source tree without dereferencing any source symlink."""
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        destination,
        symlinks=True,
        ignore=_ignore_git_metadata,
        dirs_exist_ok=True,
    )


def _clear_overlay(destination: Path, preserve_git: bool) -> None:
    if not _path_exists(destination):
        destination.mkdir(parents=True, exist_ok=True)
        return
    if destination.is_symlink() or not destination.is_dir():
        _remove_path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        return
    for child in list(destination.iterdir()):
        if preserve_git and child.name == ".git":
            continue
        _remove_path(child)


def _remove_clone_remotes(clone: Path, environment: Dict[str, str]) -> None:
    listed = subprocess.run(
        ["git", "remote"],
        cwd=clone,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=environment,
    )
    if listed.returncode != 0:
        raise RuntimeError(listed.stderr.strip() or "Unable to inspect temporary clone remotes")
    for remote in listed.stdout.splitlines():
        if not remote.strip():
            continue
        removed = subprocess.run(
            ["git", "remote", "remove", remote.strip()],
            cwd=clone,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env=environment,
        )
        if removed.returncode != 0:
            raise RuntimeError(removed.stderr.strip() or f"Unable to remove temporary clone remote: {remote}")


def _create_git_clone(repository: Path, commit: str, clone: Path, hooks: Path, environment: Dict[str, str]) -> None:
    cloned = subprocess.run(
        [
            "git",
            "clone",
            "--no-local",
            "--no-hardlinks",
            "--no-checkout",
            "--no-tags",
            "--no-recurse-submodules",
            "-c",
            f"core.hooksPath={hooks}",
            str(repository),
            str(clone),
        ],
        cwd=repository,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=environment,
    )
    if cloned.returncode != 0:
        raise RuntimeError(cloned.stderr.strip() or "Unable to create temporary Git clone")

    snapshot_ref = "refs/heads/refactor-forge-snapshot"
    updated = subprocess.run(
        ["git", "update-ref", snapshot_ref, commit],
        cwd=clone,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=environment,
    )
    if updated.returncode != 0:
        raise RuntimeError(updated.stderr.strip() or "Unable to prepare temporary Git clone")
    detached = subprocess.run(
        ["git", "symbolic-ref", "HEAD", snapshot_ref],
        cwd=clone,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=environment,
    )
    if detached.returncode != 0:
        raise RuntimeError(detached.stderr.strip() or "Unable to prepare temporary Git clone HEAD")
    indexed = subprocess.run(
        ["git", "read-tree", commit],
        cwd=clone,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=environment,
    )
    if indexed.returncode != 0:
        raise RuntimeError(indexed.stderr.strip() or "Unable to prepare temporary Git clone index")
    _remove_clone_remotes(clone, environment)
    _materialize_git_tree(clone, commit, environment)


@contextlib.contextmanager
def _git_sandbox(
    repository: Path,
    commit: str,
    relative_target: Path,
    source_target: Path,
    overlay_working_tree: bool,
) -> Iterator[_Sandbox]:
    """Yield an independent, hook-free clone for Git-aware plan commands."""
    temporary = tempfile.TemporaryDirectory(prefix="refactor-forge-clone-")
    temporary_root = Path(temporary.name)
    clone = temporary_root / "repo"
    hooks = temporary_root / "empty-hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    environment = _git_environment()
    state = _SandboxState()
    operation_error: Optional[BaseException] = None
    try:
        _create_git_clone(repository, commit, clone, hooks, environment)
        if overlay_working_tree:
            destination = clone / relative_target
            _clear_overlay(destination, preserve_git=(not relative_target.parts))
            _copy_working_tree(source_target, destination)
        yield _Sandbox(clone / relative_target, environment, state)
    except BaseException as exc:
        operation_error = exc
        raise
    finally:
        cleanup_errors: List[str]
        try:
            cleanup_errors = _cleanup_temporary_directory(temporary, temporary_root)
        except BaseException as exc:
            cleanup_errors = [f"temporary sandbox cleanup failed: {exc}"]
        if cleanup_errors:
            state.cleanup_messages.extend(cleanup_errors)
        if _path_exists(temporary_root):
            leak = "; ".join(cleanup_errors) or "temporary sandbox directory remains"
            if operation_error is None:
                raise RuntimeError("Git sandbox cleanup failed: " + leak)
            raise RuntimeError(f"{operation_error}; Git sandbox cleanup failed: {leak}") from operation_error


def _git_plan_warnings(repository: Path, target: Path, relative_target: Path) -> List[str]:
    """Explain content intentionally excluded from the default HEAD plan."""
    path_args = [] if not relative_target.parts else ["--", relative_target.as_posix()]
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching", *path_args],
            cwd=repository,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env=_git_environment(),
        )
    except OSError:
        status = None
    warnings: List[str] = []
    if status is not None and status.returncode == 0:
        lines = status.stdout.splitlines()
        if any(line[:2] not in ("??", "!!", "  ") for line in lines if len(line) >= 2):
            warnings.append("Committed-HEAD plan excludes tracked working-tree or staged changes under the target")
        if any(line.startswith("??") for line in lines):
            warnings.append("Committed-HEAD plan excludes untracked files under the target")
        if any(line.startswith("!!") for line in lines):
            warnings.append("Committed-HEAD plan excludes ignored files under the target")
    try:
        listed = subprocess.run(
            ["git", "ls-files", "--stage", *path_args],
            cwd=repository,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            env=_git_environment(),
        )
    except OSError:
        listed = None
    if listed is not None and listed.returncode == 0:
        if any(line.startswith("160000 ") for line in listed.stdout.splitlines()):
            warnings.append("Committed-HEAD plan does not materialize Git submodule contents")
    return warnings


def _copy_plan(
    spec: TransformationSpec,
    target: Path,
    message: Optional[str] = None,
    allow_commands: bool = False,
) -> RunReport:
    with tempfile.TemporaryDirectory(prefix="refactor-forge-") as temp:
        sandbox = Path(temp) / "repo"
        shutil.copytree(target, sandbox, symlinks=True, ignore=_ignored)
        report = _execute(spec, sandbox, "plan", allow_commands, _git_environment())
    if message:
        report.messages.insert(0, message)
    return report


def plan(
    spec: TransformationSpec,
    target: Path,
    allow_commands: bool = False,
    *,
    snapshot: str = "head",
) -> RunReport:
    """Plan a transformation in an isolated snapshot.

    ``snapshot="head"`` is the backwards-compatible Git plan mode: only the
    committed target is evaluated.  Watch mode opts into
    ``snapshot="working-tree"`` so its fingerprint and plan inspect the same
    current files, including untracked and ignored files.
    """
    if snapshot not in {"head", "working-tree"}:
        raise ValueError(f"Unsupported plan snapshot: {snapshot}")
    if not target.is_dir():
        raise ValueError(f"Target is not a directory: {target}")
    target = target.resolve()
    repository = _git_repository_root(target)
    if repository is not None and _git_has_head(repository):
        try:
            relative_target = target.relative_to(repository)
        except ValueError:
            return _copy_plan(
                spec,
                target,
                "Git worktree discovery did not contain the target; used an isolated copy without Git context",
                allow_commands,
            )
        if snapshot == "head" and (
            not _git_target_in_head(repository, relative_target)
            or _git_target_is_submodule(repository, relative_target)
        ):
            return _copy_plan(
                spec,
                target,
                "Target is not materialized as an ordinary directory in committed HEAD; used an isolated working-tree copy without Git context",
                allow_commands,
            )
        commit = _git_head_commit(repository)
        warnings = _git_plan_warnings(repository, target, relative_target) if snapshot == "head" else []
        report: Optional[RunReport] = None
        with _git_sandbox(
            repository,
            commit,
            relative_target,
            target,
            overlay_working_tree=(snapshot == "working-tree"),
        ) as sandbox:
            if not sandbox.root.is_dir():
                # A target absent from HEAD is handled above for the default
                # mode.  This guard keeps the working-tree mode explicit if a
                # concurrent source deletion occurs during setup.
                raise RuntimeError("Target disappeared while preparing the isolated Git snapshot")
            report = _execute(spec, sandbox.root, "plan", allow_commands, sandbox.environment)
        assert report is not None
        report.messages[0:0] = warnings
        if sandbox.state.cleanup_messages:
            report.messages.extend(
                f"Sandbox cleanup warning: {message}" for message in sandbox.state.cleanup_messages
            )
        return report

    return _copy_plan(
        spec,
        target,
        "Used an isolated copy without Git context because the target has no committed Git HEAD",
        allow_commands,
    )


def apply(
    spec: TransformationSpec,
    target: Path,
    allow_commands: bool = False,
    allow_dirty: bool = False,
) -> RunReport:
    if not target.is_dir():
        raise ValueError(f"Target is not a directory: {target}")
    target = target.resolve()
    repository = _git_repository_root(target)
    if repository is None:
        raise RuntimeError("Apply requires a Git repository; use plan for non-Git directories")
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repository,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=_git_environment(),
    )
    if status.returncode != 0:
        raise RuntimeError(status.stderr.strip() or "Unable to inspect Git status")
    if status.stdout.strip() and not allow_dirty:
        raise RuntimeError("Working tree is dirty. Commit/stash first, or pass --allow-dirty")
    return _execute(spec, target, "apply", allow_commands, _git_environment())

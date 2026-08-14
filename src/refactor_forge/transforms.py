from __future__ import annotations

import fnmatch
import re
import subprocess
from pathlib import Path
from typing import Iterable, Sequence

from .sdk import Transformation, TransformationContext, TransformationResult


IGNORED_PARTS = {".git", ".gradle", ".idea", "build", "target", "node_modules", ".venv", "reports"}


def _safe_relative(root: Path, path: Path):
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    # Never follow a file or directory symlink from a transformation sandbox.
    # The resolved containment check also covers a symlinked parent if a
    # platform's rglob implementation traverses one.
    if path.is_symlink():
        return None
    try:
        if not path.resolve().is_relative_to(root.resolve()):
            return None
    except (OSError, RuntimeError):
        return None
    return relative


def iter_files(root: Path, includes: Sequence[str]) -> Iterable[Path]:
    for path in root.rglob("*"):
        relative = _safe_relative(root, path)
        if relative is None or not path.is_file():
            continue
        if any(part in IGNORED_PARTS for part in relative.parts):
            continue
        rel = relative.as_posix()
        if any(fnmatch.fnmatch(rel, pattern) for pattern in includes):
            yield path


class RegexTransformation(Transformation):
    def __init__(self, name: str, includes: Sequence[str], pattern: str, replacement: str):
        self._name = name
        self.includes = list(includes)
        self.pattern = re.compile(pattern, re.MULTILINE)
        self.replacement = replacement

    @property
    def name(self) -> str:
        return self._name

    def apply(self, context: TransformationContext) -> TransformationResult:
        result = TransformationResult(name=self.name)
        for path in iter_files(context.root, self.includes):
            # Re-check immediately before reading and writing.  This keeps a
            # command step or a concurrent filesystem change from turning a
            # previously safe candidate into a symlink target.
            if _safe_relative(context.root, path) is None:
                continue
            try:
                original = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            updated, count = self.pattern.subn(self.replacement, original)
            if count and updated != original:
                if _safe_relative(context.root, path) is None:
                    continue
                path.write_text(updated, encoding="utf-8")
                result.changed_files.append(path.relative_to(context.root).as_posix())
                result.messages.append(f"{count} replacement(s) in {path.relative_to(context.root)}")
        return result


class CommandTransformation(Transformation):
    """Adapter for OpenRewrite, ast-grep, codemod, or an internal executable."""

    def __init__(self, name: str, command: Sequence[str]):
        self._name = name
        self.command = list(command)

    @property
    def name(self) -> str:
        return self._name

    def apply(self, context: TransformationContext) -> TransformationResult:
        if not context.allow_commands:
            raise PermissionError(f"Command transformation '{self.name}' requires --allow-command")
        completed = subprocess.run(
            self.command,
            cwd=context.root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            env=context.environment,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"Command transformation '{self.name}' failed ({completed.returncode}):\n{completed.stdout}"
            )
        messages = [completed.stdout.strip()] if completed.stdout.strip() else []
        return TransformationResult(name=self.name, messages=messages)

from __future__ import annotations

import difflib
import json
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

from .sdk import TransformationContext
from .spec import TransformationSpec


EXCLUDED = {".git", ".gradle", ".idea", "build", "target", "node_modules", ".venv"}


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


def _text_snapshot(root: Path) -> Dict[str, str]:
    snapshot: Dict[str, str] = {}
    for path in root.rglob("*"):
        if not path.is_file() or any(part in EXCLUDED for part in path.parts):
            continue
        try:
            snapshot[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
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


def _run_verify(root: Path, commands: List[List[str]]) -> List[str]:
    messages: List[str] = []
    for command in commands:
        completed = subprocess.run(command, cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        messages.append(f"{' '.join(command)}: {'PASS' if completed.returncode == 0 else 'FAIL'}")
        if completed.returncode != 0:
            raise RuntimeError(f"Verification failed: {' '.join(command)}\n{completed.stdout.strip()}")
    return messages


def _execute(spec: TransformationSpec, root: Path, mode: str, allow_commands: bool) -> RunReport:
    before = _text_snapshot(root)
    report = RunReport(transformation=spec.name, mode=mode)
    context = TransformationContext(root=root, dry_run=(mode == "plan"), allow_commands=allow_commands)
    for step in spec.steps:
        result = step.apply(context)
        report.messages.extend(f"[{result.name}] {message}" for message in result.messages)
    after = _text_snapshot(root)
    report.diff, report.changed_files = _diff(before, after)
    report.verification = _run_verify(root, spec.verify)
    return report


def plan(spec: TransformationSpec, target: Path, allow_commands: bool = False) -> RunReport:
    if not target.is_dir():
        raise ValueError(f"Target is not a directory: {target}")
    with tempfile.TemporaryDirectory(prefix="refactor-forge-") as temp:
        sandbox = Path(temp) / "repo"
        shutil.copytree(target, sandbox, ignore=_ignored)
        return _execute(spec, sandbox, "plan", allow_commands)


def apply(spec: TransformationSpec, target: Path, allow_commands: bool = False, allow_dirty: bool = False) -> RunReport:
    if not target.is_dir():
        raise ValueError(f"Target is not a directory: {target}")
    if not (target / ".git").exists():
        raise RuntimeError("Apply requires a Git repository; use plan for non-Git directories")
    status = subprocess.run(["git", "status", "--porcelain"], cwd=target, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if status.returncode != 0:
        raise RuntimeError(status.stderr.strip() or "Unable to inspect Git status")
    if status.stdout.strip() and not allow_dirty:
        raise RuntimeError("Working tree is dirty. Commit/stash first, or pass --allow-dirty")
    return _execute(spec, target, "apply", allow_commands)

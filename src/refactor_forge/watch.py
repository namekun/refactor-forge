from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Set

from .engine import RunReport, apply, plan
from .spec import TransformationSpec


DEFAULT_IGNORED = {".git", ".gradle", ".idea", ".venv", "build", "target", "node_modules", "reports"}


@dataclass(frozen=True)
class WatchConfig:
    target: Path
    spec: TransformationSpec
    state_file: Path
    reports_dir: Path
    interval_seconds: float = 30.0
    auto_apply: bool = False
    allow_commands: bool = False


@dataclass
class WatchEvent:
    status: str
    fingerprint: str
    report: Optional[RunReport] = None
    report_path: Optional[Path] = None
    message: str = ""


def _git_head(target: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=target, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "no-git-head"


def _tracked_tree_is_clean(target: Path) -> bool:
    unstaged = subprocess.run(["git", "diff", "--quiet"], cwd=target, check=False)
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=target, check=False)
    return unstaged.returncode == 0 and staged.returncode == 0


def repository_fingerprint(target: Path, extra_ignored: Set[Path]) -> str:
    digest = hashlib.sha256()
    digest.update(_git_head(target).encode("utf-8"))
    resolved_ignored = {path.resolve() for path in extra_ignored}
    for path in sorted(target.rglob("*")):
        if not path.is_file() or any(part in DEFAULT_IGNORED for part in path.parts):
            continue
        resolved_path = path.resolve()
        if any(resolved_path == ignored or ignored in resolved_path.parents for ignored in resolved_ignored):
            continue
        relative = path.relative_to(target).as_posix()
        digest.update(relative.encode("utf-8"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            continue
    return digest.hexdigest()


class WatchService:
    def __init__(self, config: WatchConfig):
        self.config = config

    def _fingerprint(self) -> str:
        return repository_fingerprint(
            self.config.target,
            {self.config.state_file, self.config.reports_dir},
        )

    def _load_fingerprint(self) -> Optional[str]:
        if not self.config.state_file.exists():
            return None
        try:
            return json.loads(self.config.state_file.read_text(encoding="utf-8")).get("fingerprint")
        except (json.JSONDecodeError, OSError):
            return None

    def _save_fingerprint(self, fingerprint: str, status: str) -> None:
        self.config.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fingerprint": fingerprint,
            "status": status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        temporary = self.config.state_file.with_suffix(self.config.state_file.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(self.config.state_file)

    def _write_report(self, report: RunReport, status: str) -> Path:
        self.config.reports_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        path = self.config.reports_dir / f"{timestamp}-{status}.json"
        payload = asdict(report)
        payload["watch_status"] = status
        payload["created_at"] = datetime.now(timezone.utc).isoformat()
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def tick(self) -> WatchEvent:
        fingerprint = self._fingerprint()
        previous = self._load_fingerprint()
        if previous is None:
            self._save_fingerprint(fingerprint, "baseline")
            return WatchEvent("baseline", fingerprint, message="Baseline recorded")
        if previous == fingerprint:
            return WatchEvent("unchanged", fingerprint, message="No repository changes")

        planned = plan(self.config.spec, self.config.target, self.config.allow_commands)
        if not planned.changed_files:
            self._save_fingerprint(fingerprint, "no_patch")
            return WatchEvent("no_patch", fingerprint, report=planned, message="Change detected, no patch required")

        if not self.config.auto_apply:
            report_path = self._write_report(planned, "patch_available")
            self._save_fingerprint(fingerprint, "patch_available")
            return WatchEvent("patch_available", fingerprint, planned, report_path, "Validated patch is available")

        if not _tracked_tree_is_clean(self.config.target):
            report_path = self._write_report(planned, "blocked_dirty")
            self._save_fingerprint(fingerprint, "blocked_dirty")
            return WatchEvent("blocked_dirty", fingerprint, planned, report_path, "Tracked working tree changes block auto-apply")

        applied = apply(
            self.config.spec,
            self.config.target,
            allow_commands=self.config.allow_commands,
            allow_dirty=True,
        )
        report_path = self._write_report(applied, "applied")
        resulting_fingerprint = self._fingerprint()
        self._save_fingerprint(resulting_fingerprint, "applied")
        return WatchEvent("applied", resulting_fingerprint, applied, report_path, "Patch applied and verified")

    def run_forever(self) -> None:
        while True:
            event = self.tick()
            if event.status != "unchanged":
                print(json.dumps({
                    "status": event.status,
                    "message": event.message,
                    "report_path": str(event.report_path) if event.report_path else None,
                }, ensure_ascii=False))
            time.sleep(self.config.interval_seconds)

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from refactor_forge.spec import load_spec
from refactor_forge.watch import WatchConfig, WatchService


SPEC = {
    "schema_version": 1,
    "name": "javax-to-jakarta",
    "steps": [{
        "type": "regex",
        "name": "imports",
        "includes": ["**/*.java"],
        "pattern": r"\bjavax\.annotation\.",
        "replacement": "jakarta.annotation."
    }],
    "verify": [["python3", "verify.py"]]
}


def initialize_repo(root: Path) -> None:
    (root / "src").mkdir()
    (root / "src" / "App.java").write_text("class App {}\n", encoding="utf-8")
    (root / "verify.py").write_text(
        "from pathlib import Path\n"
        "s=Path('src/App.java').read_text()\n"
        "raise SystemExit(0 if 'javax.annotation' not in s else 1)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )


class WatchServiceTest(unittest.TestCase):
    def make_service(self, root: Path, auto_apply: bool = False) -> WatchService:
        spec_path = root / "spec.json"
        spec_path.write_text(json.dumps(SPEC), encoding="utf-8")
        spec = load_spec(spec_path)
        config = WatchConfig(
            target=root,
            spec=spec,
            state_file=root / ".watch-state.json",
            reports_dir=root / "reports",
            auto_apply=auto_apply,
        )
        return WatchService(config)

    def test_monitor_only_plans_patch_without_mutating_repo(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialize_repo(root)
            service = self.make_service(root)
            first = service.tick()
            self.assertEqual("baseline", first.status)
            (root / "src" / "App.java").write_text(
                "import javax.annotation.PostConstruct;\nclass App {}\n", encoding="utf-8"
            )
            second = service.tick()
            self.assertEqual("patch_available", second.status)
            self.assertIn("jakarta.annotation", second.report.diff)
            self.assertIn("javax.annotation", (root / "src" / "App.java").read_text())
            self.assertTrue(second.report_path.exists())

    def test_auto_apply_requires_clean_tree_and_applies_after_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialize_repo(root)
            service = self.make_service(root, auto_apply=True)
            service.tick()
            (root / "src" / "App.java").write_text(
                "import javax.annotation.PostConstruct;\nclass App {}\n", encoding="utf-8"
            )
            dirty = service.tick()
            self.assertEqual("blocked_dirty", dirty.status)
            subprocess.run(["git", "add", "src/App.java"], cwd=root, check=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "legacy import"],
                cwd=root,
                check=True,
            )
            applied = service.tick()
            self.assertEqual("applied", applied.status)
            self.assertIn("jakarta.annotation", (root / "src" / "App.java").read_text())


if __name__ == "__main__":
    unittest.main()

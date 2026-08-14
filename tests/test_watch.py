import contextlib
import io
import json
import subprocess
import unittest.mock
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
    def make_service(self, root: Path, auto_apply: bool = False, target: Path = None) -> WatchService:
        spec_path = root / "spec.json"
        spec_path.write_text(json.dumps(SPEC), encoding="utf-8")
        spec = load_spec(spec_path)
        config = WatchConfig(
            target=target or root,
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
            subprocess.run(["git", "add", "src/App.java"], cwd=root, check=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "legacy import"],
                cwd=root,
                check=True,
            )
            (root / "verify.py").write_text(
                (root / "verify.py").read_text(encoding="utf-8") + "# unrelated dirty edit\n",
                encoding="utf-8",
            )
            dirty = service.tick()
            self.assertEqual("blocked_dirty", dirty.status)
            subprocess.run(["git", "add", "verify.py"], cwd=root, check=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "unrelated edit"],
                cwd=root,
                check=True,
            )
            applied = service.tick()
            self.assertEqual("applied", applied.status)
            self.assertIn("jakarta.annotation", (root / "src" / "App.java").read_text())


    def test_uncommitted_drift_is_planned_and_not_latched_as_no_patch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialize_repo(root)
            service = self.make_service(root)
            self.assertEqual("baseline", service.tick().status)
            (root / "src" / "App.java").write_text(
                "import javax.annotation.PostConstruct;\nclass App {}\n", encoding="utf-8"
            )

            changed = service.tick()
            unchanged = service.tick()

            self.assertEqual("patch_available", changed.status)
            self.assertIn("jakarta.annotation", changed.report.diff)
            self.assertEqual("unchanged", unchanged.status)
            self.assertIn("javax.annotation", (root / "src" / "App.java").read_text(encoding="utf-8"))

    def test_watch_sees_untracked_and_ignored_working_tree_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialize_repo(root)
            (root / ".gitignore").write_text("src/Generated.java\n", encoding="utf-8")
            subprocess.run(["git", "add", ".gitignore"], cwd=root, check=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "ignore"],
                cwd=root,
                check=True,
            )
            service = self.make_service(root)
            service.tick()
            (root / "src" / "Untracked.java").write_text("javax.annotation.X\n", encoding="utf-8")
            (root / "src" / "Generated.java").write_text("javax.annotation.X\n", encoding="utf-8")

            event = service.tick()

            self.assertEqual("patch_available", event.status)
            self.assertIn("src/Untracked.java", event.report.changed_files)
            self.assertIn("src/Generated.java", event.report.changed_files)
            self.assertIn("javax.annotation", (root / "src" / "Untracked.java").read_text(encoding="utf-8"))

    def test_nested_target_fingerprint_does_not_use_absolute_excluded_parts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "build" / "demo"
            (target / "src").mkdir(parents=True)
            (target / "src" / "App.java").write_text("class App {}\n", encoding="utf-8")
            (target / "verify.py").write_text(
                "from pathlib import Path\n"
                "raise SystemExit(0 if 'jakarta.annotation' in Path('src/App.java').read_text() else 1)\n",
                encoding="utf-8",
            )
            initialize_repo(root)
            service = self.make_service(root, target=target)
            self.assertEqual("baseline", service.tick().status)
            (target / "src" / "App.java").write_text("import javax.annotation.X;\n", encoding="utf-8")
            subprocess.run(["git", "add", "build/demo/src/App.java"], cwd=root, check=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "nested"],
                cwd=root,
                check=True,
            )

            event = service.tick()

            self.assertEqual("patch_available", event.status)
            self.assertEqual(["src/App.java"], event.report.changed_files)

    def test_nested_target_auto_apply_uses_containing_repository(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "packages" / "demo"
            (target / "src").mkdir(parents=True)
            (target / "src" / "App.java").write_text("class App {}\n", encoding="utf-8")
            (target / "verify.py").write_text(
                "from pathlib import Path\n"
                "raise SystemExit(0 if 'jakarta.annotation' in Path('src/App.java').read_text() else 1)\n",
                encoding="utf-8",
            )
            initialize_repo(root)
            service = self.make_service(root, auto_apply=True, target=target)
            service.tick()
            (target / "src" / "App.java").write_text("import javax.annotation.X;\n", encoding="utf-8")
            subprocess.run(["git", "add", "packages/demo/src/App.java"], cwd=root, check=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "nested"],
                cwd=root,
                check=True,
            )

            event = service.tick()

            self.assertEqual("applied", event.status)
            self.assertIn("jakarta.annotation", (target / "src" / "App.java").read_text(encoding="utf-8"))

    def test_run_forever_survives_one_tick_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialize_repo(root)
            service = self.make_service(root)
            calls = []

            def tick_side_effect():
                calls.append(True)
                if len(calls) == 1:
                    raise RuntimeError("transient cleanup failure")
                raise KeyboardInterrupt

            with unittest.mock.patch.object(service, "tick", side_effect=tick_side_effect):
                with unittest.mock.patch("refactor_forge.watch.time.sleep"):
                    output = io.StringIO()
                    with contextlib.redirect_stderr(output):
                        with self.assertRaises(KeyboardInterrupt):
                            service.run_forever()

            self.assertEqual(2, len(calls))
            self.assertIn("transient cleanup failure", output.getvalue())


if __name__ == "__main__":
    unittest.main()

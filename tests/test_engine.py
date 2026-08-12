import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from refactor_forge.engine import apply, plan
from refactor_forge.spec import load_spec


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


class EngineTest(unittest.TestCase):
    def make_repo(self, root: Path):
        (root / "src").mkdir()
        (root / "src" / "App.java").write_text("import javax.annotation.PostConstruct;\nclass App {}\n", encoding="utf-8")
        (root / "verify.py").write_text(
            "from pathlib import Path\n"
            "s=Path('src/App.java').read_text()\n"
            "raise SystemExit(0 if 'jakarta.annotation' in s else 1)\n",
            encoding="utf-8",
        )
        spec_path = root / "spec.json"
        spec_path.write_text(json.dumps(SPEC), encoding="utf-8")
        return load_spec(spec_path)

    def test_plan_is_isolated_and_returns_diff(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spec = self.make_repo(root)
            report = plan(spec, root)
            self.assertIn("jakarta.annotation", report.diff)
            self.assertIn("javax.annotation", (root / "src" / "App.java").read_text())
            self.assertEqual(["src/App.java"], report.changed_files)

    def test_apply_requires_clean_git_and_changes_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spec = self.make_repo(root)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "init"],
                cwd=root,
                check=True,
            )
            report = apply(spec, root)
            self.assertEqual(["src/App.java"], report.changed_files)
            self.assertIn("jakarta.annotation", (root / "src" / "App.java").read_text())
            self.assertEqual(["python3 verify.py: PASS"], report.verification)


if __name__ == "__main__":
    unittest.main()

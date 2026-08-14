import json
import os
import subprocess
import unittest.mock
import sys
import tempfile
import unittest
from pathlib import Path

from refactor_forge.engine import apply, plan
from refactor_forge.sdk import Transformation, TransformationContext, TransformationResult
from refactor_forge.spec import TransformationSpec, load_spec
from refactor_forge.transforms import CommandTransformation, RegexTransformation


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


class ExplodingTransformation(Transformation):
    @property
    def name(self) -> str:
        return "explode"

    def apply(self, context: TransformationContext) -> TransformationResult:
        raise RuntimeError("intentional transformation failure")


class EngineTest(unittest.TestCase):
    def make_repo(self, root: Path):
        (root / "src").mkdir()
        (root / "src" / "App.java").write_text(
            "import javax.annotation.PostConstruct;\nclass App {}\n", encoding="utf-8"
        )
        (root / "verify.py").write_text(
            "from pathlib import Path\n"
            "s=Path('src/App.java').read_text()\n"
            "raise SystemExit(0 if 'jakarta.annotation' in s else 1)\n",
            encoding="utf-8",
        )
        spec_path = root / "spec.json"
        spec_path.write_text(json.dumps(SPEC), encoding="utf-8")
        return load_spec(spec_path)

    def initialize_git(self, root: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "init"],
            cwd=root,
            check=True,
        )

    def git_output(self, root: Path, *command: str) -> str:
        return subprocess.run(
            ["git", *command], cwd=root, text=True, stdout=subprocess.PIPE, check=True
        ).stdout

    def worktree_admin_entries(self, root: Path):
        directory = root / ".git" / "worktrees"
        return sorted(path.name for path in directory.iterdir()) if directory.is_dir() else []

    def test_non_git_plan_uses_copy_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spec = self.make_repo(root)
            report = plan(spec, root)
            self.assertEqual(["src/App.java"], report.changed_files)
            self.assertIn("jakarta.annotation", report.diff)
            self.assertIn("javax.annotation", (root / "src" / "App.java").read_text())

    def test_git_plan_uses_head_snapshot_and_preserves_source_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spec = self.make_repo(root)
            self.initialize_git(root)
            (root / "src" / "App.java").write_text("class WorkingTreeOnly {}\n", encoding="utf-8")
            (root / "staged.txt").write_text("staged but not committed\n", encoding="utf-8")
            subprocess.run(["git", "add", "staged.txt"], cwd=root, check=True)
            (root / "existing-untracked.txt").write_text("keep me\n", encoding="utf-8")
            source_contents = {
                "app": (root / "src" / "App.java").read_bytes(),
                "untracked": (root / "existing-untracked.txt").read_bytes(),
            }
            status_before = self.git_output(root, "status", "--porcelain=v1")
            diff_before = self.git_output(root, "diff", "--binary")
            index_before = self.git_output(root, "diff", "--cached", "--binary")
            branch_before = self.git_output(root, "symbolic-ref", "--short", "HEAD")
            worktrees_before = self.git_output(root, "worktree", "list", "--porcelain")
            admin_before = self.worktree_admin_entries(root)

            report = plan(spec, root)

            # The report is based on committed HEAD, not the dirty source file.
            self.assertEqual(["src/App.java"], report.changed_files)
            self.assertIn("javax.annotation", report.diff)
            self.assertIn("jakarta.annotation", report.diff)
            self.assertEqual(source_contents["app"], (root / "src" / "App.java").read_bytes())
            self.assertEqual(source_contents["untracked"], (root / "existing-untracked.txt").read_bytes())
            self.assertEqual(status_before, self.git_output(root, "status", "--porcelain=v1"))
            self.assertEqual(diff_before, self.git_output(root, "diff", "--binary"))
            self.assertEqual(index_before, self.git_output(root, "diff", "--cached", "--binary"))
            self.assertEqual(branch_before, self.git_output(root, "symbolic-ref", "--short", "HEAD"))
            self.assertEqual(worktrees_before, self.git_output(root, "worktree", "list", "--porcelain"))
            self.assertEqual(admin_before, self.worktree_admin_entries(root))

    def test_git_plan_nested_target_named_excluded_component_is_processed(self):
        for component in ("build", "target", "node_modules"):
            with self.subTest(component=component):
                with tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    target = root / component / "demo"
                    (target / "src").mkdir(parents=True)
                    (target / "src" / "App.java").write_text(
                        "import javax.annotation.PostConstruct;\n", encoding="utf-8"
                    )
                    self.initialize_git(root)

                    report = plan(self._regex_spec(), target)

                    self.assertEqual(["src/App.java"], report.changed_files)

    def test_git_plan_runs_command_and_verify_in_worktree_context(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "source.txt").write_text("old\n", encoding="utf-8")
            spec_data = {
                "schema_version": 1,
                "name": "git-context",
                "steps": [{
                    "type": "command",
                    "name": "git-command",
                    "command": [
                        sys.executable,
                        "-c",
                        "import subprocess; raise SystemExit(0 if subprocess.check_output(['git', 'rev-parse', '--is-inside-work-tree'], text=True).strip() == 'true' else 1)",
                    ],
                }, {
                    "type": "regex",
                    "name": "replace",
                    "includes": ["*.txt"],
                    "pattern": "old",
                    "replacement": "new",
                }],
                "verify": [[
                    sys.executable,
                    "-c",
                    "import subprocess; raise SystemExit(0 if subprocess.check_output(['git', 'rev-parse', '--is-inside-work-tree'], text=True).strip() == 'true' else 1)",
                ]],
            }
            spec_path = root / "spec.json"
            spec_path.write_text(json.dumps(spec_data), encoding="utf-8")
            self.initialize_git(root)

            report = plan(load_spec(spec_path), root, allow_commands=True)

            self.assertEqual(["source.txt"], report.changed_files)
            self.assertEqual(1, len(report.verification))
            self.assertTrue(report.verification[0].endswith(": PASS"))

    def test_git_plan_nested_target_uses_nested_worktree_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "packages" / "demo"
            (target / "src").mkdir(parents=True)
            (target / "src" / "App.java").write_text(
                "import javax.annotation.PostConstruct;\n", encoding="utf-8"
            )
            spec = load_spec(self._write_spec(root, {
                "schema_version": 1,
                "name": "nested",
                "steps": [{
                    "type": "regex", "name": "imports", "includes": ["src/*.java"],
                    "pattern": r"\bjavax\.annotation\.", "replacement": "jakarta.annotation.",
                }],
            }))
            self.initialize_git(root)
            worktrees_before = self.git_output(root, "worktree", "list", "--porcelain")
            report = plan(spec, target)
            self.assertEqual(["src/App.java"], report.changed_files)
            self.assertIn("javax.annotation", (target / "src" / "App.java").read_text())
            self.assertEqual(worktrees_before, self.git_output(root, "worktree", "list", "--porcelain"))

    def _write_spec(self, root: Path, raw) -> Path:
        path = root / "nested-spec.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return path

    def test_git_plan_cleans_worktree_after_verification_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spec = self.make_repo(root)
            spec.verify = [[sys.executable, "-c", "raise SystemExit(1)"]]
            self.initialize_git(root)
            worktrees_before = self.git_output(root, "worktree", "list", "--porcelain")
            admin_before = self.worktree_admin_entries(root)
            with self.assertRaisesRegex(RuntimeError, "Verification failed"):
                plan(spec, root)
            self.assertEqual(worktrees_before, self.git_output(root, "worktree", "list", "--porcelain"))
            self.assertEqual(admin_before, self.worktree_admin_entries(root))

    def test_git_plan_cleans_worktree_after_transformation_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "source.txt").write_text("source\n", encoding="utf-8")
            self.initialize_git(root)
            spec = TransformationSpec("failure", "", [ExplodingTransformation()])
            worktrees_before = self.git_output(root, "worktree", "list", "--porcelain")
            admin_before = self.worktree_admin_entries(root)
            with self.assertRaisesRegex(RuntimeError, "intentional transformation failure"):
                plan(spec, root)
            self.assertEqual(worktrees_before, self.git_output(root, "worktree", "list", "--porcelain"))
            self.assertEqual(admin_before, self.worktree_admin_entries(root))

    def test_unborn_git_plan_uses_copy_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spec = self.make_repo(root)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            worktrees_before = self.git_output(root, "worktree", "list", "--porcelain")
            report = plan(spec, root)
            self.assertEqual(["src/App.java"], report.changed_files)
            self.assertIn("javax.annotation", (root / "src" / "App.java").read_text())
            self.assertEqual(worktrees_before, self.git_output(root, "worktree", "list", "--porcelain"))
            self.assertEqual([], self.worktree_admin_entries(root))

    def test_apply_requires_clean_git_and_changes_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spec = self.make_repo(root)
            self.initialize_git(root)
            report = apply(spec, root)
            self.assertEqual(["src/App.java"], report.changed_files)
            self.assertIn("jakarta.annotation", (root / "src" / "App.java").read_text())
            self.assertEqual(["python3 verify.py: PASS"], report.verification)


    def _regex_spec(self, includes=None) -> TransformationSpec:
        return TransformationSpec(
            "regex",
            "",
            [RegexTransformation(
                "imports",
                includes or ["**/*.java"],
                r"\bjavax\.annotation\.",
                "jakarta.annotation.",
            )],
        )

    def test_git_plan_symlink_escape_keeps_external_target_byte_identical(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repo"
            repository.mkdir()
            outside = root / "outside.java"
            outside.write_bytes(b"import javax.annotation.PostConstruct;\n")
            (repository / "src").mkdir()
            os.symlink(outside, repository / "src" / "Linked.java")
            (repository / "keep.txt").write_text("keep\n", encoding="utf-8")
            self.initialize_git(repository)
            before = outside.read_bytes()

            report = plan(self._regex_spec(), repository)

            self.assertEqual([], report.changed_files)
            self.assertEqual(before, outside.read_bytes())

    def test_git_plan_commands_are_isolated_from_source_refs_config_and_remotes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "source.txt").write_text("source\n", encoding="utf-8")
            self.initialize_git(root)
            source_git = (root / ".git").resolve()
            command_code = (
                "import pathlib, subprocess; "
                f"common=pathlib.Path(subprocess.check_output(['git','rev-parse','--git-common-dir'], text=True).strip()).resolve(); "
                f"assert common != pathlib.Path({str(source_git)!r}); "
                "subprocess.run(['git','branch','sandbox-only'], check=True); "
                "subprocess.run(['git','config','--local','sandbox.key','value'], check=True); "
                "subprocess.run(['git','remote','add','sandbox-remote','/tmp/no-such-source'], check=True)"
            )
            verify_code = (
                "import subprocess; "
                "assert subprocess.check_output(['git','config','--local','sandbox.key'], text=True).strip() == 'value'; "
                "assert subprocess.check_output(['git','branch','--list','sandbox-only'], text=True).strip()"
            )
            spec = TransformationSpec(
                "git-isolation",
                "",
                [CommandTransformation("mutate-git", [sys.executable, "-c", command_code])],
                [[sys.executable, "-c", verify_code]],
            )
            refs_before = self.git_output(root, "for-each-ref", "--format=%(refname):%(objectname)")
            config_before = self.git_output(root, "config", "--local", "--list")
            remotes_before = self.git_output(root, "remote", "-v")

            report = plan(spec, root, allow_commands=True)

            self.assertEqual(1, len(report.verification))
            self.assertEqual(refs_before, self.git_output(root, "for-each-ref", "--format=%(refname):%(objectname)"))
            self.assertEqual(config_before, self.git_output(root, "config", "--local", "--list"))
            self.assertEqual(remotes_before, self.git_output(root, "remote", "-v"))
            self.assertNotIn("sandbox-only", self.git_output(root, "branch", "--list"))

    def test_git_plan_does_not_run_source_hooks_or_smudge_filters(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            marker = root / "side-effect-marker"
            smudge_script = root / "smudge.py"
            smudge_script.write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('smudge')\n"
                "import sys\nsys.stdout.write(sys.stdin.read())\n",
                encoding="utf-8",
            )
            (root / ".gitattributes").write_text("*.txt filter=forge-smudge\n", encoding="utf-8")
            (root / "source.txt").write_text("source\n", encoding="utf-8")
            self.initialize_git(root)
            hook = root / ".git" / "hooks" / "post-checkout"
            hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
            hook.chmod(0o755)
            subprocess.run(["git", "config", "filter.forge-smudge.clean", "cat"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "filter.forge-smudge.smudge", f"{sys.executable} {smudge_script}"],
                cwd=root,
                check=True,
            )

            plan(self._regex_spec(), root)

            self.assertFalse(marker.exists())

    def test_git_environment_pollution_is_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repository"
            other = Path(temp) / "other"
            root.mkdir()
            other.mkdir()
            spec = self.make_repo(root)
            self.initialize_git(root)
            (other / "other.txt").write_text("other\n", encoding="utf-8")
            self.initialize_git(other)
            with unittest.mock.patch.dict(
                os.environ,
                {
                    "GIT_DIR": str(other / ".git"),
                    "GIT_WORK_TREE": str(other),
                    "GIT_INDEX_FILE": str(other / ".git" / "wrong-index"),
                },
                clear=False,
            ):
                report = plan(spec, root)
            self.assertEqual(["src/App.java"], report.changed_files)

    def test_git_plan_uncommitted_target_falls_back_without_git_context(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "committed.txt").write_text("committed\n", encoding="utf-8")
            self.initialize_git(root)
            target = root / "newmod"
            (target / "src").mkdir(parents=True)
            source = target / "src" / "App.java"
            source.write_text("import javax.annotation.PostConstruct;\n", encoding="utf-8")
            worktrees_before = self.git_output(root, "worktree", "list", "--porcelain")

            report = plan(self._regex_spec(), target)

            self.assertEqual(["src/App.java"], report.changed_files)
            self.assertIn("without Git context", " ".join(report.messages))
            self.assertIn("javax.annotation", source.read_text(encoding="utf-8"))
            self.assertEqual(worktrees_before, self.git_output(root, "worktree", "list", "--porcelain"))

    def test_head_plan_warns_when_untracked_or_ignored_files_are_excluded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "src").mkdir()
            (root / "src" / "App.java").write_text("class App {}\n", encoding="utf-8")
            (root / ".gitignore").write_text("src/Generated.java\n", encoding="utf-8")
            self.initialize_git(root)
            (root / "src" / "Untracked.java").write_text("javax.annotation.X\n", encoding="utf-8")
            (root / "src" / "Generated.java").write_text("javax.annotation.X\n", encoding="utf-8")

            report = plan(self._regex_spec(), root)

            self.assertEqual([], report.changed_files)
            messages = " ".join(report.messages).lower()
            self.assertIn("untracked", messages)
            self.assertIn("ignored", messages)

    def test_working_tree_plan_includes_populated_submodule_without_source_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "super"
            library = Path(temp) / "library"
            root.mkdir()
            library.mkdir()
            (library / "Lib.java").write_text("javax.annotation.X\n", encoding="utf-8")
            self.initialize_git(library)
            (root / "README").write_text("super\n", encoding="utf-8")
            self.initialize_git(root)
            subprocess.run(
                ["git", "-c", "protocol.file.allow=always", "submodule", "add", str(library), "vendor/library"],
                cwd=root,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "submodule"],
                cwd=root,
                check=True,
            )

            head_report = plan(self._regex_spec(), root)
            working_report = plan(self._regex_spec(), root, snapshot="working-tree")

            self.assertEqual([], head_report.changed_files)
            self.assertTrue(any("submodule" in message.lower() for message in head_report.messages))
            self.assertEqual(["vendor/library/Lib.java"], working_report.changed_files)
            self.assertIn("javax.annotation", (library / "Lib.java").read_text(encoding="utf-8"))

    def test_cleanup_warning_is_attached_to_completed_report(self):
        from refactor_forge import engine

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spec = self.make_repo(root)
            self.initialize_git(root)
            original_cleanup = engine._cleanup_temporary_directory

            def cleanup_with_warning(temporary, temporary_root):
                errors = original_cleanup(temporary, temporary_root)
                return errors + ["simulated cleanup warning"]

            with unittest.mock.patch.object(engine, "_cleanup_temporary_directory", cleanup_with_warning):
                report = plan(spec, root)

            self.assertIn("simulated cleanup warning", " ".join(report.messages))

    def test_apply_rejects_dirty_tree(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spec = self.make_repo(root)
            self.initialize_git(root)
            (root / "unrelated.txt").write_text("dirty\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "Working tree is dirty"):
                apply(spec, root)

    def test_apply_accepts_nested_target(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "packages" / "demo"
            (target / "src").mkdir(parents=True)
            (target / "src" / "App.java").write_text("javax.annotation.X\n", encoding="utf-8")
            spec = self._regex_spec(["src/*.java"])
            self.initialize_git(root)

            report = apply(spec, target)

            self.assertEqual(["src/App.java"], report.changed_files)
            self.assertIn("jakarta.annotation", (target / "src" / "App.java").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

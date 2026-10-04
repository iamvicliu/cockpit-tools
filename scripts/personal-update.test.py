"""Safety regressions. Local Git repositories and fake app bundles only."""
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("updater", Path(__file__).with_name("personal-update.py"))
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


class SafetyTests(unittest.TestCase):
    def setUp(self):
        # Keep evidence: this suite never deletes its temporary repositories.
        test_root = updater.DEFAULT_ROOT / "tests"
        test_root.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="cockpit-update-test-", dir=test_root))
        self.repo = self.root / "fixture"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Update Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "sidecars/cockpit-cliproxy").mkdir(parents=True)
        (self.repo / "sidecars/cockpit-cliproxy/source.go").write_text("baseline\n")
        (self.repo / "src-tauri").mkdir()
        (self.repo / "src-tauri/build.rs").write_text("baseline\n")
        (self.repo / "feature.txt").write_text("base\n")
        (self.repo / "scripts").mkdir()
        (self.repo / "scripts/personal-update.py").write_text("# fixture updater\n")
        self.commit("baseline")
        self.git("tag", "v1.0.0")
        self.git("switch", "-c", "personal/cockpit")
        (self.repo / "personal.txt").write_text("reset credit feature\n")
        self.commit("personal feature")
        self.personal_sha = self.git("rev-parse", "HEAD")
        self.fork = self.root / "fork.git"
        self.command("git", "clone", "--bare", str(self.repo), str(self.fork))
        self.git("switch", "main")

    def command(self, *args):
        return subprocess.check_output(args, cwd=self.repo, text=True, stderr=subprocess.DEVNULL).strip()

    def git(self, *args):
        return self.command("git", *args)

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "-m", message)

    def remote_sha(self):
        return self.command("git", "--git-dir", str(self.fork), "rev-parse", "personal/cockpit")

    def build_fixture(self, fail_test=False):
        real_run = updater.run

        def run(args, cwd=None, env=None, log=None):
            if args[0] == "git":
                if args[1] == "merge":
                    # Repo-local identity, no changes to global Git config.
                    real_run(["git", "config", "user.name", "Update Test"], cwd=cwd)
                    real_run(["git", "config", "user.email", "test@example.invalid"], cwd=cwd)
                return real_run(args, cwd=cwd, env=env, log=log)
            if args[:2] == ["npm", "test"] and fail_test:
                raise RuntimeError("fixture test failure")
            if args[0] == "xcrun":
                return "/fixture/sdk"
            if args[0] == "node":
                app = Path(env["CARGO_TARGET_DIR"]) / "debug/bundle/macos/Cockpit Tools.app"
                (app / "Contents/MacOS").mkdir(parents=True)
                (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
                    "CFBundleIdentifier": updater.IDENTIFIER, "CFBundleExecutable": "cockpit-tools"}))
                (app / "Contents/MacOS/cockpit-tools").write_text("fixture executable")
            if args[0] == "ditto":
                import shutil
                shutil.copytree(args[1], args[2])
            return ""

        return run

    def prepare(self, fail_test=False):
        with patch.object(updater, "FORK", str(self.fork)), \
             patch.object(updater, "UPSTREAM", str(self.repo)), \
             patch.object(updater, "release", return_value={"tagName": "v1.1.0"}), \
             patch.object(updater.shutil, "which", return_value="/fixture/tool"), \
             patch.object(updater, "run", side_effect=self.build_fixture(fail_test)):
            return updater.prepare(self.root / "maintenance", None, "build")

    def test_update_merges_stable_release_and_preserves_personal_change(self):
        (self.repo / "upstream.txt").write_text("upstream release")
        self.commit("upstream release")
        self.git("tag", "v1.1.0")
        manifest = self.prepare()
        source = Path(manifest["source"])
        self.assertEqual((source / "personal.txt").read_text(), "reset credit feature\n")
        self.assertTrue((source / "upstream.txt").exists())
        self.assertEqual(self.remote_sha(), manifest["build"]["revision"])
        self.assertNotEqual(self.remote_sha(), self.personal_sha)
        self.assertFalse((self.root / "maintenance/backups").exists())

    def test_failed_tests_never_publish_candidate_or_install(self):
        (self.repo / "upstream.txt").write_text("new")
        self.commit("upstream release")
        self.git("tag", "v1.1.0")
        with self.assertRaisesRegex(RuntimeError, "fixture test failure"):
            self.prepare(fail_test=True)
        self.assertEqual(self.remote_sha(), self.personal_sha)
        self.assertFalse((self.root / "maintenance/latest-build.json").exists())

    def test_merge_conflict_preserves_git_evidence_and_remote_branch(self):
        self.git("switch", "personal/cockpit")
        (self.repo / "feature.txt").write_text("personal\n")
        self.commit("personal conflict")
        self.git("push", str(self.fork), "personal/cockpit")
        before = self.remote_sha()
        self.git("switch", "main")
        (self.repo / "feature.txt").write_text("upstream\n")
        self.commit("upstream conflict")
        self.git("tag", "v1.1.0")
        with self.assertRaises(RuntimeError):
            self.prepare()
        self.assertEqual(self.remote_sha(), before)
        source = next((self.root / "maintenance/runs").glob("*/source"))
        self.assertTrue((source / ".git/MERGE_HEAD").exists())

    def test_changed_sidecar_source_refuses_reuse(self):
        metadata = {"machine": updater.platform.machine(), "inputs": updater.sidecar_inputs(self.repo), "files": []}
        (self.repo / "src-tauri/build.rs").write_text("changed\n")
        self.commit("build changed")
        with self.assertRaisesRegex(RuntimeError, "禁止复用"):
            updater.validate_cache(self.repo, metadata)

    def test_modified_cache_refuses_reuse(self):
        binary = self.root / "cache.bin"
        binary.write_bytes(b"original")
        metadata = {"machine": updater.platform.machine(), "inputs": updater.sidecar_inputs(self.repo),
                    "files": [{"path": str(binary), "sha256": updater.digest(binary)}]}
        binary.write_bytes(b"modified")
        with self.assertRaisesRegex(RuntimeError, "缓存校验失败"):
            updater.validate_cache(self.repo, metadata)

    def test_failed_swap_restores_old_app_and_keeps_candidate(self):
        staged, target, backup = [self.root / n for n in ("staged", "target", "backup")]
        staged.mkdir()
        target.mkdir()
        (target / "old").write_text("old")
        real_rename = os.rename

        def rename(a, b):
            if a == staged:
                raise OSError("fixture rename failure")
            return real_rename(a, b)

        with patch.object(updater.os, "rename", side_effect=rename):
            with self.assertRaises(OSError):
                updater.swap_bundle(staged, target, backup)
        self.assertTrue((target / "old").exists())
        self.assertTrue(staged.exists())

    def test_successful_swap_preserves_complete_previous_app(self):
        staged, target, backup = [self.root / n for n in ("staged", "target", "backup")]
        staged.mkdir()
        target.mkdir()
        (staged / "new").write_text("new")
        (target / "old").write_text("old")
        updater.swap_bundle(staged, target, backup)
        self.assertTrue((target / "new").exists())
        self.assertTrue((backup / "old").exists())


if __name__ == "__main__":
    unittest.main()

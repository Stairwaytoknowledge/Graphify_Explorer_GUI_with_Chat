"""Tests for graphify_clone (partial+sparse clone + cache management).

Run from repo root:

    .venv\\Scripts\\python -m pytest tests/test_clone.py -v   (Windows)
    .venv/bin/python      -m pytest tests/test_clone.py -v   (Mac/Linux)

Or without pytest installed:

    .venv\\Scripts\\python tests/test_clone.py

Network tests are off by default. Set GRAPHIFY_TEST_NETWORK=1 to enable
the real-clone integration test (clones a tiny public repo from GitHub).
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import graphify_clone as gc  # noqa: E402


class DeriveCloneDestTest(unittest.TestCase):
    def test_https_github_url(self) -> None:
        d = gc.derive_clone_dest("https://github.com/foo/bar")
        self.assertEqual(d.parts[-2:], ("foo", "bar"))

    def test_https_github_dot_git(self) -> None:
        d = gc.derive_clone_dest("https://github.com/foo/bar.git")
        self.assertEqual(d.parts[-2:], ("foo", "bar"))

    def test_scp_style(self) -> None:
        d = gc.derive_clone_dest("git@github.com:foo/bar.git")
        self.assertEqual(d.parts[-2:], ("foo", "bar"))

    def test_internal_gitlab(self) -> None:
        d = gc.derive_clone_dest("https://git.corp.example/team/proj.git")
        self.assertEqual(d.parts[-2:], ("team", "proj"))

    def test_ssh_url(self) -> None:
        d = gc.derive_clone_dest("ssh://git@host.example/team/proj.git")
        self.assertEqual(d.parts[-2:], ("team", "proj"))

    def test_unknown_shape_does_not_crash(self) -> None:
        # Not a real git URL but the helper must not throw.
        d = gc.derive_clone_dest("notaurl")
        self.assertTrue(str(d))

    def test_under_cache_root(self) -> None:
        d = gc.derive_clone_dest("https://github.com/foo/bar")
        self.assertTrue(
            str(d).startswith(str(gc.cache_root())),
            f"{d} not under {gc.cache_root()}",
        )


class GitVersionTest(unittest.TestCase):
    def test_supports_old(self) -> None:
        v = gc.GitVersion(2, 18, 0)
        self.assertFalse(v.supports((2, 19)))
        self.assertFalse(v.supports((2, 25)))

    def test_supports_modern(self) -> None:
        v = gc.GitVersion(2, 40, 1)
        self.assertTrue(v.supports((2, 19)))
        self.assertTrue(v.supports((2, 25)))

    def test_supports_exact_threshold(self) -> None:
        v = gc.GitVersion(2, 19, 0)
        self.assertTrue(v.supports((2, 19)))
        self.assertFalse(v.supports((2, 25)))

    def test_supports_major_bump(self) -> None:
        v = gc.GitVersion(3, 0, 0)
        self.assertTrue(v.supports((2, 19)))
        self.assertTrue(v.supports((2, 25)))


class BuildCommandsTest(unittest.TestCase):
    URL = "https://github.com/foo/bar.git"
    DEST = Path("/tmp/foo/bar")

    def test_old_git_falls_back_to_shallow(self) -> None:
        v = gc.GitVersion(2, 17, 0)
        cmds = gc.build_partial_sparse_commands(self.URL, self.DEST, git_version=v)
        self.assertEqual(len(cmds), 1)
        self.assertIn("--depth", cmds[0])
        self.assertNotIn("--filter=blob:none", cmds[0])

    def test_modern_git_uses_partial_and_sparse(self) -> None:
        v = gc.GitVersion(2, 40, 0)
        cmds = gc.build_partial_sparse_commands(self.URL, self.DEST, git_version=v)
        # clone, sparse-checkout init, sparse-checkout set, checkout
        self.assertEqual(len(cmds), 4)
        self.assertIn("--filter=blob:none", cmds[0])
        self.assertIn("--no-checkout", cmds[0])
        self.assertEqual(cmds[1][3:6], ["sparse-checkout", "init", "--no-cone"])
        self.assertEqual(cmds[2][3:5], ["sparse-checkout", "set"])
        self.assertEqual(cmds[3][-1], "checkout")
        # Patterns include common languages.
        patterns = cmds[2][5:]
        self.assertIn("*.py", patterns)
        self.assertIn("*.js", patterns)
        self.assertIn("*.go", patterns)
        self.assertIn("*.ts", patterns)

    def test_middle_git_uses_partial_with_manual_pattern(self) -> None:
        # 2.19 <= git < 2.25: filter is supported but porcelain isn't.
        v = gc.GitVersion(2, 22, 0)
        cmds = gc.build_partial_sparse_commands(self.URL, self.DEST, git_version=v)
        self.assertIn("--filter=blob:none", cmds[0])
        # No porcelain sparse-checkout subcommand should appear.
        for c in cmds:
            self.assertNotIn("sparse-checkout", c)
        # core.sparseCheckout config must be set.
        config_steps = [c for c in cmds if "config" in c]
        self.assertTrue(config_steps, "expected core.sparseCheckout config")
        self.assertEqual(config_steps[0][-2:], ["core.sparseCheckout", "true"])

    def test_custom_extension_list(self) -> None:
        v = gc.GitVersion(2, 40, 0)
        cmds = gc.build_partial_sparse_commands(
            self.URL, self.DEST, extensions=("py",), git_version=v,
        )
        # Only *.py should be in the patterns.
        patterns = cmds[2][5:]
        self.assertEqual(patterns, ["*.py"])


class CacheManagementTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="gxclone-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_cache(self) -> None:
        empty = self.tmp / "empty"
        self.assertEqual(gc.cache_size_bytes(empty), 0)
        self.assertEqual(gc.cache_repo_count(empty), 0)
        self.assertEqual(gc.clear_cache(empty), 0)

    def test_count_and_size(self) -> None:
        # Populate two fake repos.
        for owner, repo in [("alice", "one"), ("bob", "two")]:
            d = self.tmp / owner / repo
            d.mkdir(parents=True)
            (d / "f.txt").write_bytes(b"x" * 100)
        self.assertEqual(gc.cache_repo_count(self.tmp), 2)
        self.assertGreaterEqual(gc.cache_size_bytes(self.tmp), 200)

    def test_clear_removes_everything(self) -> None:
        d = self.tmp / "owner" / "repo"
        d.mkdir(parents=True)
        (d / "f.bin").write_bytes(b"x" * 5000)
        freed = gc.clear_cache(self.tmp)
        self.assertGreaterEqual(freed, 5000)
        self.assertFalse(self.tmp.exists())

    def test_clear_safe_on_missing(self) -> None:
        # Should not raise even if the path doesn't exist.
        nope = self.tmp / "does" / "not" / "exist"
        gc.clear_cache(nope)

    def test_clear_removes_readonly_files(self) -> None:
        # Mimics .git/objects/pack files which Windows git marks
        # read-only. The old implementation used ignore_errors=True
        # and silently left these behind, breaking the next clone.
        import stat
        d = self.tmp / "owner" / "repo" / ".git" / "objects" / "pack"
        d.mkdir(parents=True)
        f = d / "pack-deadbeef.idx"
        f.write_bytes(b"\x00" * 256)
        os.chmod(f, stat.S_IREAD)
        try:
            freed = gc.clear_cache(self.tmp)
            self.assertGreaterEqual(freed, 256)
            self.assertFalse(self.tmp.exists(),
                             "read-only files left undeleted")
        finally:
            # If the test failed mid-flight, restore +w so tearDown can clean.
            if f.exists():
                os.chmod(f, stat.S_IWRITE)


class ForceRmtreeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="gxrm-"))

    def tearDown(self) -> None:
        # Belt-and-suspenders: tests should clean themselves up, but
        # if they didn't, force the cleanup with mode-fix.
        if self.tmp.exists():
            gc.force_rmtree(self.tmp)

    def test_returns_true_when_missing(self) -> None:
        nope = self.tmp / "missing"
        self.assertTrue(gc.force_rmtree(nope))

    def test_removes_normal_dir(self) -> None:
        d = self.tmp / "x"
        d.mkdir()
        (d / "a.txt").write_text("hi")
        self.assertTrue(gc.force_rmtree(d))
        self.assertFalse(d.exists())

    def test_removes_readonly_file(self) -> None:
        import stat
        d = self.tmp / "x"
        d.mkdir()
        f = d / "ro.txt"
        f.write_text("hi")
        os.chmod(f, stat.S_IREAD)
        try:
            self.assertTrue(gc.force_rmtree(d))
            self.assertFalse(d.exists())
        finally:
            if f.exists():
                os.chmod(f, stat.S_IWRITE)


class HasWorkingTreeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="gxwt-"))

    def tearDown(self) -> None:
        gc.force_rmtree(self.tmp)

    def test_no_dot_git(self) -> None:
        # Plain dir with files but no .git -> not a repo at all.
        (self.tmp / "a.py").write_text("x")
        self.assertFalse(gc.has_working_tree(self.tmp))

    def test_only_dot_git_no_working_tree(self) -> None:
        # The half-clone case: --no-checkout left only .git.
        (self.tmp / ".git").mkdir()
        self.assertFalse(gc.has_working_tree(self.tmp))

    def test_dot_git_plus_files(self) -> None:
        # Real repo with checked-out files.
        (self.tmp / ".git").mkdir()
        (self.tmp / "main.py").write_text("print(1)")
        self.assertTrue(gc.has_working_tree(self.tmp))

    def test_dot_git_plus_subdir(self) -> None:
        # A subdir at the top level still counts as working tree.
        (self.tmp / ".git").mkdir()
        (self.tmp / "src").mkdir()
        self.assertTrue(gc.has_working_tree(self.tmp))


class FormatSizeTest(unittest.TestCase):
    def test_bytes(self) -> None:
        self.assertEqual(gc.format_size(0), "0 B")
        self.assertEqual(gc.format_size(512), "512 B")

    def test_kilobytes(self) -> None:
        self.assertTrue(gc.format_size(2048).endswith("KB"))

    def test_megabytes(self) -> None:
        self.assertTrue(gc.format_size(5 * 1024 * 1024).endswith("MB"))

    def test_gigabytes(self) -> None:
        self.assertTrue(gc.format_size(3 * 1024 ** 3).endswith("GB"))


class WriteSparsePatternsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="gxsparse-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_writes_patterns(self) -> None:
        gc.write_sparse_patterns(self.tmp, extensions=("py", "js"))
        body = (self.tmp / ".git" / "info" / "sparse-checkout").read_text()
        self.assertIn("*.py", body)
        self.assertIn("*.js", body)

    def test_creates_info_dir(self) -> None:
        gc.write_sparse_patterns(self.tmp, extensions=("py",))
        self.assertTrue((self.tmp / ".git" / "info").is_dir())


@unittest.skipUnless(
    os.environ.get("GRAPHIFY_TEST_NETWORK"),
    "Set GRAPHIFY_TEST_NETWORK=1 to enable real-clone tests.",
)
class IntegrationTest(unittest.TestCase):
    """End-to-end clone of a tiny public repo. Off by default."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="gxint-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_real_partial_clone(self) -> None:
        # Use a tiny public repo. octocat/Hello-World is only a few KB.
        url = "https://github.com/octocat/Hello-World.git"
        dest = self.tmp / "octocat" / "Hello-World"
        cmds = gc.build_partial_sparse_commands(url, dest)
        for cmd in cmds:
            cwd = (
                dest.parent if cmd[:2] == ["git", "clone"]
                else cmd[2] if cmd[:2] == ["git", "-C"] else dest
            )
            cwd_path = Path(cwd)
            cwd_path.mkdir(parents=True, exist_ok=True)
            import subprocess
            r = subprocess.run(
                cmd, cwd=str(cwd_path),
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(
                r.returncode, 0,
                f"step failed: {cmd}\n{r.stderr}",
            )
        # README is the only meaningful file in Hello-World; under our
        # extension allow-list it won't materialize but the clone must
        # complete cleanly.
        self.assertTrue((dest / ".git").is_dir())


if __name__ == "__main__":
    unittest.main(verbosity=2)

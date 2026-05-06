"""Safety tests for graphify_remote (read-only mirror flow).

The most important test here is TripwireTest. It runs the full mirror
flow with PathGuard installed and verifies that no write of any kind
reaches the source directory, even when graphify-style downstream code
tries to write into what looks like the source path.

Run from repo root:

    .venv\\Scripts\\python -m unittest -v tests.test_remote   (Windows)
    .venv/bin/python      -m unittest -v tests.test_remote   (Mac/Linux)
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import graphify_remote as gr  # noqa: E402


class IsNetworkPathTest(unittest.TestCase):
    def test_unc_windows(self) -> None:
        self.assertTrue(gr.is_network_path(r"\\server\share\code"))

    def test_double_slash_posix(self) -> None:
        self.assertTrue(gr.is_network_path("//server/share/code"))

    def test_local_paths_are_not_network(self) -> None:
        self.assertFalse(gr.is_network_path("C:/Code/foo"))
        self.assertFalse(gr.is_network_path("./repo"))
        self.assertFalse(gr.is_network_path("/home/x/repo"))

    def test_linux_mnt(self) -> None:
        if sys.platform.startswith("linux"):
            self.assertTrue(gr.is_network_path("/mnt/share/code"))
            self.assertTrue(gr.is_network_path("/media/share/code"))

    def test_mac_volumes(self) -> None:
        if sys.platform == "darwin":
            self.assertTrue(gr.is_network_path("/Volumes/share/code"))

    def test_empty(self) -> None:
        self.assertFalse(gr.is_network_path(""))
        self.assertFalse(gr.is_network_path("   "))


class MirrorIdTest(unittest.TestCase):
    def test_stable(self) -> None:
        a = gr.mirror_id(Path("/tmp/foo"))
        b = gr.mirror_id(Path("/tmp/foo"))
        self.assertEqual(a, b)

    def test_unique_per_source(self) -> None:
        a = gr.mirror_id(Path("/tmp/foo"))
        b = gr.mirror_id(Path("/tmp/bar"))
        self.assertNotEqual(a, b)


class _Fixture(unittest.TestCase):
    """Base: builds a fake remote tree with a mix of sources + non-sources."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="gxremote-"))
        self.src = self.tmp / "fake_remote"
        self.src.mkdir()
        # Source files (should mirror).
        (self.src / "main.py").write_text("def f(): return 1\n")
        (self.src / "util.js").write_text("export const x = 1;\n")
        (self.src / "doc.md").write_text("# nope, docs\n")
        sub = self.src / "lib"
        sub.mkdir()
        (sub / "a.py").write_text("import os\n")
        (sub / "binary.bin").write_bytes(b"\x00" * 4096)
        # Excluded directory.
        nm = self.src / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        (nm / "lib.js").write_text("// should be skipped\n")
        # Build dir.
        b = self.src / "build"
        b.mkdir()
        (b / "out.py").write_text("# build artifact\n")
        # Output dir to ensure we don't recurse into existing graphify-out.
        out = self.src / "graphify-out"
        out.mkdir()
        (out / "graph.json").write_text("{}")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


class EstimateMirrorSizeTest(_Fixture):
    def test_counts_only_source_files(self) -> None:
        files, bytes_ = gr.estimate_mirror_size(
            self.src,
            gr.MirrorPolicy(extensions=("py", "js")),
        )
        # main.py + util.js + lib/a.py = 3 files. node_modules/* and
        # build/*.py are pruned by SKIP_DIRS.
        self.assertEqual(files, 3)
        self.assertGreater(bytes_, 0)

    def test_excludes_md(self) -> None:
        files, _ = gr.estimate_mirror_size(
            self.src,
            gr.MirrorPolicy(extensions=("py",)),
        )
        # .py files only: main.py + lib/a.py
        self.assertEqual(files, 2)


class BuildMirrorTest(_Fixture):
    def setUp(self) -> None:
        super().setUp()
        # Redirect mirror_root so we don't pollute the user's real home.
        self._orig_root = gr.mirror_root
        self._fake_root = self.tmp / "mirror-home"
        gr.mirror_root = lambda: self._fake_root  # type: ignore[assignment]

    def tearDown(self) -> None:
        gr.mirror_root = self._orig_root  # type: ignore[assignment]
        super().tearDown()

    def test_requires_consent(self) -> None:
        with self.assertRaises(ValueError):
            gr.build_mirror(self.src)  # default consent=False

    def test_copies_only_source_files(self) -> None:
        report = gr.build_mirror(
            self.src,
            gr.MirrorPolicy(
                extensions=("py", "js"),
                user_consented=True,
            ),
        )
        self.assertIsNone(report.aborted_reason)
        self.assertEqual(report.files_copied, 3)
        self.assertGreater(report.bytes_copied, 0)
        # The mirror tree.
        self.assertTrue((report.mirror / "main.py").exists())
        self.assertTrue((report.mirror / "lib" / "a.py").exists())
        self.assertTrue((report.mirror / "util.js").exists())
        # Skipped: docs, binaries, node_modules, build, graphify-out.
        self.assertFalse((report.mirror / "doc.md").exists())
        self.assertFalse((report.mirror / "lib" / "binary.bin").exists())
        self.assertFalse((report.mirror / "node_modules").exists())
        self.assertFalse((report.mirror / "build").exists())
        self.assertFalse((report.mirror / "graphify-out").exists())

    def test_size_cap_aborts(self) -> None:
        report = gr.build_mirror(
            self.src,
            gr.MirrorPolicy(
                extensions=("py", "js"),
                max_total_bytes=10,  # 10 bytes - we have more
                user_consented=True,
            ),
        )
        self.assertIsNotNone(report.aborted_reason)
        self.assertEqual(report.files_copied, 0)

    def test_file_count_cap_aborts(self) -> None:
        report = gr.build_mirror(
            self.src,
            gr.MirrorPolicy(
                extensions=("py", "js"),
                max_files=1,  # we have 3
                user_consented=True,
            ),
        )
        self.assertIsNotNone(report.aborted_reason)
        self.assertEqual(report.files_copied, 0)

    def test_per_file_size_cap(self) -> None:
        # Make one .py file big.
        big = self.src / "huge.py"
        big.write_bytes(b"# " + b"x" * 4096)
        report = gr.build_mirror(
            self.src,
            gr.MirrorPolicy(
                extensions=("py",),
                per_file_max_bytes=512,
                user_consented=True,
            ),
        )
        self.assertIsNone(report.aborted_reason)
        # huge.py exceeds per-file cap and is skipped.
        self.assertFalse((report.mirror / "huge.py").exists())
        self.assertGreaterEqual(report.skipped_too_big, 1)

    def test_missing_source_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            gr.build_mirror(
                self.tmp / "no-such-dir",
                gr.MirrorPolicy(user_consented=True),
            )


class PathGuardTest(_Fixture):
    def test_blocks_write_to_remote(self) -> None:
        guard = gr.PathGuard(self.src)
        with guard:
            with self.assertRaises(gr.RemoteWriteAttempt):
                with open(self.src / "main.py", "w") as f:
                    f.write("malicious")
        # Source file is untouched.
        self.assertTrue("def f(): return 1" in (self.src / "main.py").read_text())

    def test_blocks_append_to_remote(self) -> None:
        guard = gr.PathGuard(self.src)
        original = (self.src / "main.py").read_text()
        with guard:
            with self.assertRaises(gr.RemoteWriteAttempt):
                with open(self.src / "main.py", "a") as f:
                    f.write("\nappended")
        self.assertEqual((self.src / "main.py").read_text(), original)

    def test_blocks_remove(self) -> None:
        guard = gr.PathGuard(self.src)
        target = self.src / "main.py"
        with guard:
            with self.assertRaises(gr.RemoteWriteAttempt):
                os.remove(target)
        self.assertTrue(target.exists())

    def test_blocks_unlink(self) -> None:
        guard = gr.PathGuard(self.src)
        target = self.src / "main.py"
        with guard:
            with self.assertRaises(gr.RemoteWriteAttempt):
                os.unlink(target)
        self.assertTrue(target.exists())

    def test_blocks_rmdir(self) -> None:
        guard = gr.PathGuard(self.src)
        target = self.src / "lib"
        with guard:
            with self.assertRaises(gr.RemoteWriteAttempt):
                os.rmdir(target)
        self.assertTrue(target.exists())

    def test_blocks_rename(self) -> None:
        guard = gr.PathGuard(self.src)
        with guard:
            with self.assertRaises(gr.RemoteWriteAttempt):
                os.rename(self.src / "main.py", self.src / "renamed.py")
        self.assertTrue((self.src / "main.py").exists())
        self.assertFalse((self.src / "renamed.py").exists())

    def test_blocks_mkdir(self) -> None:
        guard = gr.PathGuard(self.src)
        with guard:
            with self.assertRaises(gr.RemoteWriteAttempt):
                os.mkdir(self.src / "new_dir")
        self.assertFalse((self.src / "new_dir").exists())

    def test_blocks_makedirs_into_remote(self) -> None:
        guard = gr.PathGuard(self.src)
        with guard:
            with self.assertRaises(gr.RemoteWriteAttempt):
                os.makedirs(self.src / "deep" / "nest")

    def test_allows_read_from_remote(self) -> None:
        guard = gr.PathGuard(self.src)
        with guard:
            text = (self.src / "main.py").read_text()
        self.assertIn("def f", text)

    def test_allows_writes_outside_remote(self) -> None:
        guard = gr.PathGuard(self.src)
        outside = self.tmp / "scratch"
        outside.mkdir()
        with guard:
            (outside / "ok.txt").write_text("fine")
        self.assertEqual((outside / "ok.txt").read_text(), "fine")

    def test_uninstall_restores_open(self) -> None:
        import builtins
        original = builtins.open
        guard = gr.PathGuard(self.src)
        guard.install()
        guard.uninstall()
        self.assertIs(builtins.open, original)

    def test_records_attempts(self) -> None:
        guard = gr.PathGuard(self.src)
        with guard:
            try:
                with open(self.src / "main.py", "w"):
                    pass
            except gr.RemoteWriteAttempt:
                pass
        self.assertEqual(len(guard.attempts), 1)
        self.assertIn("main.py", guard.attempts[0])


class TripwireTest(_Fixture):
    """End-to-end: full mirror flow + write-attempt check.

    Runs build_mirror inside a PathGuard. Since build_mirror only opens
    source files in 'rb' mode, no RemoteWriteAttempt should fire. If a
    future contributor adds a write-mode open against the source, this
    test catches it.
    """

    def setUp(self) -> None:
        super().setUp()
        self._orig_root = gr.mirror_root
        gr.mirror_root = lambda: self.tmp / "mirror-home"  # type: ignore[assignment]

    def tearDown(self) -> None:
        gr.mirror_root = self._orig_root  # type: ignore[assignment]
        super().tearDown()

    def test_no_writes_to_source(self) -> None:
        guard = gr.PathGuard(self.src)
        with guard:
            report = gr.build_mirror(
                self.src,
                gr.MirrorPolicy(
                    extensions=("py", "js"),
                    user_consented=True,
                ),
            )
        self.assertIsNone(report.aborted_reason)
        self.assertEqual(report.files_copied, 3)
        self.assertEqual(
            len(guard.attempts), 0,
            f"expected zero write attempts to source, got: {guard.attempts!r}",
        )

    def test_post_mirror_source_unchanged(self) -> None:
        # Snapshot mtime + content before, then again after, and confirm
        # nothing inside the source has changed.
        before = {}
        for p in self.src.rglob("*"):
            if p.is_file():
                before[p] = (p.stat().st_size, p.read_bytes())
        with gr.PathGuard(self.src):
            gr.build_mirror(
                self.src,
                gr.MirrorPolicy(
                    extensions=("py", "js"),
                    user_consented=True,
                ),
            )
        for p, (size, content) in before.items():
            self.assertTrue(p.exists(), f"source file vanished: {p}")
            self.assertEqual(p.stat().st_size, size, f"size changed: {p}")
            self.assertEqual(p.read_bytes(), content, f"content changed: {p}")


class DiscardMirrorTest(_Fixture):
    def setUp(self) -> None:
        super().setUp()
        self._orig_root = gr.mirror_root
        gr.mirror_root = lambda: self.tmp / "mirror-home"  # type: ignore[assignment]

    def tearDown(self) -> None:
        gr.mirror_root = self._orig_root  # type: ignore[assignment]
        super().tearDown()

    def test_discard_specific_source(self) -> None:
        gr.build_mirror(
            self.src,
            gr.MirrorPolicy(
                extensions=("py", "js"),
                user_consented=True,
            ),
        )
        mirror = gr.mirror_path_for(self.src)
        self.assertTrue(mirror.exists())
        freed = gr.discard_mirror(self.src)
        self.assertGreater(freed, 0)
        self.assertFalse(mirror.exists())

    def test_discard_all(self) -> None:
        gr.build_mirror(
            self.src,
            gr.MirrorPolicy(
                extensions=("py", "js"),
                user_consented=True,
            ),
        )
        n_before, _ = gr.mirror_count_and_size()
        self.assertGreaterEqual(n_before, 1)
        gr.discard_mirror()
        n_after, _ = gr.mirror_count_and_size()
        self.assertEqual(n_after, 0)

    def test_discard_safe_on_missing(self) -> None:
        # No mirror yet; should return 0 bytes freed and not raise.
        self.assertEqual(gr.discard_mirror(self.src), 0)


class RemoteShareWriteProbeTest(_Fixture):
    def test_writable_on_local_dir(self) -> None:
        # Local temp dir should report writable.
        result = gr.remote_share_is_writable(self.src)
        self.assertTrue(result)

    def test_missing_returns_none(self) -> None:
        result = gr.remote_share_is_writable(self.tmp / "no-such-dir")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)

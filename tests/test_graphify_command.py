"""Tests for graphify_command() WDAC fallback in graphify_gui."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Bootstrap venv site-packages just like graphify_gui does.
_root = Path(__file__).resolve().parent.parent
_venv_sp = _root / ".venv" / "Lib" / "site-packages"
if _venv_sp.exists() and str(_venv_sp) not in sys.path:
    sys.path.insert(0, str(_venv_sp))

import graphify_gui as gg  # noqa: E402


class GraphifyCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        # Reset the probe cache before each test so we're not picking
        # up state from a previous test's stub or monkeypatch.
        gg._GRAPHIFY_STUB_OK = None
        self._saved_probe = gg._probe_graphify_stub

    def tearDown(self) -> None:
        gg._GRAPHIFY_STUB_OK = None
        gg._probe_graphify_stub = self._saved_probe

    def test_falls_back_to_python_m_when_stub_blocked(self) -> None:
        # Simulate WDAC-blocked stub: probe returns False.
        gg._probe_graphify_stub = lambda exe: False
        gg._GRAPHIFY_STUB_OK = None
        cmd, env = gg.graphify_command(["update", "."])
        self.assertEqual(cmd[0], sys.executable)
        self.assertEqual(cmd[1:4], ["-m", "graphify", "update"])
        self.assertEqual(cmd[4], ".")
        # PYTHONPATH is set so the home-python case can still import graphifyy.
        self.assertIn("PYTHONPATH", env)

    def test_uses_stub_when_probe_passes(self) -> None:
        # Force the probe to pass.
        gg._probe_graphify_stub = lambda exe: True
        gg._GRAPHIFY_STUB_OK = None
        # Only meaningful when a stub actually exists on this machine.
        if not gg.graphify_executable():
            self.skipTest("no graphify stub present")
        cmd, env = gg.graphify_command(["query", "x"])
        self.assertTrue(cmd[0].endswith(("graphify.exe", "graphify")))
        self.assertEqual(cmd[1:], ["query", "x"])

    def test_probe_is_cached(self) -> None:
        calls = {"n": 0}
        def counting_probe(exe: str) -> bool:
            calls["n"] += 1
            return False
        gg._probe_graphify_stub = counting_probe
        gg._GRAPHIFY_STUB_OK = None
        gg.graphify_command(["update", "."])
        gg.graphify_command(["query", "x"])
        gg.graphify_command(["explain", "y"])
        # If the stub exists, we probe at most once across N calls.
        if gg.graphify_executable():
            self.assertEqual(calls["n"], 1)

    def test_args_default_empty(self) -> None:
        # No args means no positional arguments passed to graphify.
        gg._probe_graphify_stub = lambda exe: False
        gg._GRAPHIFY_STUB_OK = None
        cmd, _env = gg.graphify_command()
        self.assertEqual(cmd[1:3], ["-m", "graphify"])
        self.assertEqual(len(cmd), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)

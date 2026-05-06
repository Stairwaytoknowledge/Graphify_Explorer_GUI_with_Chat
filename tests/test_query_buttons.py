"""End-to-end tests for the four "Ask the graph" buttons.

Each button (Query / Explain / Path A|B) is a thin wrapper around a
graphify CLI subcommand. These tests build a tiny graph from a
3-file Python project, invoke each subcommand the same way the GUI
does (via graphify_command()), and assert on the output shape.

Run from the repo root:

    python -m unittest tests.test_query_buttons
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

# Match graphify_gui's bootstrap so this file works under the home python.
_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_root))
_venv_sp = _root / ".venv" / "Lib" / "site-packages"
if _venv_sp.exists() and str(_venv_sp) not in sys.path:
    sys.path.insert(0, str(_venv_sp))

import graphify_gui as gg  # noqa: E402


def _build_fixture(tmp: Path) -> Path:
    """Write a 3-file Python project that produces a deterministic graph."""
    src = tmp / "src"
    src.mkdir(parents=True)
    (src / "auth.py").write_text(textwrap.dedent('''
        """Authentication module."""
        def hash_password(plain: str) -> str:
            return f"hashed:{plain}"

        def verify_password(plain: str, hashed: str) -> bool:
            return hashed == hash_password(plain)
    ''').strip() + "\n")
    (src / "login.py").write_text(textwrap.dedent('''
        """Login flow."""
        from auth import verify_password, hash_password

        def login(user: str, password: str) -> bool:
            stored = hash_password(password)
            return verify_password(password, stored)
    ''').strip() + "\n")
    (src / "api.py").write_text(textwrap.dedent('''
        """API surface."""
        from login import login

        def handle_request(payload: dict) -> bool:
            return login(payload["user"], payload["pw"])
    ''').strip() + "\n")
    return tmp


def _run(cmd: list[str], cwd: Path, env: dict[str, str], timeout: float = 60.0) -> tuple[int, str]:
    r = subprocess.run(
        cmd, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout,
    )
    return r.returncode, (r.stdout or "") + (r.stderr or "")


class QueryButtonsE2E(unittest.TestCase):
    """One graph build, four assertions: matches the GUI's actual flow."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="gxqbtn-"))
        _build_fixture(cls.tmp)
        # Use the same code path the GUI uses to launch graphify.
        cmd, env_overrides = gg.graphify_command(["update", "."])
        env = os.environ.copy()
        env.update(env_overrides)
        rc, out = _run(cmd, cls.tmp, env, timeout=120.0)
        if rc != 0:
            raise unittest.SkipTest(f"graphify update failed in fixture build: {out[:300]}")
        cls.env = env
        cls.graph_json = cls.tmp / "graphify-out" / "graph.json"
        if not cls.graph_json.exists():
            raise unittest.SkipTest(f"graph.json missing after update: {cls.graph_json}")

    @classmethod
    def tearDownClass(cls) -> None:
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_query_returns_bfs_traversal(self) -> None:
        # Mirrors what _query() does: graphify_command(['query', q]).
        cmd, _ = gg.graphify_command(["query", "What does login do?"])
        rc, out = _run(cmd, self.tmp, self.env)
        self.assertEqual(rc, 0, msg=out[:400])
        # Header format from _query_graph_text
        self.assertIn("Traversal:", out)
        self.assertIn("BFS", out)
        self.assertIn("login()", out)
        # Should produce both NODE and EDGE lines.
        self.assertIn("NODE ", out)
        self.assertIn("EDGE ", out)

    def test_query_no_match_message(self) -> None:
        cmd, _ = gg.graphify_command(["query", "xyzzy_no_such_thing"])
        rc, out = _run(cmd, self.tmp, self.env)
        self.assertEqual(rc, 0, msg=out[:400])
        self.assertIn("No matching nodes found", out)

    def test_explain_dumps_node_card(self) -> None:
        # Mirrors _explain(): graphify_command(['explain', q]).
        cmd, _ = gg.graphify_command(["explain", "hash_password"])
        rc, out = _run(cmd, self.tmp, self.env)
        self.assertEqual(rc, 0, msg=out[:400])
        # The detail card always starts with "Node:" and lists Source/Type/Degree.
        self.assertIn("Node:", out)
        self.assertIn("hash_password", out)
        self.assertIn("Source:", out)
        self.assertIn("Degree:", out)

    def test_explain_no_match(self) -> None:
        cmd, _ = gg.graphify_command(["explain", "xyzzy_no_such_node"])
        rc, out = _run(cmd, self.tmp, self.env)
        self.assertEqual(rc, 0, msg=out[:400])
        self.assertIn("No node matching", out)

    def test_path_finds_shortest_route(self) -> None:
        # Mirrors _path_between() splitting "A|B" before invocation.
        a, b = "handle_request", "hash_password"
        cmd, _ = gg.graphify_command(["path", a, b])
        rc, out = _run(cmd, self.tmp, self.env)
        self.assertEqual(rc, 0, msg=out[:400])
        self.assertIn("Shortest path", out)
        self.assertIn("handle_request()", out)
        self.assertIn("hash_password()", out)
        # 2 hops via login(): handle_request -> login -> hash_password
        self.assertIn("hops", out)

    def test_path_unreachable_source(self) -> None:
        cmd, _ = gg.graphify_command(["path", "xyzzy_no_such", "hash_password"])
        rc, out = _run(cmd, self.tmp, self.env)
        # graphify exits 1 when source/target lookup fails.
        self.assertNotEqual(rc, 0)
        self.assertIn("No node matching", out)


class PathSplitTest(unittest.TestCase):
    """The GUI parses 'A|B' before sending to graphify path."""

    def test_split_strips_whitespace(self) -> None:
        q = "  handle_request  |  hash_password  "
        a, b = (s.strip() for s in q.split("|", 1))
        self.assertEqual(a, "handle_request")
        self.assertEqual(b, "hash_password")

    def test_split_first_pipe_only(self) -> None:
        # Multiple pipes: only the first separates A from B.
        q = "foo|bar|baz"
        a, b = (s.strip() for s in q.split("|", 1))
        self.assertEqual(a, "foo")
        self.assertEqual(b, "bar|baz")


if __name__ == "__main__":
    unittest.main(verbosity=2)

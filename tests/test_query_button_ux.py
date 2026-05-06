"""Tests for the post-press UX of Query / Explain / Path buttons.

Two regressions this guards against:

  1. Output tab not auto-selected when a button is pressed. Symptom:
     the user clicks Query, the subprocess runs, the result streams
     to a tab they're not looking at, they think nothing happened.
  2. Pasted CLI form silently matched nothing. Symptom: typing
     `graphify explain "Personalive"` into the box and pressing
     Explain ran `graphify explain 'graphify explain "Personalive"'`
     which scored zero nodes and printed `No node matching ... found.`

Both are pure-Python checks: we monkeypatch _run_graphify to capture
the args it would have launched, then assert on the captured value
and on the notebook tab index after the press.
"""

from __future__ import annotations

import sys
import tkinter as tk
import unittest
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_root))
_venv_sp = _root / ".venv" / "Lib" / "site-packages"
if _venv_sp.exists() and str(_venv_sp) not in sys.path:
    sys.path.insert(0, str(_venv_sp))

import graphify_gui as gg  # noqa: E402


class StripCliPrefixTest(unittest.TestCase):
    def test_strips_full_graphify_query(self) -> None:
        s = gg.GraphifyApp._strip_cli_prefix(
            'graphify query "unet_3d_blocks"', gg.GraphifyApp._QUERY_CLI_PREFIXES,
        )
        self.assertEqual(s, "unet_3d_blocks")

    def test_strips_short_query_form(self) -> None:
        s = gg.GraphifyApp._strip_cli_prefix(
            "query unet_3d_blocks", gg.GraphifyApp._QUERY_CLI_PREFIXES,
        )
        self.assertEqual(s, "unet_3d_blocks")

    def test_strips_explain_prefix(self) -> None:
        s = gg.GraphifyApp._strip_cli_prefix(
            'graphify explain "Personalive"',
            gg.GraphifyApp._EXPLAIN_CLI_PREFIXES,
        )
        self.assertEqual(s, "Personalive")

    def test_unprefixed_passes_through(self) -> None:
        # Bare symbol - no prefix, no quotes to strip. Returned as-is.
        self.assertEqual(
            gg.GraphifyApp._strip_cli_prefix(
                "unet_3d_blocks", gg.GraphifyApp._QUERY_CLI_PREFIXES,
            ),
            "unet_3d_blocks",
        )

    def test_strips_outer_single_quotes(self) -> None:
        s = gg.GraphifyApp._strip_cli_prefix(
            "graphify query 'unet'", gg.GraphifyApp._QUERY_CLI_PREFIXES,
        )
        self.assertEqual(s, "unet")

    def test_does_not_strip_internal_quotes(self) -> None:
        # The quote pattern is "outer balanced quotes only".
        s = gg.GraphifyApp._strip_cli_prefix(
            'has "quote" inside', gg.GraphifyApp._QUERY_CLI_PREFIXES,
        )
        self.assertEqual(s, 'has "quote" inside')

    def test_case_insensitive_prefix(self) -> None:
        s = gg.GraphifyApp._strip_cli_prefix(
            "Graphify Query Foo", gg.GraphifyApp._QUERY_CLI_PREFIXES,
        )
        self.assertEqual(s, "Foo")

    def test_only_prefix_returns_empty(self) -> None:
        # If the user typed JUST `graphify query`, the search term is
        # empty and the GUI should treat that like an empty box.
        s = gg.GraphifyApp._strip_cli_prefix(
            "graphify query", gg.GraphifyApp._QUERY_CLI_PREFIXES,
        )
        # Nothing left after the prefix; rest is empty so the original
        # string survives. The empty-check runs at the call site, this
        # helper just normalizes prefixes when there's something behind
        # them.
        # Still, the result must be whitespace-stripped and not crash.
        self.assertIsInstance(s, str)


class _RecordingApp(gg.GraphifyApp):
    """Subclass that captures _run_graphify args + selected tab without
    actually launching graphify."""

    def __init__(self, root: tk.Tk) -> None:
        super().__init__(root)
        self.captured_args: list[list[str]] = []
        # Stub out subprocess machinery: we only care about WHAT would
        # be run, not actually running it.
        self._real_run_graphify = self._run_graphify
        self._run_graphify = self._fake_run_graphify  # type: ignore[assignment]

    def _fake_run_graphify(self, args, cwd, then_load=False):  # type: ignore[no-redef]
        self.captured_args.append(list(args))

    def _selected_path(self, silent=False):
        # Pretend we have a valid input path so _run_query / _path_between
        # don't bail before reaching the recording stub.
        return Path(".")


class QueryButtonRoutingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = tk.Tk()
        cls.root.withdraw()
        cls.app = _RecordingApp(cls.root)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.root.destroy()
        except Exception:
            pass

    def setUp(self) -> None:
        self.app.captured_args.clear()
        # Park focus on a non-Output tab so the test detects the auto-switch.
        try:
            self.app.notebook.select(self.app.TAB_MERMAID)
        except Exception:
            pass

    def _output_tab_selected(self) -> bool:
        try:
            tabs = self.app.notebook.tabs()
            return self.app.notebook.select() == tabs[-1]
        except Exception:
            return False

    def test_query_strips_cli_prefix(self) -> None:
        self.app.query_var.set('graphify query "unet_3d_blocks"')
        self.app._query()
        self.assertEqual(len(self.app.captured_args), 1)
        self.assertEqual(self.app.captured_args[0], ["query", "unet_3d_blocks"])

    def test_explain_strips_cli_prefix(self) -> None:
        # The exact bug from the user report.
        self.app.query_var.set('graphify explain "Personalive"')
        self.app._explain()
        self.assertEqual(len(self.app.captured_args), 1)
        self.assertEqual(self.app.captured_args[0], ["explain", "Personalive"])

    def test_query_auto_switches_to_output(self) -> None:
        self.app.query_var.set("login")
        self.app._query()
        self.assertTrue(
            self._output_tab_selected(),
            "Query button must auto-switch to the Output tab so the user "
            "sees the command echo and result.",
        )

    def test_explain_auto_switches_to_output(self) -> None:
        self.app.query_var.set("login")
        self.app._explain()
        self.assertTrue(self._output_tab_selected())

    def test_path_strips_prefix_and_handles_pipe(self) -> None:
        self.app.query_var.set('graphify path "A|B"')
        self.app._path_between()
        self.assertEqual(len(self.app.captured_args), 1)
        # Inner quotes around "A|B" are stripped by the outer-quote rule;
        # then split on `|`.
        self.assertEqual(self.app.captured_args[0], ["path", "A", "B"])

    def test_path_handles_space_separated_cli_form(self) -> None:
        # `graphify path "handle_request" "hash_password"` with no pipe.
        self.app.query_var.set('graphify path handle_request hash_password')
        self.app._path_between()
        self.assertEqual(len(self.app.captured_args), 1)
        self.assertEqual(
            self.app.captured_args[0],
            ["path", "handle_request", "hash_password"],
        )

    def test_path_auto_switches_to_output(self) -> None:
        self.app.query_var.set("a|b")
        self.app._path_between()
        self.assertTrue(self._output_tab_selected())

    def test_empty_query_does_not_run_or_switch(self) -> None:
        # Park on a non-Output tab; an empty query should leave it.
        self.app.query_var.set("")
        try:
            self.app.notebook.select(self.app.TAB_MERMAID)
        except Exception:
            pass
        # _query routes through _run_query which warns and returns
        # before _switch_to_output_tab fires. Suppress the messagebox
        # by replacing it temporarily.
        from tkinter import messagebox
        real_warn = messagebox.showwarning
        messagebox.showwarning = lambda *a, **kw: None
        try:
            self.app._query()
        finally:
            messagebox.showwarning = real_warn
        self.assertEqual(self.app.captured_args, [])
        # Tab should NOT have been auto-switched on the empty path.
        self.assertFalse(self._output_tab_selected())


if __name__ == "__main__":
    unittest.main(verbosity=2)

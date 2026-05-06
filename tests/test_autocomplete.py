"""Tests for graphify_autocomplete: scorer, candidate building, ranking.

The popup widget itself is not tested at the Tk level (it needs a
display); its bindings are exercised indirectly via the smoke test
that drives a real GUI in the CI matrix. The scorer + candidate
builder + ranker are the important pieces and they're pure Python.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Bootstrap so tests run under the home python with the venv on path.
_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_root))
_venv_sp = _root / ".venv" / "Lib" / "site-packages"
if _venv_sp.exists() and str(_venv_sp) not in sys.path:
    sys.path.insert(0, str(_venv_sp))

import networkx as nx  # noqa: E402

import graphify_autocomplete as ac  # noqa: E402


def _sym(value: str, source_file: str = "") -> ac.Candidate:
    return ac.Candidate(
        value=value,
        description="",
        kind="symbol",
        match_target=value.lower(),
        match_source_file=source_file.lower(),
    )


class ScorerTest(unittest.TestCase):
    def test_prefix_outranks_substring(self) -> None:
        prefix = _sym("auth_token")
        sub = _sym("verify_auth")
        s_pre = ac.score("auth", prefix)
        s_sub = ac.score("auth", sub)
        self.assertGreater(s_pre, s_sub)

    def test_token_prefix_beats_substring(self) -> None:
        # `auth` matches `verify_auth_token` as a token-prefix (snake)
        # but only as substring in `verifauthor`. Token-prefix wins.
        token_prefix = _sym("verify_auth_token")
        substr_only = _sym("verifauthor")
        s_tp = ac.score("auth", token_prefix)
        s_sub = ac.score("auth", substr_only)
        self.assertGreater(s_tp, s_sub)

    def test_camelcase_token_boundary(self) -> None:
        # `Auth` should be a token-prefix for camelCase `verifyAuthToken`.
        cand = _sym("verifyAuthToken")
        self.assertGreater(ac.score("auth", cand), 0.0)

    def test_dotted_token_boundary(self) -> None:
        cand = _sym("a.b.unet_block")
        self.assertGreater(ac.score("unet", cand), 0.0)

    def test_no_match_zero(self) -> None:
        cand = _sym("alpha_beta_gamma")
        self.assertEqual(ac.score("xyzzy", cand), 0.0)

    def test_empty_query_zero(self) -> None:
        cand = _sym("alpha")
        self.assertEqual(ac.score("", cand), 0.0)
        self.assertEqual(ac.score("   ", cand), 0.0)

    def test_multi_token_all_required(self) -> None:
        # "auth login" - only `auth login_handler` matches both.
        good = _sym("auth_login_handler")
        bad = _sym("auth_only_thing")
        self.assertGreater(ac.score("auth login", good), 0.0)
        self.assertEqual(ac.score("auth login", bad), 0.0)

    def test_source_file_substring_scores(self) -> None:
        # Label doesn't contain the term but source_file does.
        cand = _sym("helper", source_file="src/auth/utils.py")
        self.assertGreater(ac.score("auth", cand), 0.0)
        # Pure label match still ranks higher than source-file-only match.
        label_match = _sym("auth_check")
        self.assertGreater(
            ac.score("auth", label_match),
            ac.score("auth", cand),
        )

    def test_case_insensitive(self) -> None:
        cand = _sym("UNet3DBlocks")
        self.assertGreater(ac.score("unet", cand), 0.0)
        self.assertGreater(ac.score("UNET", cand), 0.0)
        self.assertGreater(ac.score("Unet", cand), 0.0)

    def test_shorter_label_breaks_ties(self) -> None:
        # Same prefix-match score; shorter label should rank higher.
        short = _sym("unet_3d_blocks")
        long = _sym("unet_3d_blocks_helper_extra")
        self.assertGreater(
            ac.score("unet_3d_blocks", short),
            ac.score("unet_3d_blocks", long),
        )


class CommandCandidatesTest(unittest.TestCase):
    def test_three_commands_present(self) -> None:
        values = {c.value for c in ac.COMMAND_CANDIDATES}
        self.assertIn("graphify query", values)
        self.assertIn("graphify explain", values)
        self.assertIn("graphify path", values)

    def test_each_has_description(self) -> None:
        for c in ac.COMMAND_CANDIDATES:
            self.assertTrue(c.description, f"{c.value} missing description")
            self.assertEqual(c.kind, "command")

    def test_command_matches_typing_gra(self) -> None:
        # The motivating user case: typing "gra" should suggest all
        # three command shapes.
        ranked = ac.rank("gra", ac.COMMAND_CANDIDATES)
        self.assertEqual(len(ranked), 3)
        # All "graphify ..." shapes should win prefix score.
        self.assertTrue(all(c.match_target.startswith("graphify") for c in ranked))

    def test_typing_que_finds_query_only(self) -> None:
        ranked = ac.rank("que", ac.COMMAND_CANDIDATES)
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0].value, "graphify query")


class BuildGraphCandidatesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.G = nx.DiGraph()
        self.G.add_node(
            "n1", label="hash_password",
            source_file="src/auth.py",
            file_type="function",
            community=0,
        )
        self.G.add_node(
            "n2", label="UNet3DBlocks",
            source_file="src/diffusion/unet_3d_blocks.py",
            file_type="class",
            community=4,
        )
        self.G.add_node(
            "n3", label="",  # blank label -> dropped
            source_file="src/skip.py",
        )
        self.G.add_edge("n1", "n2")

    def test_drops_blank_labels(self) -> None:
        cands = ac.build_graph_candidates(self.G)
        values = {c.value for c in cands}
        self.assertNotIn("", values)
        self.assertEqual(len(cands), 2)

    def test_preserves_label_in_value(self) -> None:
        cands = ac.build_graph_candidates(self.G)
        values = {c.value for c in cands}
        self.assertIn("hash_password", values)
        self.assertIn("UNet3DBlocks", values)

    def test_description_contains_source_and_kind(self) -> None:
        cands = ac.build_graph_candidates(self.G)
        unet = next(c for c in cands if c.value == "UNet3DBlocks")
        self.assertIn("unet_3d_blocks.py", unet.description)
        self.assertIn("class", unet.description)
        self.assertIn("comm 4", unet.description)
        self.assertIn("deg ", unet.description)

    def test_match_target_lowercased(self) -> None:
        cands = ac.build_graph_candidates(self.G)
        for c in cands:
            self.assertEqual(c.match_target, c.match_target.lower())

    def test_none_graph_returns_empty(self) -> None:
        self.assertEqual(ac.build_graph_candidates(None), [])

    def test_path_normalized(self) -> None:
        # Backslashes get normalized to forward slashes for stable
        # cross-platform display.
        G = nx.DiGraph()
        G.add_node(
            "n", label="x",
            source_file="src\\foo\\bar.py",
        )
        cands = ac.build_graph_candidates(G)
        self.assertIn("src/foo/bar.py", cands[0].description)


class RankTest(unittest.TestCase):
    def test_combines_commands_and_symbols(self) -> None:
        symbols = [
            _sym("unet_3d_blocks", "src/diffusion/unet_3d_blocks.py"),
            _sym("UNet2DConditionModel", "src/diffusion/unet_2d.py"),
        ]
        all_cands = list(ac.COMMAND_CANDIDATES) + symbols
        ranked = ac.rank("unet", all_cands, top_n=10)
        # Symbols should match; commands should not.
        kinds = [c.kind for c in ranked]
        self.assertIn("symbol", kinds)
        self.assertNotIn("command", kinds)

    def test_top_n_caps_results(self) -> None:
        many = [_sym(f"unet_block_{i}") for i in range(50)]
        ranked = ac.rank("unet", many, top_n=5)
        self.assertEqual(len(ranked), 5)

    def test_empty_query_yields_nothing(self) -> None:
        symbols = [_sym("anything")]
        self.assertEqual(ac.rank("", symbols), [])
        self.assertEqual(ac.rank("   ", symbols), [])

    def test_results_sorted_by_score_desc(self) -> None:
        cands = [
            _sym("unet"),                   # exact prefix, short
            _sym("unet_3d_blocks"),         # prefix, longer
            _sym("verify_unet_block"),      # token prefix only
            _sym("verifunetextra"),         # pure substring (no token bound)
        ]
        ranked = ac.rank("unet", cands, top_n=10)
        # First should be the exact-prefix shortest one.
        self.assertEqual(ranked[0].value, "unet")
        # The substring-only entry has the lowest score (no token-prefix
        # bonus because `unet` doesn't sit on a snake/camel boundary
        # inside `verifunetextra`).
        self.assertEqual(ranked[-1].value, "verifunetextra")
        # Order is monotonically non-increasing by score.
        scores = [ac.score("unet", c) for c in ranked]
        self.assertEqual(scores, sorted(scores, reverse=True))


class TokensTest(unittest.TestCase):
    def test_snake_case(self) -> None:
        self.assertEqual(ac._tokens("hash_password"), ["hash", "password"])

    def test_kebab_case(self) -> None:
        self.assertEqual(ac._tokens("hash-password"), ["hash", "password"])

    def test_dotted(self) -> None:
        self.assertEqual(ac._tokens("a.b.c"), ["a", "b", "c"])

    def test_camelcase(self) -> None:
        self.assertEqual(ac._tokens("verifyAuthToken"),
                         ["verify", "auth", "token"])

    def test_pascalcase(self) -> None:
        self.assertEqual(ac._tokens("UNetBlocks"), ["u", "net", "blocks"])

    def test_empty(self) -> None:
        self.assertEqual(ac._tokens(""), [])

    def test_path_segments(self) -> None:
        self.assertEqual(ac._tokens("src/auth/utils"),
                         ["src", "auth", "utils"])


class PopupResetTest(unittest.TestCase):
    """Regression: the user sometimes saw the Entry "stuck and unable
    to type". The popup's reset() recovers the Entry's typing focus
    from any state it might have ended up in.
    """

    def test_reset_clears_suppress_flag(self) -> None:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        try:
            entry = tk.Entry(root)
            popup = ac.AutocompletePopup(
                master=root,
                entry=entry,
                candidate_provider=lambda: [],
                on_select=lambda c: None,
            )
            popup._suppress_show = True
            popup.reset()
            self.assertFalse(popup._suppress_show)
        finally:
            try:
                root.destroy()
            except Exception:
                pass

    def test_reset_cancels_pending_after(self) -> None:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        try:
            entry = tk.Entry(root)
            popup = ac.AutocompletePopup(
                master=root,
                entry=entry,
                candidate_provider=lambda: [],
                on_select=lambda c: None,
            )
            # Schedule a fake pending refresh.
            popup._after_id = root.after(99999, lambda: None)
            self.assertIsNotNone(popup._after_id)
            popup.reset()
            self.assertIsNone(popup._after_id)
        finally:
            try:
                root.destroy()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main(verbosity=2)

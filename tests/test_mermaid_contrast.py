"""Lock in the Mermaid contrast fix.

The annotated Mermaid generator emits one classDef per community.
The fill is the community's bright palette color and the text color
is auto-picked by luma so it stays readable on light fills (yellow,
red, mint, etc.).

This test builds a 3-community graph in memory, runs the annotator,
and asserts that each classDef line carries an auto-picked text
color (not the old hardcoded `#e7ecf3`).
"""

from __future__ import annotations

import re
import sys
import tkinter as tk
import unittest
from pathlib import Path

# Bootstrap venv site-packages just like graphify_gui does.
_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_root))
_venv_sp = _root / ".venv" / "Lib" / "site-packages"
if _venv_sp.exists() and str(_venv_sp) not in sys.path:
    sys.path.insert(0, str(_venv_sp))

import networkx as nx  # noqa: E402
import graphify_gui as gg  # noqa: E402
import graphify_theme as gt  # noqa: E402


def _make_graph_with_communities() -> nx.Graph:
    G = nx.DiGraph()
    # Community 0: 2 nodes
    G.add_node("a", label="alpha", community=0)
    G.add_node("b", label="beta",  community=0)
    G.add_edge("a", "b", relation="calls")
    # Community 1: 2 nodes
    G.add_node("c", label="gamma", community=1)
    G.add_node("d", label="delta", community=1)
    G.add_edge("c", "d", relation="calls")
    G.add_edge("b", "c", relation="imports")  # cross-community link
    # Community 4: yellow palette index (#ffb454) - the buggy case
    G.add_node("e", label="epsilon", community=4)
    G.add_node("f", label="zeta",    community=4)
    G.add_edge("e", "f", relation="calls")
    G.add_edge("d", "e", relation="contains")
    return G


class MermaidContrastTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # The annotator is a method of GraphifyApp, so we need an app
        # instance. Tk root is created once and reused.
        cls.root = tk.Tk()
        cls.root.withdraw()
        cls.app = gg.GraphifyApp(cls.root)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.root.destroy()
        except Exception:
            pass

    def test_classdefs_use_solid_fill_no_alpha(self) -> None:
        G = _make_graph_with_communities()
        text = self.app._graph_to_mermaid_annotated(G)
        # The old code wrote `fill:#xxxxxx22,...`; the fix uses solid
        # `fill:#xxxxxx,stroke:#xxxxxx,color:#yyyyyy`. Assert no
        # 8-char hex (which would be #RRGGBBAA) appears in fill:.
        for m in re.finditer(r"fill:(#[0-9A-Fa-f]+)", text):
            hex_value = m.group(1)
            self.assertEqual(
                len(hex_value), 7,
                f"unexpected alpha in fill: {hex_value} (text was: {text[:200]})",
            )

    def test_classdef_text_color_is_auto_picked(self) -> None:
        G = _make_graph_with_communities()
        text = self.app._graph_to_mermaid_annotated(G)
        # Hardcoded #e7ecf3 from the buggy version must NOT appear.
        self.assertNotIn(
            "#e7ecf3", text,
            "old hardcoded ash text color leaked through",
        )
        # Each classDef line carries a `color:` that matches one of
        # the two text variants returned by text_for_bg.
        valid = {gt._TEXT_ON_LIGHT.lower(), gt._TEXT_ON_DARK.lower()}
        seen_colors = set()
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("classDef "):
                continue
            m = re.search(r"color:(#[0-9A-Fa-f]{6})", line)
            self.assertIsNotNone(
                m, f"classDef line missing color: {line!r}",
            )
            seen_colors.add(m.group(1).lower())
        self.assertGreater(len(seen_colors), 0,
                           "no classDef lines emitted")
        for c in seen_colors:
            self.assertIn(c, valid,
                          f"classDef text color {c} is not a luma-picked variant")

    def test_yellow_community_uses_dark_text(self) -> None:
        # Community 4 maps to PALETTE_HEX[4] = #ffb454 (orange-yellow,
        # luma > 128). The classDef for that community must carry the
        # dark text variant.
        G = _make_graph_with_communities()
        text = self.app._graph_to_mermaid_annotated(G)
        m = re.search(
            r"classDef cls_4\s+fill:#ffb454,stroke:#ffb454,color:(#[0-9A-Fa-f]{6})",
            text,
        )
        self.assertIsNotNone(m, f"classDef cls_4 missing or malformed:\n{text}")
        self.assertEqual(m.group(1).lower(), gt._TEXT_ON_LIGHT.lower())

    def test_purple_community_uses_light_text(self) -> None:
        # Community 2 -> PALETTE_HEX[2] = #7c5cff (dark purple).
        G = _make_graph_with_communities()
        # Reuse the same fixture by adding a community-2 node.
        G.add_node("g", label="eta", community=2)
        G.add_node("h", label="theta", community=2)
        G.add_edge("g", "h", relation="calls")
        text = self.app._graph_to_mermaid_annotated(G)
        m = re.search(
            r"classDef cls_2\s+fill:#7c5cff,stroke:#7c5cff,color:(#[0-9A-Fa-f]{6})",
            text,
        )
        self.assertIsNotNone(m, f"classDef cls_2 missing or malformed:\n{text}")
        self.assertEqual(m.group(1).lower(), gt._TEXT_ON_DARK.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)

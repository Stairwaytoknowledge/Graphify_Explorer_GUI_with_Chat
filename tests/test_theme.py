"""Tests for graphify_theme."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import graphify_theme as gt  # noqa: E402


class PaletteShapeTest(unittest.TestCase):
    def test_keys_match_across_palettes(self) -> None:
        # Every key in DARK must also be in LIGHT and CYBERPUNK.
        self.assertEqual(
            set(gt.DARK_PALETTE.keys()),
            set(gt.LIGHT_PALETTE.keys()),
        )
        self.assertEqual(
            set(gt.DARK_PALETTE.keys()),
            set(gt.CYBERPUNK_PALETTE.keys()),
        )

    def test_palette_for_dark(self) -> None:
        self.assertEqual(gt.palette_for_mode("dark"), gt.DARK_PALETTE)

    def test_palette_for_light(self) -> None:
        self.assertEqual(gt.palette_for_mode("light"), gt.LIGHT_PALETTE)

    def test_palette_for_cyberpunk(self) -> None:
        self.assertEqual(gt.palette_for_mode("cyberpunk"), gt.CYBERPUNK_PALETTE)

    def test_palette_unknown_falls_back(self) -> None:
        # Unknown mode strings fall back to dark.
        self.assertEqual(gt.palette_for_mode("garbage"), gt.DARK_PALETTE)
        self.assertEqual(gt.palette_for_mode(""), gt.DARK_PALETTE)

    def test_palette_returns_copy_not_alias(self) -> None:
        p1 = gt.palette_for_mode("dark")
        p2 = gt.palette_for_mode("dark")
        self.assertIsNot(p1, p2)
        p1["bg"] = "#000"
        self.assertNotEqual(p1["bg"], p2["bg"])

    def test_required_keys_present(self) -> None:
        # The keys the GUI relies on at minimum.
        required = {
            "bg", "panel", "panel_alt", "fg", "fg_dim",
            "accent", "accent2", "ok", "warn", "edge", "on_accent",
        }
        self.assertTrue(required.issubset(set(gt.DARK_PALETTE.keys())))
        self.assertTrue(required.issubset(set(gt.LIGHT_PALETTE.keys())))
        self.assertTrue(required.issubset(set(gt.CYBERPUNK_PALETTE.keys())))

    def test_cyberpunk_is_dark_family(self) -> None:
        # Sanity check on the cyberpunk bg luma so the Mermaid bridge
        # picks the dark Mermaid theme rather than the light one.
        self.assertFalse(gt._is_light(gt.CYBERPUNK_PALETTE["bg"]))


class AutoModeTest(unittest.TestCase):
    def test_morning_is_daytime(self) -> None:
        self.assertTrue(gt.is_daytime(datetime(2026, 5, 6, 8, 0)))
        self.assertTrue(gt.is_daytime(datetime(2026, 5, 6, 12, 30)))

    def test_evening_is_not_daytime(self) -> None:
        self.assertFalse(gt.is_daytime(datetime(2026, 5, 6, 19, 0)))
        self.assertFalse(gt.is_daytime(datetime(2026, 5, 6, 22, 0)))

    def test_late_night_is_not_daytime(self) -> None:
        self.assertFalse(gt.is_daytime(datetime(2026, 5, 6, 2, 0)))

    def test_boundary_07_is_daytime(self) -> None:
        self.assertTrue(gt.is_daytime(datetime(2026, 5, 6, 7, 0)))

    def test_boundary_19_is_not_daytime(self) -> None:
        # 19:00 is the cutover; dark from 19:00 onwards.
        self.assertFalse(gt.is_daytime(datetime(2026, 5, 6, 19, 0)))

    def test_auto_picks_light_at_noon(self) -> None:
        p = gt.palette_for_mode("auto", now=datetime(2026, 5, 6, 12, 0))
        self.assertEqual(p, gt.LIGHT_PALETTE)

    def test_auto_picks_dark_at_midnight(self) -> None:
        p = gt.palette_for_mode("auto", now=datetime(2026, 5, 6, 0, 0))
        self.assertEqual(p, gt.DARK_PALETTE)

    def test_effective_mode_resolves_auto(self) -> None:
        self.assertEqual(
            gt.effective_mode("auto", now=datetime(2026, 5, 6, 12, 0)),
            "light",
        )
        self.assertEqual(
            gt.effective_mode("auto", now=datetime(2026, 5, 6, 23, 0)),
            "dark",
        )

    def test_effective_mode_passthrough(self) -> None:
        self.assertEqual(gt.effective_mode("dark"), "dark")
        self.assertEqual(gt.effective_mode("light"), "light")


class ResolveModeTest(unittest.TestCase):
    def test_canonical(self) -> None:
        self.assertEqual(gt.resolve_mode("dark"), "dark")
        self.assertEqual(gt.resolve_mode("light"), "light")
        self.assertEqual(gt.resolve_mode("auto"), "auto")
        self.assertEqual(gt.resolve_mode("cyberpunk"), "cyberpunk")

    def test_effective_mode_passes_cyberpunk(self) -> None:
        # Cyberpunk is a concrete palette so effective_mode returns it
        # as-is. The Mermaid bridge then uses bg luma to pick "dark".
        self.assertEqual(gt.effective_mode("cyberpunk"), "cyberpunk")

    def test_normalize(self) -> None:
        self.assertEqual(gt.resolve_mode("  Dark  "), "dark")
        self.assertEqual(gt.resolve_mode("LIGHT"), "light")

    def test_unknown_default(self) -> None:
        self.assertEqual(gt.resolve_mode("rainbow"), gt.DEFAULT_MODE)
        self.assertEqual(gt.resolve_mode(""), gt.DEFAULT_MODE)


class SettingsPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="gxset-"))
        self.settings_path = self.tmpdir / "settings.json"

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_load_missing_returns_empty(self) -> None:
        self.assertEqual(gt.load_settings(self.settings_path), {})

    def test_load_corrupt_returns_empty(self) -> None:
        self.settings_path.write_text("{not json")
        self.assertEqual(gt.load_settings(self.settings_path), {})

    def test_roundtrip(self) -> None:
        self.assertTrue(gt.save_settings({"k": "v"}, self.settings_path))
        self.assertEqual(gt.load_settings(self.settings_path), {"k": "v"})

    def test_merges_existing(self) -> None:
        gt.save_settings({"a": 1}, self.settings_path)
        gt.save_settings({"b": 2}, self.settings_path)
        d = gt.load_settings(self.settings_path)
        self.assertEqual(d, {"a": 1, "b": 2})

    def test_theme_mode_helpers(self) -> None:
        self.assertEqual(gt.load_theme_mode(self.settings_path), gt.DEFAULT_MODE)
        gt.save_theme_mode("light", self.settings_path)
        self.assertEqual(gt.load_theme_mode(self.settings_path), "light")

    def test_theme_mode_normalizes_on_save(self) -> None:
        gt.save_theme_mode("AUTO", self.settings_path)
        self.assertEqual(gt.load_theme_mode(self.settings_path), "auto")

    def test_theme_mode_normalizes_garbage_on_load(self) -> None:
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(json.dumps({"theme_mode": "rainbow"}))
        # Garbage in stored value -> falls back to default.
        self.assertEqual(gt.load_theme_mode(self.settings_path), gt.DEFAULT_MODE)


class MermaidThemeTest(unittest.TestCase):
    def test_dark_palette_to_dark_theme(self) -> None:
        self.assertEqual(gt.mermaid_theme_for(gt.DARK_PALETTE), "dark")

    def test_light_palette_to_default_theme(self) -> None:
        # Mermaid's "default" theme is its light theme.
        self.assertEqual(gt.mermaid_theme_for(gt.LIGHT_PALETTE), "default")

    def test_dark_mode_string(self) -> None:
        self.assertEqual(gt.mermaid_theme_for("dark"), "dark")

    def test_light_mode_string(self) -> None:
        self.assertEqual(gt.mermaid_theme_for("light"), "default")

    def test_cyberpunk_palette_to_dark_theme(self) -> None:
        # Bg luma is below the threshold so the dark Mermaid theme is
        # chosen; the actual neon colors are passed via env vars.
        self.assertEqual(gt.mermaid_theme_for(gt.CYBERPUNK_PALETTE), "dark")

    def test_is_light_helper(self) -> None:
        self.assertTrue(gt._is_light("#ffffff"))
        self.assertFalse(gt._is_light("#000000"))
        self.assertTrue(gt._is_light("#f6f8fb"))   # light palette bg
        self.assertFalse(gt._is_light("#101826"))  # dark palette bg

    def test_is_light_3_char_hex(self) -> None:
        self.assertTrue(gt._is_light("#fff"))
        self.assertFalse(gt._is_light("#000"))

    def test_is_light_invalid_returns_false(self) -> None:
        self.assertFalse(gt._is_light("not-a-color"))

    def test_is_light_strips_alpha(self) -> None:
        # 8-char hex #RRGGBBAA must be evaluated on RGB only.
        self.assertTrue(gt._is_light("#ffffff22"))
        self.assertFalse(gt._is_light("#000000ff"))


class TextForBgTest(unittest.TestCase):
    def test_dark_text_on_light_bg(self) -> None:
        # Saturated bright colors that blew up the original Mermaid
        # contrast: yellow, mint, cyan, orange. All should pick the
        # dark text variant.
        for c in ("#ffffff", "#ffd166", "#5fd38f", "#ffb454",
                  "#9bd4ff", "#5ac6ff"):
            self.assertEqual(gt.text_for_bg(c), gt._TEXT_ON_LIGHT,
                             f"expected dark text on {c}")

    def test_light_text_on_dark_bg(self) -> None:
        for c in ("#000000", "#101826", "#0a0418", "#7c5cff",
                  "#1c0d4a"):
            self.assertEqual(gt.text_for_bg(c), gt._TEXT_ON_DARK,
                             f"expected light text on {c}")

    def test_handles_alpha_suffix(self) -> None:
        # Same color +/- alpha should pick the same text color.
        self.assertEqual(
            gt.text_for_bg("#ffd166"), gt.text_for_bg("#ffd16622"),
        )

    def test_handles_3char_hex(self) -> None:
        self.assertEqual(gt.text_for_bg("#fff"), gt._TEXT_ON_LIGHT)
        self.assertEqual(gt.text_for_bg("#000"), gt._TEXT_ON_DARK)

    def test_garbage_falls_back_to_dark_text(self) -> None:
        # Invalid hex defaults to "not light", which means the dark
        # text variant (white-ish) is chosen. Better than crashing.
        self.assertEqual(gt.text_for_bg("nope"), gt._TEXT_ON_DARK)


class CommunityColorContrastTest(unittest.TestCase):
    """Every community color the Mermaid annotator uses must produce
    a readable contrast pair via text_for_bg."""

    # Mirrors graphify_gui.py PALETTE_HEX in _graph_to_mermaid_annotated.
    COMMUNITY_COLORS = (
        "#5ac6ff", "#ff7a90", "#7c5cff", "#5fd38f", "#ffb454",
        "#ff8b3d", "#3dd1c5", "#d2a4ff", "#ffd166", "#9bd4ff",
    )

    def test_each_color_yields_a_text_variant(self) -> None:
        for c in self.COMMUNITY_COLORS:
            text = gt.text_for_bg(c)
            self.assertIn(text, (gt._TEXT_ON_LIGHT, gt._TEXT_ON_DARK))

    def test_yellow_picks_dark_text_not_white(self) -> None:
        # The original bug: bright yellow (#ffd166) + ash text
        # (#e7ecf3) was unreadable. Confirm the fix flips to dark.
        self.assertEqual(gt.text_for_bg("#ffd166"), gt._TEXT_ON_LIGHT)

    def test_red_picks_dark_text_not_white(self) -> None:
        # Same bug for red community fills.
        self.assertEqual(gt.text_for_bg("#ff7a90"), gt._TEXT_ON_LIGHT)

    def test_purple_picks_light_text(self) -> None:
        # And the inverse: a dark community fill must keep light text.
        self.assertEqual(gt.text_for_bg("#7c5cff"), gt._TEXT_ON_DARK)


class UpdatePaletteInPlaceTest(unittest.TestCase):
    def test_preserves_identity(self) -> None:
        target = dict(gt.DARK_PALETTE)
        original_id = id(target)
        gt.update_palette_in_place(target, gt.LIGHT_PALETTE)
        self.assertEqual(id(target), original_id)
        self.assertEqual(target, gt.LIGHT_PALETTE)

    def test_clears_stale_keys(self) -> None:
        target = {"bg": "#000", "leftover": "should-go"}
        gt.update_palette_in_place(target, gt.LIGHT_PALETTE)
        self.assertNotIn("leftover", target)


if __name__ == "__main__":
    unittest.main(verbosity=2)

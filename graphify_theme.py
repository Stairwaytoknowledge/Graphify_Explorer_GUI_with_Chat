"""Themes for Graphify Explorer: dark, light, and auto-by-time.

The GUI keeps a single live `PALETTE` dict and applies it through ttk
styles plus a small registry of tk.Text / tk.Canvas widgets whose
colors are set as direct kwargs (not style-based). On theme change the
GUI swaps PALETTE in place, re-runs the style configuration, and walks
the registry.

Auto mode picks light during local daytime hours and dark otherwise.
The cutover is checked once a minute while the GUI is running so users
who leave the app open across sunset see the theme follow them.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Iterable

# ----- Palette definitions ----------------------------------------------
#
# Keys are stable; values change with mode. All keys present in every
# palette so callers can index PALETTE[key] without try/except.
#
# Color choices:
# - Dark: cool blue-grey background, high-contrast cyan accent
# - Light: warm off-white background, deeper cyan accent for legibility
# Contrast was checked against WCAG AA for the primary text/background
# pairs.

DARK_PALETTE: dict[str, str] = {
    "bg":        "#101826",
    "panel":     "#162033",
    "panel_alt": "#1d2a40",
    "fg":        "#e7ecf3",
    "fg_dim":    "#9aa6b8",
    "accent":    "#5ac6ff",
    "accent2":   "#7c5cff",
    "ok":        "#5fd38f",
    "warn":      "#ffb454",
    "edge":      "#33415a",
    "on_accent": "#0b1220",
}

LIGHT_PALETTE: dict[str, str] = {
    "bg":        "#f6f8fb",
    "panel":     "#eef1f7",
    "panel_alt": "#e2e7f0",
    "fg":        "#1a2233",
    "fg_dim":    "#5b6678",
    "accent":    "#1e7fb8",
    "accent2":   "#5b46c4",
    "ok":        "#118a4a",
    "warn":      "#b5650f",
    "edge":      "#aab4c3",
    "on_accent": "#ffffff",
}

# Recognised mode names. "auto" picks dark or light based on local time.
VALID_MODES: tuple[str, ...] = ("dark", "light", "auto")
DEFAULT_MODE = "dark"

# Daytime window for auto mode (local time, 24-hour). 7:00 -> 19:00 is
# light; everything else is dark.
DAYTIME_START_HOUR = 7
DAYTIME_END_HOUR = 19


def is_daytime(now: datetime | None = None) -> bool:
    """True if `now` (default: local clock) falls in the daytime window."""
    t = now if now is not None else datetime.now()
    return DAYTIME_START_HOUR <= t.hour < DAYTIME_END_HOUR


def palette_for_mode(mode: str, now: datetime | None = None) -> dict[str, str]:
    """Return a fresh copy of the palette appropriate for `mode`."""
    m = (mode or "").strip().lower()
    if m == "light":
        return dict(LIGHT_PALETTE)
    if m == "dark":
        return dict(DARK_PALETTE)
    if m == "auto":
        return dict(LIGHT_PALETTE if is_daytime(now) else DARK_PALETTE)
    return dict(DARK_PALETTE)


def resolve_mode(mode: str) -> str:
    """Canonicalize a mode string. Unknown -> the default."""
    m = (mode or "").strip().lower()
    return m if m in VALID_MODES else DEFAULT_MODE


def effective_mode(mode: str, now: datetime | None = None) -> str:
    """Resolve `mode` to either 'light' or 'dark' (auto picks one)."""
    m = resolve_mode(mode)
    if m == "auto":
        return "light" if is_daytime(now) else "dark"
    return m


# ----- Settings persistence ---------------------------------------------


SETTINGS_PATH = Path.home() / ".graphify" / "settings.json"


def load_settings(path: Path | None = None) -> dict:
    """Read settings.json; return {} on any failure."""
    p = path if path is not None else SETTINGS_PATH
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(data: dict, path: Path | None = None) -> bool:
    """Atomically merge `data` into settings.json. True on success."""
    p = path if path is not None else SETTINGS_PATH
    try:
        existing = load_settings(p)
        existing.update(data)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(existing, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, p)
        return True
    except OSError:
        return False


def load_theme_mode(path: Path | None = None) -> str:
    return resolve_mode(load_settings(path).get("theme_mode", DEFAULT_MODE))


def save_theme_mode(mode: str, path: Path | None = None) -> bool:
    return save_settings({"theme_mode": resolve_mode(mode)}, path)


# ----- Mermaid + vis.js theme bridge ------------------------------------


def mermaid_theme_for(mode_or_palette: str | dict[str, str]) -> str:
    """Return the Mermaid.js theme name (string) appropriate for the
    current palette or mode. Used when spawning the Mermaid subprocess.
    """
    if isinstance(mode_or_palette, dict):
        # Distinguish by background brightness.
        bg = mode_or_palette.get("bg", "#000")
        return "default" if _is_light(bg) else "dark"
    eff = effective_mode(mode_or_palette)
    return "default" if eff == "light" else "dark"


def _is_light(hex_color: str) -> bool:
    try:
        h = hex_color.lstrip("#")
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        # Standard luma formula.
        luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
        return luma > 128
    except (ValueError, IndexError):
        return False


# ----- Live update helpers ----------------------------------------------


def update_palette_in_place(target: dict[str, str], source: dict[str, str]) -> None:
    """Mutate `target` to match `source`, preserving its identity.

    Modules that imported the original PALETTE (and bound to that
    object) keep working after a theme switch because the dict
    instance doesn't change, only its contents.
    """
    target.clear()
    target.update(source)


def palette_keys() -> Iterable[str]:
    """Stable enumeration of keys present in every palette."""
    return tuple(DARK_PALETTE.keys())

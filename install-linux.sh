#!/usr/bin/env bash
# Linux installer. Run from a terminal: bash install-linux.sh
# Or make it double-clickable in your file manager (Nautilus, Dolphin, ...).
set -euo pipefail

cd "$(dirname "$0")"

echo
echo "============================================================"
echo "  Graphify Explorer  -  Linux installer"
echo "============================================================"
echo

# 1. Find python or uv -------------------------------------------------------
# We need a `PY` that points at a usable interpreter for pre-flight
# checks BEFORE the venv exists. With uv we still need a system python
# to do the GTK-WebKit / tkinter probes; use whichever python3 is on
# PATH if we can find one, else fall back to a placeholder that we'll
# only use after the venv is built.
USE_UV=0
if command -v uv >/dev/null 2>&1; then
    USE_UV=1
    echo "[1/5] Found uv. Using it for venv + install."
fi
if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif [[ "$USE_UV" -eq 0 ]]; then
    echo "ERROR: No python3 in PATH. Install Python 3.10+ via your package"
    echo "       manager (apt/dnf/pacman) or install uv from"
    echo "       https://docs.astral.sh/uv/"
    exit 1
else
    # uv present but no system python3 - skip pre-venv probes; we'll
    # use the venv's python after it's created.
    PY=""
fi

# Some distros split out venv/tk. Check for tkinter early when we have
# a system python to probe with; skip when uv-only with no system python.
if [[ -n "$PY" && "$USE_UV" -eq 0 ]]; then
    if ! "$PY" -c "import tkinter" >/dev/null 2>&1; then
        echo "ERROR: Python tkinter is missing."
        echo "       Debian/Ubuntu:  sudo apt install python3-tk python3-venv"
        echo "       Fedora/RHEL:    sudo dnf install python3-tkinter"
        echo "       Arch:           sudo pacman -S tk"
        exit 1
    fi
fi

# 2. Create venv -------------------------------------------------------------
if [[ -x ".venv/bin/python" ]]; then
    echo "[2/5] Reusing existing .venv/"
else
    if [[ "$USE_UV" -eq 1 ]]; then
        uv venv .venv --python 3.12 || uv venv .venv
    else
        "$PY" -m venv .venv
    fi
    echo "[2/5] Created .venv/"
fi

# 3a. Pre-flight: GTK WebKit is needed for pywebview on Linux ---------------
# Only probe if we have a system python to probe with. Even when this
# probe is skipped, the install proceeds; the runtime warns clearly if
# WebKit is missing when the user tries to open the Interactive Graph.
WEBKIT_OK=0
if [[ -n "$PY" ]]; then
    if "$PY" -c "import gi; gi.require_version('WebKit2', '4.1'); from gi.repository import WebKit2" >/dev/null 2>&1 \
       || "$PY" -c "import gi; gi.require_version('WebKit2', '4.0'); from gi.repository import WebKit2" >/dev/null 2>&1; then
        WEBKIT_OK=1
    fi
fi
if [[ "$WEBKIT_OK" -eq 0 ]]; then
    echo
    echo "============================================================"
    echo "  WARNING: GTK WebKit not detected."
    echo "============================================================"
    echo "  The Interactive Graph window needs GTK WebKit. Install it:"
    echo "    Debian/Ubuntu:  sudo apt install python3-gi gir1.2-webkit2-4.0"
    echo "    Fedora:         sudo dnf install python3-gobject webkit2gtk4.0"
    echo "    Arch:           sudo pacman -S python-gobject webkit2gtk-4.1"
    echo "  Then re-run this installer."
    echo
    echo "  The installer will continue anyway. The GUI will fail to"
    echo "  open the graph viewer until WebKit is installed."
    echo
fi

# 3b. Install dependencies (prefer locked file for reproducibility) ---------
REQ_FILE="requirements.txt"
if [[ -f "requirements.lock" ]]; then
    REQ_FILE="requirements.lock"
fi
echo "[3/5] Installing dependencies from $REQ_FILE..."
set +e  # we want to handle the failure ourselves
if [[ "$USE_UV" -eq 1 ]]; then
    uv pip install --python ".venv/bin/python" -r "$REQ_FILE"
    rc=$?
else
    ".venv/bin/python" -m pip install --upgrade pip >/dev/null
    ".venv/bin/python" -m pip install -r "$REQ_FILE"
    rc=$?
fi
set -e
if [[ $rc -ne 0 ]]; then
    cat <<'ERR'

============================================================
  ERROR: dependency install failed.
============================================================

What this means:
  At least one required package could not be installed. The GUI
  needs all of these to run:
    - graphifyy   (the upstream graph engine)
    - watchdog    (used by `graphify watch`)
    - numpy       (used by the Insights tab's PageRank)
    - pywebview   (Interactive Graph window; needs GTK WebKit on Linux)

How to fix on Linux:
  1. Make sure system packages are installed:
       Debian/Ubuntu:  sudo apt install python3-gi gir1.2-webkit2-4.0 python3-tk
       Fedora:         sudo dnf install python3-gobject webkit2gtk4.0 python3-tkinter
       Arch:           sudo pacman -S python-gobject webkit2gtk-4.1 tk
  2. Make sure you have an internet connection (PyPI is needed).
  3. Re-run this installer. If it still fails, scroll up and read
     the last few lines of pip's output - the missing package
     name is usually right above the traceback.

See README.md (Troubleshooting) for more options.

ERR
    exit 1
fi

# Sanity check: pywebview should be importable, but a missing GTK WebKit
# is recoverable (the rest of the GUI works; only the graph viewer needs
# webview). Warn instead of failing the install so headless / system
# environments without WebKit can still get the rest installed.
if ! ".venv/bin/python" -c "import webview" >/dev/null 2>&1; then
    cat <<'WARN'

============================================================
  WARNING: pywebview installed but cannot be imported.
============================================================
  The Interactive Graph window will not open until GTK WebKit
  is installed:
    Debian/Ubuntu:  sudo apt install python3-gi gir1.2-webkit2-4.1
                    (or gir1.2-webkit2-4.0 on older releases)
    Fedora:         sudo dnf install python3-gobject webkit2gtk4.0
    Arch:           sudo pacman -S python-gobject webkit2gtk-4.1
  Everything else (graph build, query, Insights tab, chat) will
  work without it.

WARN
fi

# 4. Icon + .desktop entry --------------------------------------------------
echo "[4/5] Generating icon and .desktop entry..."
".venv/bin/python" make_icon.py || true

HERE="$(pwd)"
DESKTOP_FILE="$HERE/Graphify.desktop"
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=Graphify Explorer
Comment=Knowledge-graph GUI for any folder
Exec=$HERE/Graphify.sh %F
Icon=$HERE/icon.png
Terminal=false
Categories=Development;Utility;
StartupNotify=true
EOF
chmod +x "$DESKTOP_FILE"

# Direct double-click launcher
cat > Graphify.sh <<EOF
#!/usr/bin/env bash
cd "\$(dirname "\$0")"
exec ./.venv/bin/python ./graphify_gui.py "\$@"
EOF
chmod +x Graphify.sh

# Best-effort: install the .desktop into the user's apps folder.
USER_APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$USER_APPS"
cp "$DESKTOP_FILE" "$USER_APPS/graphify-explorer.desktop"
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$USER_APPS" >/dev/null 2>&1 || true
fi

echo "[5/5] Done."
echo
echo "============================================================"
echo "  Install complete."
echo "  - Launch from your apps menu: Graphify Explorer"
echo "  - Or double-click Graphify.sh / Graphify.desktop"
echo "============================================================"

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
USE_UV=0
if command -v uv >/dev/null 2>&1; then
    USE_UV=1
    echo "[1/5] Found uv. Using it for venv + install."
else
    if command -v python3 >/dev/null 2>&1; then
        PY=python3
    else
        echo "ERROR: No python3 in PATH. Install Python 3.10+ via your package"
        echo "       manager (apt/dnf/pacman) or install uv from"
        echo "       https://docs.astral.sh/uv/"
        exit 1
    fi
    echo "[1/5] Using $PY to create the venv."
fi

# Some distros split out venv/tk. Check for tkinter early so the user
# gets a useful message instead of a confusing import error later.
if [[ "$USE_UV" -eq 0 ]]; then
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

# 3. Install graphifyy -------------------------------------------------------
echo "[3/5] Installing graphifyy into .venv..."
if [[ "$USE_UV" -eq 1 ]]; then
    uv pip install --python ".venv/bin/python" -r requirements.txt
else
    ".venv/bin/python" -m pip install --upgrade pip >/dev/null
    ".venv/bin/python" -m pip install -r requirements.txt
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

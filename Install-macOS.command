#!/usr/bin/env bash
# Double-click installer for macOS.
# Right-click -> Open the first time (Gatekeeper).
set -euo pipefail

cd "$(dirname "$0")"

echo
echo "============================================================"
echo "  Graphify Explorer  -  macOS installer"
echo "============================================================"
echo

# 1. Find python or uv -------------------------------------------------------
USE_UV=0
if command -v uv >/dev/null 2>&1; then
    USE_UV=1
    echo "[1/6] Found uv. Using it for venv + install."
else
    if command -v python3 >/dev/null 2>&1; then
        PY=python3
    else
        echo "ERROR: No python3 in PATH. Install Python 3.10+ from python.org"
        echo "       or install uv from https://docs.astral.sh/uv/"
        read -r -p "Press Enter to close..." _
        exit 1
    fi
    echo "[1/6] Using $PY to create the venv."
fi

# 2. Create venv -------------------------------------------------------------
if [[ -x ".venv/bin/python" ]]; then
    echo "[2/6] Reusing existing .venv/"
else
    if [[ "$USE_UV" -eq 1 ]]; then
        uv venv .venv --python 3.12 || uv venv .venv
    else
        "$PY" -m venv .venv
    fi
    echo "[2/6] Created .venv/"
fi

# 3. Install graphifyy -------------------------------------------------------
echo "[3/6] Installing graphifyy into .venv (this may take a minute)..."
if [[ "$USE_UV" -eq 1 ]]; then
    uv pip install --python ".venv/bin/python" -r requirements.txt
else
    ".venv/bin/python" -m pip install --upgrade pip >/dev/null
    ".venv/bin/python" -m pip install -r requirements.txt
fi

# 4. Generate icon -----------------------------------------------------------
echo "[4/6] Generating icon..."
".venv/bin/python" make_icon.py || true

# 5. Build a tiny .app bundle so users can double-click with the icon -------
echo "[5/6] Building Graphify Explorer.app bundle..."
APP="Graphify Explorer.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Graphify Explorer</string>
  <key>CFBundleDisplayName</key><string>Graphify Explorer</string>
  <key>CFBundleIdentifier</key><string>com.graphify.explorer</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleExecutable</key><string>graphify-launch</string>
  <key>CFBundleIconFile</key><string>icon.icns</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>10.13</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

HERE_ABS="$(pwd)"
cat > "$APP/Contents/MacOS/graphify-launch" <<EOF
#!/usr/bin/env bash
cd "$HERE_ABS"
exec "$HERE_ABS/.venv/bin/python" "$HERE_ABS/graphify_gui.py" "\$@"
EOF
chmod +x "$APP/Contents/MacOS/graphify-launch"

# Create .icns from the PNG (uses iconutil if available, else copies PNG).
if command -v sips >/dev/null 2>&1 && command -v iconutil >/dev/null 2>&1; then
    ICONSET="$(mktemp -d)/icon.iconset"
    mkdir -p "$ICONSET"
    for sz in 16 32 64 128 256 512; do
        sips -z "$sz" "$sz" icon.png --out "$ICONSET/icon_${sz}x${sz}.png" >/dev/null
        sips -z "$((sz*2))" "$((sz*2))" icon.png --out "$ICONSET/icon_${sz}x${sz}@2x.png" >/dev/null
    done
    iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/icon.icns"
    rm -rf "$ICONSET"
else
    cp icon.png "$APP/Contents/Resources/icon.icns" || true
fi

# 6. Also drop a .command direct launcher -----------------------------------
cat > Graphify.command <<EOF
#!/usr/bin/env bash
cd "\$(dirname "\$0")"
exec ./.venv/bin/python ./graphify_gui.py "\$@"
EOF
chmod +x Graphify.command
chmod +x "$APP/Contents/MacOS/graphify-launch"

echo "[6/6] Done."
echo
echo "============================================================"
echo "  Install complete."
echo "  - Double-click 'Graphify Explorer.app' (with icon), or"
echo "    double-click 'Graphify.command'."
echo "============================================================"
echo
read -r -p "Press Enter to close..." _

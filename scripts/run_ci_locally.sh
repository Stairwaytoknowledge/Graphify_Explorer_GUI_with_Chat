#!/usr/bin/env bash
# Run the same checks as .github/workflows/ci.yml on the current machine.
#
# Usage:
#   bash scripts/run_ci_locally.sh
#
# Returns non-zero on the first failing step. Skips OS-specific steps
# that don't apply.
set -uo pipefail

cd "$(dirname "$0")/.."

PASS=0
FAIL=0
SKIP=0
FAILED_STEPS=()

step() {
    local name=$1
    shift
    printf "\n>>> %s\n" "$name"
    if "$@"; then
        printf "    OK\n"
        PASS=$((PASS + 1))
    else
        printf "    FAIL\n"
        FAIL=$((FAIL + 1))
        FAILED_STEPS+=("$name")
    fi
}

skip() {
    printf "\n--- skip: %s\n" "$1"
    SKIP=$((SKIP + 1))
}

# ----------------------------- OS detection ---------------------------------
case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*)  OS=windows ;;
    Darwin*)               OS=mac ;;
    Linux*)                OS=linux ;;
    *)                     OS=unknown ;;
esac
echo "Running local CI on: $OS ($(uname -srm))"

# ---------------------- 1. Validate workflow YAML ---------------------------
PYBIN=$(command -v python || command -v python3)
[[ -z "${PYBIN:-}" ]] && { echo "no python in PATH"; exit 2; }
step "lint .github/workflows/ci.yml" "$PYBIN" -c "
import sys
try:
    import yaml
except ImportError:
    print('PyYAML not available; using basic structural check', file=sys.stderr)
    raw = open('.github/workflows/ci.yml').read()
    assert 'jobs:' in raw and 'steps:' in raw, 'workflow malformed'
    sys.exit(0)
d = yaml.safe_load(open('.github/workflows/ci.yml'))
assert 'jobs' in d, 'no jobs key'
assert d['jobs'], 'no jobs defined'
job = next(iter(d['jobs'].values()))
assert 'steps' in job and isinstance(job['steps'], list), 'no steps list'
print(f'workflow: {len(job[\"steps\"])} steps in {len(d[\"jobs\"])} job(s)')
"

# ---------------------- 2. Run the OS-matching installer --------------------
case "$OS" in
    windows)
        step "Run Install-Windows.bat" \
            env CI=1 NONINTERACTIVE=1 cmd //c ".\\Install-Windows.bat"
        ;;
    mac)
        step "Run Install-macOS.command" \
            bash -c "chmod +x Install-macOS.command && printf '\n' | bash Install-macOS.command"
        ;;
    linux)
        step "Run install-linux.sh" \
            bash -c "chmod +x install-linux.sh && bash install-linux.sh"
        ;;
    *) skip "no installer for $OS" ;;
esac

# ---------------------- 3. Verify icons --------------------------------------
step "icon files generated" bash -c '
test -f icon.png && test -f icon.ico
'

# ---------------------- 4. Verify graphify CLI -------------------------------
if [[ "$OS" == "windows" ]]; then
    PY=".venv/Scripts/python.exe"
    GRAPHIFY=".venv/Scripts/graphify.exe"
else
    PY=".venv/bin/python"
    GRAPHIFY=".venv/bin/graphify"
fi

step "graphify importable + CLI exists" bash -c "
'$PY' -c 'import graphify; print(graphify.__file__)' &&
test -e '$GRAPHIFY'
"

# ---------------------- 5. GUI smoke test ------------------------------------
GUI_SMOKE() {
    if [[ "$OS" == "linux" ]]; then
        if ! pgrep -x Xvfb >/dev/null; then
            Xvfb :99 -screen 0 1024x768x24 &
            sleep 1
        fi
        export DISPLAY=:99
    fi
    "$PY" - <<'PY'
import graphify_gui
import tkinter as tk
root = tk.Tk()
graphify_gui.GraphifyApp(root)
root.after(500, root.destroy)
root.mainloop()
PY
}
step "GUI smoke (headless Tk)" GUI_SMOKE

# ---------------------- 6. Unit tests for input helpers ----------------------
step "URL/path/network helpers" "$PY" - <<'PY'
import graphify_gui as g
for s in ["https://github.com/x/y", "git@github.com:x/y.git",
          "ssh://git@h/x/y", "user@server:repo.git"]:
    assert g.is_url(s), f"expected URL: {s!r}"
for s in ["/home/x/repo", "C:/Code/foo", "./repo"]:
    assert not g.is_url(s), f"expected path: {s!r}"
d = g.derive_clone_dest("https://github.com/foo/bar")
assert d.parts[-2:] == ("foo", "bar"), d
d = g.derive_clone_dest("git@github.com:foo/bar.git")
assert d.parts[-2:] == ("foo", "bar"), d
print("helpers OK")
PY

# ---------------------- 7. End-to-end build + query --------------------------
E2E() {
    rm -rf e2e
    mkdir -p e2e/src
    cat > e2e/src/a.py <<'PY'
def hello(): return 1 + 1
PY
    cat > e2e/src/b.py <<'PY'
from a import hello
def world(): return hello() * 2
PY
    (cd e2e && "../$GRAPHIFY" update .) >/dev/null
    test -f e2e/graphify-out/graph.json || return 1
    test -f e2e/graphify-out/graph.html || return 1
    "$GRAPHIFY" --help | grep -q query
    (cd e2e && "../$GRAPHIFY" query "what does hello do?") >/dev/null
    rm -rf e2e
}
step "End-to-end build + query" E2E

# ---------------------- 8. Custom output redirect ---------------------------
CUSTOM_OUT() {
    rm -rf src_test out_test
    mkdir -p src_test out_test
    echo 'def fn(): return 42' > src_test/m.py
    "$PY" - <<'PY'
import graphify_gui, tkinter as tk
from pathlib import Path
root = tk.Tk()
app = graphify_gui.GraphifyApp(root)
app.path_var.set("src_test")
app.output_dir_var.set("out_test")
app._maybe_link_output(Path("src_test").resolve())
root.destroy()
PY
    (cd src_test && "../$GRAPHIFY" update .) >/dev/null
    test -f out_test/graph.json || { rm -rf src_test out_test; return 1; }
    rm -rf src_test out_test
}
step "Custom output redirect (junction/symlink)" CUSTOM_OUT

# ---------------------- 9. Launcher shape -----------------------------------
LAUNCHER_SHAPE() {
    case "$OS" in
        windows)
            test -f Graphify.vbs && test -f Graphify.bat &&
            grep -q 'pythonw.exe' Graphify.vbs &&
            grep -q 'graphify_gui.py' Graphify.vbs
            ;;
        mac)
            test -f Graphify.command && test -x Graphify.command &&
            test -d "Graphify Explorer.app" &&
            test -x "Graphify Explorer.app/Contents/MacOS/graphify-launch"
            ;;
        linux)
            test -f Graphify.sh && test -x Graphify.sh &&
            test -f Graphify.desktop &&
            grep -q "Graphify.sh" Graphify.desktop
            ;;
        *) return 0 ;;
    esac
}
step "Launcher script shape" LAUNCHER_SHAPE

# ---------------------- 10. Launcher round-trip -----------------------------
LAUNCHER_RT() {
    if [[ "$OS" == "linux" ]]; then
        if ! pgrep -x Xvfb >/dev/null; then
            Xvfb :99 -screen 0 1024x768x24 &
            sleep 1
        fi
        export DISPLAY=:99
    fi
    export GRAPHIFY_TEST_AUTOQUIT=1
    case "$OS" in
        windows) cmd //c ".\\Graphify.bat" ;;
        mac)     ./Graphify.command ;;
        linux)   ./Graphify.sh ;;
        *)       return 0 ;;
    esac
}
step "Launcher round-trip (autoquit)" LAUNCHER_RT

# ---------------------- 11. Benchmarks --------------------------------------
step "Reproducible benchmark runs" bash -c "
'$PY' benchmarks/compare.py \
    --question 'What does itsdangerous protect against?' >/dev/null 2>&1 &&
test -f docs/COMPARISON.md
"

# ---------------------- summary ---------------------------------------------
printf "\n=================================\n"
printf "Pass: %d   Fail: %d   Skip: %d\n" "$PASS" "$FAIL" "$SKIP"
if (( FAIL > 0 )); then
    printf "Failed steps:\n"
    for s in "${FAILED_STEPS[@]}"; do printf "  - %s\n" "$s"; done
    exit 1
fi
exit 0

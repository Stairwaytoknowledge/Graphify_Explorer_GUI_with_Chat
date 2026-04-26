# Graphify Explorer

A small, **100% self-contained**, **double-click-installable** GUI front-end
for [**Graphify**](https://github.com/safishamsi/graphify) — the tool that
turns any folder of code, docs, papers, images, or videos into a queryable
knowledge graph.

> **Credits / upstream:** all the heavy lifting is done by Graphify, by
> [**Safi Shamsi (@safishamsi)**](https://github.com/safishamsi).
> Repo: <https://github.com/safishamsi/graphify> · PyPI:
> [`graphifyy`](https://pypi.org/project/graphifyy/).
> This project is just a thin Tkinter wrapper + cross-platform installer
> around the `graphify` CLI it ships. If you find it useful, please star
> the upstream project.

![Icon](icon.png)

---

## What this gives you

- **One double-click installer per OS** — Windows, macOS, Linux.
- A **GUI with an icon** (no terminal needed once installed):
  - Pick any folder, then **Build Graph**, **Update**, **Query**, **Explain**,
    or **Path Between** two nodes.
  - **Open Visualization** opens the generated `graph.html` in your browser.
  - **Open Report** opens `GRAPH_REPORT.md`.
- A self-contained Python virtual environment in `.venv/` next to the
  installer — nothing is installed system-wide.
- Cross-platform GitHub Actions CI that **proves** the installer runs and
  the GUI opens on Windows, macOS, and Linux.

---

## Quick start

### Windows

1. Make sure Python 3.10+ is installed (<https://www.python.org/downloads/>),
   *or* install [`uv`](https://docs.astral.sh/uv/) — either is enough.
2. **Double-click `Install-Windows.bat`.**
3. The installer creates `.venv\`, installs `graphifyy`, generates `icon.ico`,
   and drops a **"Graphify Explorer"** shortcut on your Desktop with the icon.
4. Double-click that shortcut (or `Graphify.vbs` in this folder) to open the
   GUI without a terminal window.

### macOS

1. Make sure Python 3.10+ is installed, *or* `uv`.
2. **Double-click `Install-macOS.command`** (the first time, right-click →
   *Open* to bypass Gatekeeper).
3. The installer builds `Graphify Explorer.app` next to itself, with an `.icns`
   icon. Double-click it like any other Mac app.
4. A `Graphify.command` direct launcher is also produced.

### Linux

1. Install Python 3.10+, Tk and venv from your package manager:

   - Debian/Ubuntu: `sudo apt install python3 python3-tk python3-venv`
   - Fedora/RHEL: `sudo dnf install python3 python3-tkinter`
   - Arch: `sudo pacman -S python tk`

2. Run `bash install-linux.sh` (or make it double-clickable in your file
   manager). The installer registers a `Graphify Explorer` entry in your apps
   menu and writes `Graphify.sh` / `Graphify.desktop` in this folder.

---

## Using the GUI

| Control                     | What it runs (under the hood)                         |
| --------------------------- | ------------------------------------------------------ |
| **Browse…**                 | Pick the folder you want to analyze.                   |
| **Build / Refresh Graph**   | `graphify update <folder>` (re-extracts code, no LLM)  |
| **Watch (live rebuild)**    | `graphify watch <folder>` — rebuilds on every save     |
| **Open Visualization**      | Opens `<folder>/graphify-out/graph.html`               |
| **Open Report**             | Opens `<folder>/graphify-out/GRAPH_REPORT.md`          |
| **Query** (with text)       | `graphify query "<text>"`                              |
| **Explain** (with text)     | `graphify explain "<text>"`                            |
| **Path Between (A\|B)**     | `graphify path "A" "B"` — type `A|B` in the box        |
| **Stop**                    | Terminates the running graphify subprocess.            |

> **Note on "deep" / multimodal mode:** the upstream `--mode deep` and
> doc/paper/image semantic extraction live behind the `/graphify` slash
> command in Claude Code (which is what calls Claude). The bare CLI used by
> this GUI builds and queries the **code graph** locally without any API
> key. For multimodal corpora, install Graphify as a Claude Code skill
> (`graphify install` after `pip install graphifyy`) and use `/graphify`.

Output (stdout + stderr) is streamed live into the bottom pane. The GUI never
blocks: each command runs in a background thread.

### Where outputs land

Graphify writes everything into `<your-folder>/graphify-out/`:

- `graph.html` — interactive vis.js visualization
- `graph.json` — persistent graph (used for query/update across sessions)
- `GRAPH_REPORT.md` — top concepts, communities, suggested questions
- `obsidian/` — Obsidian vault (if produced by upstream)
- `wiki/` — wiki articles (if produced by upstream)

---

## API keys / model access

Graphify uses Claude / GPT for semantic extraction on docs, images, and
videos. The GUI passes your environment through to the subprocess unchanged,
so set whatever Graphify needs **before** launching the GUI:

- Windows (CMD): `setx ANTHROPIC_API_KEY "sk-ant-..."` then re-open the shortcut.
- macOS / Linux: `export ANTHROPIC_API_KEY=sk-ant-...` in your shell rc.

Pure-code repos can be analyzed locally with tree-sitter alone — no key needed.

---

## Repo layout

```
.
├── graphify_gui.py        Tkinter GUI; calls the `graphify` CLI as a subprocess
├── make_icon.py           Stdlib-only generator for icon.png + icon.ico
├── icon.png / icon.ico    Generated icons (regenerated by the installers)
├── requirements.txt       Pins `graphifyy` (the upstream PyPI package)
│
├── Install-Windows.bat    Double-click installer (Windows)
├── Graphify.vbs           Silent (no console) GUI launcher
├── Graphify.bat           Visible-console GUI launcher
│
├── Install-macOS.command  Double-click installer (macOS) — builds .app bundle
│
├── install-linux.sh       Installer for Linux — registers a .desktop entry
│
└── .github/workflows/ci.yml   Matrix CI: Windows + macOS + Linux × Py 3.11/3.12
```

---

## How it works (one paragraph)

`Install-*` scripts detect either [`uv`](https://docs.astral.sh/uv/) (preferred,
faster) or `python -m venv` (fallback), build a `.venv` next to themselves,
install `graphifyy` from PyPI, and generate icon assets. Each OS then gets
the most native double-click experience that doesn't require code-signing:
a Desktop `.lnk` with embedded `.ico` (Windows), a real `.app` bundle with
`.icns` (macOS), and an `XDG` `.desktop` entry pointing at `icon.png` (Linux).
The GUI itself is a single Tkinter file that shells out to the `graphify`
binary inside `.venv`, streaming stdout into a scrollable pane.

---

## CI / "provable in GitHub Actions"

`.github/workflows/ci.yml` runs the **real installer** on each OS:

- `ubuntu-latest`, `windows-latest`, `macos-latest`
- Python 3.11 and 3.12

For each combination it:

1. Executes the OS-specific installer (`Install-Windows.bat`,
   `Install-macOS.command`, or `install-linux.sh`).
2. Verifies `icon.png` and `icon.ico` were generated.
3. Verifies the `graphify` CLI is importable and the console script exists
   inside `.venv`.
4. Boots a headless Tk window (Xvfb on Linux) and instantiates `GraphifyApp`
   to confirm the GUI actually constructs end-to-end.
5. Uploads installer artefacts.

That's the proof.

---

## Uninstall

There is nothing system-wide. Delete this folder and remove the entries the
installer created:

- Windows: `%USERPROFILE%\Desktop\Graphify Explorer.lnk`
- macOS: drag `Graphify Explorer.app` to Trash.
- Linux: `rm ~/.local/share/applications/graphify-explorer.desktop`

---

## License & credits

This wrapper is provided as-is (MIT). All credit for the underlying
knowledge-graph engine — including the multimodal extractors, Leiden
clustering, tree-sitter wiring, query interface, and visualization — belongs
to **Safi Shamsi**:

- GitHub: <https://github.com/safishamsi>
- Project: <https://github.com/safishamsi/graphify>
- PyPI: <https://pypi.org/project/graphifyy/>

If you ship this further, keep these links visible.

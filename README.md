# Graphify Explorer

Desktop GUI around the [graphify](https://github.com/safishamsi/graphify) CLI.
Point it at a local folder or a git URL and you get an interactive
knowledge graph, a node-by-node detail view, and an optional chat tab
backed by a local Ollama model.

The graph engine, the extraction, the clustering, the query algorithm,
the vis.js HTML and the GRAPH_REPORT format are all upstream's work
([Safi Shamsi](https://github.com/safishamsi),
[`graphifyy`](https://pypi.org/project/graphifyy/) on PyPI). What this
repo adds is packaging, a desktop UI, and a local-LLM chat layer on top
of it. See [`docs/COMPARISON.md`](docs/COMPARISON.md) for measured numbers.

![Icon](icon.png)

## Install

Pick the file for your OS and double-click it. Each one creates a
`.venv` next to itself, installs `graphifyy` + `matplotlib` + `watchdog`,
generates the icon, and registers a launcher.

| OS      | File                  | What it produces                         |
| ------- | --------------------- | ---------------------------------------- |
| Windows | `Install-Windows.bat` | Desktop shortcut → `Graphify.vbs`        |
| macOS   | `Install-macOS.command` | `Graphify Explorer.app` bundle         |
| Linux   | `install-linux.sh`    | `~/.local/share/applications/*.desktop`  |

Requirements:
- Python 3.10+ *or* [`uv`](https://docs.astral.sh/uv/) on PATH.
- `git` on PATH (only needed if you want to graph remote URLs).
- Tkinter. On most distros it ships with Python; on Debian/Ubuntu install
  `python3-tk` if missing.

## Use

The input field accepts either:

- a local folder path (`C:\Code\my-project`, `/home/me/repo`)
- a git URL (`https://github.com/<owner>/<repo>`,
  `git@github.com:<owner>/<repo>.git`, `ssh://...`)

For URLs the wrapper does a shallow `git clone` first, into
`~/.graphify/repos/<owner>/<repo>/`. The graph artifacts land in
`graphify-out/` next to the source. The full destination is logged in the
Output tab before the clone starts, and the **Show in Files** button opens
it in the OS file manager.

| Button                   | Runs                                          |
| ------------------------ | --------------------------------------------- |
| Build / Refresh Graph    | `git clone` (if URL) then `graphify update`   |
| Watch                    | `graphify watch <folder>`                     |
| Reload View              | Re-reads `graph.json` into the inline viewer  |
| Open HTML                | Opens `graphify-out/graph.html` in a browser  |
| Open Report              | Opens `graphify-out/GRAPH_REPORT.md`          |
| Show in Files            | Opens the folder in Explorer/Finder/xdg-open  |
| Query / Explain / Path   | `graphify query / explain / path`             |

The right-hand pane is a notebook with three tabs:

- **Details**: clicked-node label, source file, community, neighbours.
- **Chat (local LLM)**: see below.
- **Output**: streaming stdout/stderr from any subprocess the GUI spawned.

### The chat tab

If [Ollama](https://ollama.com) is running on `localhost:11434`, the chat
tab discovers your installed models and lets you ask questions about the
loaded repo. Each question runs `graphify query` first to pull a small
BFS context out of the graph, then sends `{system, context, question}` to
the local model. Answers stream back token-by-token.

```
ollama pull qwen2.5:7b      # or qwen3-coder:30b, llama3.2:3b, etc.
```

If Ollama isn't running, the chat tab tells you so and the rest of the
GUI works as before.

The wrapper never bundles a model. Nothing is sent to a hosted API.

## Graph rendering caveat

The inline matplotlib view is capped at 300 nodes (top-N by degree). For
larger graphs, click **Open HTML** for the upstream vis.js view, which
handles thousands of nodes well.

## Repo layout

```
graphify_gui.py        Tkinter GUI; subprocesses graphify and Ollama
make_icon.py           Generates icon.png and icon.ico (stdlib only)
requirements.txt       graphifyy, matplotlib, watchdog
Install-Windows.bat    Win installer + Desktop shortcut
Graphify.vbs / .bat    Win launchers
Install-macOS.command  macOS installer + .app bundle
install-linux.sh       Linux installer + .desktop entry
benchmarks/compare.py  Reproducible measurements (see COMPARISON.md)
docs/COMPARISON.md     What's actually different vs upstream, with numbers
.github/workflows/ci.yml  Win/macOS/Linux × Py 3.11/3.12 matrix
```

## CI

`.github/workflows/ci.yml` runs the actual installer on each OS, builds
a real graph from a small Python tree, and runs `query` + `explain`
against it. Linux uses Xvfb to give Tkinter a display. The matrix is
`{ubuntu-latest, windows-latest, macos-latest} × {3.11, 3.12}`.

## Uninstall

Nothing is installed system-wide. Delete this folder and remove:

- Windows: `%USERPROFILE%\Desktop\Graphify Explorer.lnk`
- macOS: `Graphify Explorer.app`
- Linux: `~/.local/share/applications/graphify-explorer.desktop`
- Optional: `~/.graphify/repos/` (cloned source trees)

## Credits

- Graphify: <https://github.com/safishamsi/graphify>
- Author: <https://github.com/safishamsi>
- PyPI: <https://pypi.org/project/graphifyy/>

This wrapper is MIT.

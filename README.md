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

The input field accepts:

- a local folder path (`C:\Code\my-project`, `/home/me/repo`)
- a git URL: `https://github.com/<owner>/<repo>`,
  `git@github.com:<owner>/<repo>.git`, `ssh://...`, `git://...`,
  or SCP-style `user@host:/path/to/repo.git`
- a network path: Windows UNC (`\\server\share\repo`), mapped drives
  (`Z:\repo`), macOS SMB mounts (`/Volumes/...`), Linux NFS/SMB mounts
  (`/mnt/...`, `/media/...`). Network paths produce a status-bar warning
  about slower scans and write-back to the share.

For URLs the wrapper does a shallow `git clone` first into
`~/.graphify/repos/<owner>/<repo>/`. By default graph artifacts land in
`graphify-out/` next to the source. The full destination is logged in the
Output tab before the clone starts, and **Show in Files** opens it.

If you point at a `.git/` directory by accident the wrapper redirects to
the repo root.

### Custom output directory

The **Output (optional)** field above the action buttons takes any
directory you want. When set, the wrapper transparently redirects
graphify's hardcoded `<source>/graphify-out` to your chosen location via:

- a directory junction on Windows (`mklink /J`, no admin needed)
- a symlink on macOS / Linux (`os.symlink`)

Bytes physically land in your chosen folder; graphify never knows the
difference. Leave the field empty to keep the default behaviour.

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
loaded repo.

```
ollama pull qwen2.5:7b      # or qwen3-coder:30b, llama3.2:3b, etc.
```

How a question is grounded:

1. Run `graphify query` to pull the BFS slice of the graph relevant to
   the question.
2. For every node mentioned in that slice, read 12 lines of actual
   source from the file/line recorded in the graph (capped at 6
   snippets).
3. Append a slice of `GRAPH_REPORT.md` for high-level concepts.
4. Send the bundle to the local model with `temperature=0.1`, a strict
   system prompt that demands `[node_id]` citations, and a refusal
   instruction: if the answer is not in the context, the model is told
   to reply "I don't know based on the graph."

Trade-off: lower hallucination, but answers are bounded by what
graphify's BFS surfaced. For better recall, increase the `--budget`
default in `graphify query` or run an explicit `Explain` first on a
seed node.

If Ollama isn't running, the chat tab tells you so and the rest of the
GUI works as before. Nothing is sent to a hosted API; nothing leaves the
machine.

### Progress and completion

While a build, clone, or query is running, the status bar shows
`graphify update... working - elapsed M:SS`. On exit it shows
`Done in M:SS` and fires a system beep + brings the window to the front
so you don't have to keep watching.

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
a real graph, and exercises every behaviour the README claims. Matrix:
`{ubuntu-latest, windows-latest, macos-latest} × {3.11, 3.12}`.

For each cell it:

1. Runs the OS-specific installer end-to-end.
2. Verifies icons, the `graphify` CLI, and the GUI Tk window construct.
3. Runs `graphify update / query / explain` on a small Python tree.
4. Runs unit asserts on `is_url`, `looks_like_network_path`, and
   `derive_clone_dest`.
5. Sets a custom output directory, builds a graph, asserts the bytes
   landed in the custom dir (proves the junction/symlink redirect).
6. Verifies each launcher (`Graphify.bat`, `Graphify.command`,
   `Graphify.sh`, the macOS `.app` bundle, the Linux `.desktop` entry)
   exists and points at the right Python.
7. Invokes the launcher with `GRAPHIFY_TEST_AUTOQUIT=1` so it opens the
   GUI, waits 800 ms, and exits. The launcher round-trip returning 0 is
   the "double-click works" proof.
8. Re-runs `benchmarks/compare.py` so `docs/COMPARISON.md` stays in
   sync with the wrapper's behaviour.

Linux uses Xvfb for the headless display. macOS uses its real Aqua
session. Windows uses the real Tk that ships with Python.

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

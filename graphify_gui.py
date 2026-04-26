"""Graphify Explorer — a Tkinter GUI front-end for the `graphify` CLI.

Point it at any folder, build a knowledge graph, then ask questions.
"""

from __future__ import annotations

import os
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from tkinter import (
    BOTH,
    DISABLED,
    END,
    LEFT,
    NORMAL,
    RIGHT,
    Tk,
    StringVar,
    Text,
    filedialog,
    messagebox,
    ttk,
)


APP_DIR = Path(__file__).resolve().parent
ICON_ICO = APP_DIR / "icon.ico"
ICON_PNG = APP_DIR / "icon.png"


def graphify_executable() -> str | None:
    """Find the `graphify` console script. Prefer the venv next to this file."""
    candidates: list[Path] = []
    venv = APP_DIR / ".venv"
    if os.name == "nt":
        candidates += [venv / "Scripts" / "graphify.exe", venv / "Scripts" / "graphify"]
    else:
        candidates += [venv / "bin" / "graphify"]
    for c in candidates:
        if c.exists():
            return str(c)
    found = shutil.which("graphify")
    return found


class GraphifyApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title("Graphify Explorer")
        self.root.geometry("900x640")
        self.root.minsize(720, 520)
        self._apply_icon()

        self.path_var = StringVar(value=str(Path.home()))
        self.query_var = StringVar()
        self.mode_var = StringVar(value="normal")
        self.status_var = StringVar(value="Ready.")
        self.proc: subprocess.Popen | None = None
        self.q: queue.Queue[str] = queue.Queue()

        self._build_widgets()
        self._poll_output()

        graphify = graphify_executable()
        if graphify:
            self._set_status(f"graphify: {graphify}")
        else:
            self._set_status(
                "graphify CLI not found. Run launch.bat / launch.sh to bootstrap."
            )

    # ------------------------------------------------------------------ UI

    def _apply_icon(self) -> None:
        try:
            if os.name == "nt" and ICON_ICO.exists():
                self.root.iconbitmap(default=str(ICON_ICO))
                return
        except Exception:
            pass
        try:
            if ICON_PNG.exists():
                from tkinter import PhotoImage

                self._icon_img = PhotoImage(file=str(ICON_PNG))
                self.root.iconphoto(True, self._icon_img)
        except Exception:
            pass

    def _build_widgets(self) -> None:
        pad = {"padx": 8, "pady": 4}

        top = ttk.Frame(self.root)
        top.pack(side="top", fill="x", **pad)

        ttk.Label(top, text="Folder to graph:").pack(side=LEFT)
        entry = ttk.Entry(top, textvariable=self.path_var)
        entry.pack(side=LEFT, fill="x", expand=True, padx=6)
        ttk.Button(top, text="Browse...", command=self._browse).pack(side=LEFT)

        opts = ttk.Frame(self.root)
        opts.pack(side="top", fill="x", **pad)
        ttk.Label(opts, text="Mode:").pack(side=LEFT)
        ttk.Combobox(
            opts,
            textvariable=self.mode_var,
            values=["normal", "deep"],
            width=10,
            state="readonly",
        ).pack(side=LEFT, padx=(4, 16))

        ttk.Button(opts, text="Build Graph", command=self._build_graph).pack(
            side=LEFT, padx=2
        )
        ttk.Button(opts, text="Update", command=self._update_graph).pack(
            side=LEFT, padx=2
        )
        ttk.Button(opts, text="Open Visualization", command=self._open_html).pack(
            side=LEFT, padx=2
        )
        ttk.Button(opts, text="Open Report", command=self._open_report).pack(
            side=LEFT, padx=2
        )
        ttk.Button(opts, text="Stop", command=self._stop).pack(side=RIGHT, padx=2)

        qf = ttk.LabelFrame(self.root, text="Query the graph")
        qf.pack(side="top", fill="x", **pad)
        ttk.Entry(qf, textvariable=self.query_var).pack(
            side=LEFT, fill="x", expand=True, padx=6, pady=6
        )
        ttk.Button(qf, text="Query", command=self._query).pack(side=LEFT, padx=2, pady=6)
        ttk.Button(qf, text="Explain", command=self._explain).pack(
            side=LEFT, padx=2, pady=6
        )
        ttk.Button(qf, text="Path Between (A|B)", command=self._path_between).pack(
            side=LEFT, padx=2, pady=6
        )

        outf = ttk.LabelFrame(self.root, text="Output")
        outf.pack(side="top", fill=BOTH, expand=True, **pad)
        self.output = Text(outf, wrap="word", height=20)
        scroll = ttk.Scrollbar(outf, orient="vertical", command=self.output.yview)
        self.output.configure(yscrollcommand=scroll.set)
        scroll.pack(side=RIGHT, fill="y")
        self.output.pack(side=LEFT, fill=BOTH, expand=True)

        bar = ttk.Frame(self.root)
        bar.pack(side="bottom", fill="x")
        ttk.Label(bar, textvariable=self.status_var, anchor="w").pack(
            side=LEFT, fill="x", expand=True, padx=8, pady=4
        )
        ttk.Button(bar, text="Clear Output", command=self._clear).pack(
            side=RIGHT, padx=8, pady=4
        )

    # ------------------------------------------------------------------ actions

    def _browse(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.path_var.get() or str(Path.home()))
        if chosen:
            self.path_var.set(chosen)

    def _selected_path(self) -> Path | None:
        p = self.path_var.get().strip()
        if not p:
            messagebox.showwarning("No folder", "Pick a folder first.")
            return None
        path = Path(p).expanduser()
        if not path.exists():
            messagebox.showerror("Missing", f"Path does not exist:\n{path}")
            return None
        return path

    def _build_graph(self) -> None:
        path = self._selected_path()
        if not path:
            return
        args = [str(path)]
        if self.mode_var.get() == "deep":
            args += ["--mode", "deep"]
        self._run_graphify(args, cwd=path)

    def _update_graph(self) -> None:
        path = self._selected_path()
        if not path:
            return
        self._run_graphify([str(path), "--update"], cwd=path)

    def _query(self) -> None:
        self._run_query("query")

    def _explain(self) -> None:
        self._run_query("explain")

    def _path_between(self) -> None:
        q = self.query_var.get().strip()
        if "|" not in q:
            messagebox.showinfo(
                "Path between",
                'Use "A|B" in the query box (two node names separated by "|").',
            )
            return
        a, b = (s.strip() for s in q.split("|", 1))
        path = self._selected_path()
        if not path:
            return
        self._run_graphify(["path", a, b], cwd=path)

    def _run_query(self, sub: str) -> None:
        q = self.query_var.get().strip()
        if not q:
            messagebox.showwarning("Empty", "Type a query first.")
            return
        path = self._selected_path()
        if not path:
            return
        self._run_graphify([sub, q], cwd=path)

    def _open_html(self) -> None:
        path = self._selected_path()
        if not path:
            return
        html = path / "graphify-out" / "graph.html"
        if not html.exists():
            messagebox.showinfo(
                "Not found",
                f"No graph.html yet. Build the graph first.\n\nLooked at:\n{html}",
            )
            return
        webbrowser.open(html.as_uri())

    def _open_report(self) -> None:
        path = self._selected_path()
        if not path:
            return
        report = path / "graphify-out" / "GRAPH_REPORT.md"
        if not report.exists():
            messagebox.showinfo(
                "Not found",
                f"No GRAPH_REPORT.md yet. Build the graph first.\n\nLooked at:\n{report}",
            )
            return
        webbrowser.open(report.as_uri())

    def _stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self._set_status("Stopped.")
            except Exception as exc:
                self._set_status(f"Stop failed: {exc}")

    def _clear(self) -> None:
        self.output.delete("1.0", END)

    # ------------------------------------------------------------------ subprocess

    def _run_graphify(self, args: list[str], cwd: Path) -> None:
        if self.proc and self.proc.poll() is None:
            messagebox.showinfo("Busy", "A graphify command is already running.")
            return
        exe = graphify_executable()
        if not exe:
            messagebox.showerror(
                "graphify not found",
                "Could not locate the graphify CLI.\n\n"
                "Run launch.bat (Windows) or launch.sh (macOS/Linux) so it can\n"
                "bootstrap a venv and install graphifyy.",
            )
            return
        cmd = [exe, *args]
        self._append(f"\n$ {' '.join(shlex.quote(c) for c in cmd)}\n  (cwd: {cwd})\n")
        self._set_status("Running...")
        try:
            self.proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            self._append(f"[error] {exc}\n")
            self._set_status("Failed to start graphify.")
            return
        threading.Thread(target=self._reader_thread, args=(self.proc,), daemon=True).start()

    def _reader_thread(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            self.q.put(line)
        proc.wait()
        self.q.put(f"\n[exit {proc.returncode}]\n")

    def _poll_output(self) -> None:
        try:
            while True:
                line = self.q.get_nowait()
                self._append(line)
                if line.startswith("\n[exit "):
                    self._set_status("Done.")
        except queue.Empty:
            pass
        self.root.after(80, self._poll_output)

    # ------------------------------------------------------------------ small helpers

    def _append(self, text: str) -> None:
        self.output.configure(state=NORMAL)
        self.output.insert(END, text)
        self.output.see(END)

    def _set_status(self, msg: str) -> None:
        self.status_var.set(msg)


def main() -> int:
    root = Tk()
    try:
        ttk.Style().theme_use("vista" if os.name == "nt" else "clam")
    except Exception:
        pass
    GraphifyApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

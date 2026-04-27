"""Tkinter GUI for the graphify CLI.

Folder or git URL in, knowledge graph out. Click a node for its details,
ask a question against a local Ollama model in the Chat tab.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import urlparse
from tkinter import (
    BOTH,
    DISABLED,
    END,
    HORIZONTAL,
    LEFT,
    NORMAL,
    RIGHT,
    Tk,
    StringVar,
    Text,
    filedialog,
    font as tkfont,
    messagebox,
    ttk,
)

# Optional deps. We import lazily so the GUI still opens (with a graceful
# message in the graph pane) when matplotlib/networkx aren't installed.
try:
    import matplotlib

    matplotlib.use("TkAgg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_tkagg import (
        FigureCanvasTkAgg,
        NavigationToolbar2Tk,
    )
    from matplotlib.figure import Figure

    _MPL_OK = True
except Exception as _mpl_err:
    _MPL_OK = False

try:
    import networkx as nx

    _NX_OK = True
except Exception:
    _NX_OK = False


APP_DIR = Path(__file__).resolve().parent
ICON_ICO = APP_DIR / "icon.ico"
ICON_PNG = APP_DIR / "icon.png"


# ----------------------------------------------------------------------- theme

PALETTE = {
    "bg":        "#101826",   # window background
    "panel":     "#162033",   # panel background
    "panel_alt": "#1d2a40",   # secondary panel
    "fg":        "#e7ecf3",   # primary text
    "fg_dim":    "#9aa6b8",   # secondary text
    "accent":    "#5ac6ff",   # cyan accent
    "accent2":   "#7c5cff",   # violet accent
    "ok":        "#5fd38f",   # success
    "warn":      "#ffb454",   # warning
    "edge":      "#33415a",   # edges in the graph
}

# Small palette for community coloring on the graph.
COMMUNITY_COLORS = [
    "#5ac6ff", "#ff7a90", "#7c5cff", "#5fd38f", "#ffb454",
    "#ff8b3d", "#3dd1c5", "#d2a4ff", "#ffd166", "#9bd4ff",
]


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
    return shutil.which("graphify")


# Detect whether the user typed a URL vs a local path.
URL_RE = re.compile(r"^(https?://|git@|ssh://|git://)", re.IGNORECASE)
# SCP-style git URL: user@host:path/to/repo(.git)?  (no scheme prefix)
SCP_RE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:[^\\]+", re.IGNORECASE)


def is_url(s: str) -> bool:
    s = s.strip()
    return bool(URL_RE.match(s) or SCP_RE.match(s))


def looks_like_network_path(s: str) -> bool:
    """UNC, mapped network drive, or common SMB/NFS mount points."""
    s = s.strip()
    if s.startswith("\\\\") or s.startswith("//"):
        return True  # Windows UNC or POSIX-style network share
    if sys.platform == "darwin" and s.startswith("/Volumes/"):
        return True
    if sys.platform.startswith("linux") and s.startswith(("/mnt/", "/media/")):
        return True
    return False


# Stdlib HTTP client for a locally-running Ollama daemon (used by the Chat
# tab). urllib is enough, no extra deps.

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")


class OllamaClient:
    def __init__(self, host: str = OLLAMA_HOST) -> None:
        self.host = host.rstrip("/")

    def is_up(self, timeout: float = 1.5) -> bool:
        try:
            with urllib.request.urlopen(f"{self.host}/api/tags", timeout=timeout):
                return True
        except Exception:
            return False

    def list_models(self, timeout: float = 3.0) -> list[str]:
        """Return the names of installed chat-capable models."""
        try:
            with urllib.request.urlopen(
                f"{self.host}/api/tags", timeout=timeout
            ) as r:
                data = json.load(r)
        except Exception:
            return []
        names: list[str] = []
        for m in data.get("models", []):
            name = m.get("name", "")
            if not name:
                continue
            # Filter out embedding-only models - they can't chat.
            family = (m.get("details") or {}).get("family", "") or ""
            if "embed" in name.lower() or family in {"nomic-bert", "bge", "bert"}:
                continue
            names.append(name)
        return names

    def stream_chat(
        self,
        model: str,
        messages: list[dict],
        stop_event: threading.Event,
        options: dict | None = None,
    ):
        """Yield text chunks from /api/chat with stream=true."""
        body = json.dumps(
            {
                "model": model,
                "messages": messages,
                "stream": True,
                "options": options or {},
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.host}/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                for raw in resp:
                    if stop_event.is_set():
                        break
                    if not raw.strip():
                        continue
                    try:
                        chunk = json.loads(raw)
                    except Exception:
                        continue
                    msg = chunk.get("message") or {}
                    text = msg.get("content", "")
                    if text:
                        yield text
                    if chunk.get("done"):
                        break
        except urllib.error.URLError as exc:
            yield f"\n[error: {exc}]"
        except Exception as exc:
            yield f"\n[error: {exc}]"


def derive_clone_dest(url: str) -> Path:
    """Pick a deterministic local clone directory for a given git URL.

    Mirrors graphify's own ~/.graphify/repos/<owner>/<repo> convention so
    repeat clones land in the same place.
    """
    home = Path.home() / ".graphify" / "repos"
    if url.startswith("git@"):
        m = re.match(r"git@([^:]+):([^/]+)/(.+?)(?:\.git)?$", url)
        if m:
            _, owner, repo = m.groups()
            return home / owner / repo
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 2:
        owner = parts[-2]
        repo = parts[-1]
        if repo.endswith(".git"):
            repo = repo[:-4]
        return home / owner / repo
    return home / "unknown" / (parsed.netloc or "repo")


# =============================================================== app


class GraphifyApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title("Graphify Explorer")
        self.root.geometry("1280x800")
        self.root.minsize(960, 600)
        self._apply_icon()
        self._apply_theme()

        self.path_var = StringVar(value=str(Path.home()))
        self.output_dir_var = StringVar()  # empty = default <source>/graphify-out
        self.query_var = StringVar()
        self.status_var = StringVar(value="Ready.")
        self.proc: subprocess.Popen | None = None
        self.q: queue.Queue[str] = queue.Queue()
        self.graph: object | None = None  # networkx Graph
        self.node_positions: dict | None = None
        self.node_artist = None
        self.node_keys: list[str] = []
        self.selected_node: str | None = None

        self._build_widgets()
        self._poll_output()

        graphify = graphify_executable()
        if graphify:
            self._set_status(f"graphify: {graphify}")
        else:
            self._set_status("graphify CLI not found. Run the installer first.")

    # ---------------------------------------------------------- chrome

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

    def _apply_theme(self) -> None:
        self.root.configure(bg=PALETTE["bg"])
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        base_font = ("Segoe UI", 10) if os.name == "nt" else ("Helvetica", 11)
        title_font = (base_font[0], base_font[1] + 2, "bold")
        mono_font = ("Consolas", 10) if os.name == "nt" else ("Menlo", 10)

        self.root.option_add("*Font", base_font)
        self._title_font = title_font
        self._mono_font = mono_font

        style.configure(".", background=PALETTE["bg"], foreground=PALETTE["fg"])
        style.configure("TFrame", background=PALETTE["bg"])
        style.configure("Panel.TFrame", background=PALETTE["panel"])
        style.configure(
            "TLabel", background=PALETTE["bg"], foreground=PALETTE["fg"]
        )
        style.configure(
            "Dim.TLabel",
            background=PALETTE["bg"],
            foreground=PALETTE["fg_dim"],
        )
        style.configure(
            "Title.TLabel",
            background=PALETTE["bg"],
            foreground=PALETTE["fg"],
            font=title_font,
        )
        style.configure(
            "Status.TLabel",
            background=PALETTE["panel"],
            foreground=PALETTE["fg_dim"],
        )
        style.configure(
            "TButton",
            background=PALETTE["panel_alt"],
            foreground=PALETTE["fg"],
            borderwidth=0,
            focusthickness=0,
            padding=(10, 6),
        )
        style.map(
            "TButton",
            background=[
                ("active", PALETTE["accent"]),
                ("disabled", PALETTE["panel"]),
            ],
            foreground=[("active", "#0b1220")],
        )
        style.configure(
            "Accent.TButton",
            background=PALETTE["accent"],
            foreground="#0b1220",
            padding=(12, 7),
        )
        style.map(
            "Accent.TButton",
            background=[("active", PALETTE["accent2"])],
            foreground=[("active", PALETTE["fg"])],
        )
        style.configure(
            "TEntry",
            fieldbackground=PALETTE["panel_alt"],
            foreground=PALETTE["fg"],
            insertcolor=PALETTE["fg"],
            borderwidth=0,
        )
        style.configure(
            "TLabelframe",
            background=PALETTE["bg"],
            foreground=PALETTE["fg"],
            bordercolor=PALETTE["panel_alt"],
        )
        style.configure(
            "TLabelframe.Label",
            background=PALETTE["bg"],
            foreground=PALETTE["accent"],
            font=(base_font[0], base_font[1], "bold"),
        )
        style.configure(
            "Vertical.TScrollbar",
            background=PALETTE["panel"],
            troughcolor=PALETTE["bg"],
            bordercolor=PALETTE["bg"],
            arrowcolor=PALETTE["fg_dim"],
        )
        style.configure(
            "Horizontal.TScrollbar",
            background=PALETTE["panel"],
            troughcolor=PALETTE["bg"],
            bordercolor=PALETTE["bg"],
            arrowcolor=PALETTE["fg_dim"],
        )
        style.configure(
            "TPanedwindow", background=PALETTE["bg"]
        )

    # ---------------------------------------------------------- widgets

    def _build_widgets(self) -> None:
        # Top bar - input
        top = ttk.Frame(self.root)
        top.pack(side="top", fill="x", padx=12, pady=(12, 4))

        ttk.Label(top, text="Folder or URL", style="Dim.TLabel").pack(
            side=LEFT, padx=(0, 6)
        )
        entry = ttk.Entry(top, textvariable=self.path_var)
        entry.pack(side=LEFT, fill="x", expand=True, padx=(0, 6))
        ttk.Button(top, text="Browse…", command=self._browse).pack(side=LEFT)

        # Optional: custom output directory (junction/symlink to graphify-out).
        out_row = ttk.Frame(self.root)
        out_row.pack(side="top", fill="x", padx=12, pady=(0, 4))
        ttk.Label(
            out_row,
            text="Output (optional)",
            style="Dim.TLabel",
        ).pack(side=LEFT, padx=(0, 6))
        ttk.Entry(out_row, textvariable=self.output_dir_var).pack(
            side=LEFT, fill="x", expand=True, padx=(0, 6)
        )
        ttk.Button(out_row, text="Browse…", command=self._browse_output).pack(
            side=LEFT
        )
        ttk.Button(out_row, text="Clear", command=lambda: self.output_dir_var.set("")).pack(
            side=LEFT, padx=4
        )

        actions = ttk.Frame(self.root)
        actions.pack(side="top", fill="x", padx=12, pady=(0, 8))
        ttk.Button(
            actions,
            text="Build / Refresh Graph",
            style="Accent.TButton",
            command=self._update_graph,
        ).pack(side=LEFT, padx=(0, 6))
        ttk.Button(actions, text="Watch", command=self._watch).pack(side=LEFT, padx=4)
        ttk.Button(actions, text="Reload View", command=self._load_graph_into_view).pack(
            side=LEFT, padx=4
        )
        ttk.Button(actions, text="Open HTML", command=self._open_html).pack(
            side=LEFT, padx=4
        )
        ttk.Button(actions, text="Open Report", command=self._open_report).pack(
            side=LEFT, padx=4
        )
        ttk.Button(actions, text="Show in Files", command=self._show_in_files).pack(
            side=LEFT, padx=4
        )
        ttk.Button(actions, text="Stop", command=self._stop).pack(side=RIGHT)

        # Main split (graph | details) -------------------------------------
        paned = ttk.Panedwindow(self.root, orient=HORIZONTAL)
        paned.pack(side="top", fill=BOTH, expand=True, padx=12, pady=6)

        # --- left: graph viz
        graph_frame = ttk.Frame(paned, style="Panel.TFrame")
        paned.add(graph_frame, weight=3)
        self._build_graph_pane(graph_frame)

        # --- right: details + query + output
        right = ttk.Frame(paned, style="Panel.TFrame")
        paned.add(right, weight=2)
        self._build_right_pane(right)

        # Status bar --------------------------------------------------------
        bar = ttk.Frame(self.root, style="Panel.TFrame")
        bar.pack(side="bottom", fill="x")
        ttk.Label(bar, textvariable=self.status_var, style="Status.TLabel").pack(
            side=LEFT, padx=10, pady=6
        )
        ttk.Button(bar, text="Clear Output", command=self._clear).pack(
            side=RIGHT, padx=8, pady=4
        )

    def _build_graph_pane(self, parent: ttk.Frame) -> None:
        header = ttk.Label(parent, text="Knowledge Graph", style="Title.TLabel")
        header.pack(anchor="w", padx=10, pady=(8, 4))
        self.graph_meta_var = StringVar(
            value="No graph loaded. Pick a folder and Build / Refresh."
        )
        ttk.Label(
            parent, textvariable=self.graph_meta_var, style="Dim.TLabel"
        ).pack(anchor="w", padx=10)

        self.graph_container = ttk.Frame(parent, style="Panel.TFrame")
        self.graph_container.pack(fill=BOTH, expand=True, padx=8, pady=8)

        if not (_MPL_OK and _NX_OK):
            ttk.Label(
                self.graph_container,
                text=(
                    "matplotlib + networkx are required to render the graph "
                    "in-app.\nReinstall via the installer or run\n"
                    "    .venv/bin/pip install matplotlib networkx"
                ),
                style="Dim.TLabel",
                justify="left",
            ).pack(padx=20, pady=20)
            self.figure = None
            self.canvas = None
            return

        self.figure = Figure(figsize=(7, 5), dpi=100, facecolor=PALETTE["panel"])
        self.ax = self.figure.add_subplot(111)
        self._style_axes()
        self.canvas = FigureCanvasTkAgg(self.figure, master=self.graph_container)
        self.canvas.get_tk_widget().pack(fill=BOTH, expand=True)
        toolbar = NavigationToolbar2Tk(self.canvas, parent, pack_toolbar=False)
        toolbar.config(background=PALETTE["panel"])
        for child in toolbar.winfo_children():
            try:
                child.config(background=PALETTE["panel"])
            except Exception:
                pass
        toolbar.update()
        toolbar.pack(fill="x", padx=8, pady=(0, 6))
        self.canvas.mpl_connect("pick_event", self._on_pick)

    def _build_right_pane(self, parent: ttk.Frame) -> None:
        # Always-visible query row at the top.
        qf = ttk.Frame(parent, style="Panel.TFrame")
        qf.pack(fill="x", padx=10, pady=(10, 6))
        ttk.Label(qf, text="Ask the graph", style="Title.TLabel").pack(
            anchor="w", padx=4
        )
        ttk.Entry(qf, textvariable=self.query_var).pack(
            fill="x", padx=4, pady=(4, 6)
        )
        btnrow = ttk.Frame(qf, style="Panel.TFrame")
        btnrow.pack(fill="x", padx=4)
        ttk.Button(
            btnrow, text="Query", style="Accent.TButton", command=self._query
        ).pack(side=LEFT, padx=(0, 6))
        ttk.Button(btnrow, text="Explain", command=self._explain).pack(
            side=LEFT, padx=4
        )
        ttk.Button(btnrow, text="Path A|B", command=self._path_between).pack(
            side=LEFT, padx=4
        )
        ttk.Button(
            btnrow,
            text="Use selected",
            command=self._fill_query_with_selected,
        ).pack(side=RIGHT)

        # Tabbed area: Details / Chat / Output
        nb = ttk.Notebook(parent)
        nb.pack(fill=BOTH, expand=True, padx=10, pady=(4, 10))
        self.notebook = nb

        details_tab = ttk.Frame(nb, style="Panel.TFrame")
        chat_tab = ttk.Frame(nb, style="Panel.TFrame")
        output_tab = ttk.Frame(nb, style="Panel.TFrame")
        nb.add(details_tab, text="Details")
        nb.add(chat_tab, text="Chat (local LLM)")
        nb.add(output_tab, text="Output")

        self._build_details_tab(details_tab)
        self._build_chat_tab(chat_tab)
        self._build_output_tab(output_tab)

    def _build_details_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Selected node", style="Title.TLabel").pack(
            anchor="w", padx=10, pady=(10, 4)
        )
        self.detail_text = Text(
            parent,
            wrap="word",
            background=PALETTE["panel_alt"],
            foreground=PALETTE["fg"],
            insertbackground=PALETTE["fg"],
            relief="flat",
            borderwidth=0,
            padx=10,
            pady=8,
        )
        self.detail_text.pack(fill=BOTH, expand=True, padx=10, pady=(0, 10))
        self._set_detail_placeholder()

    def _build_output_tab(self, parent: ttk.Frame) -> None:
        text_frame = ttk.Frame(parent, style="Panel.TFrame")
        text_frame.pack(fill=BOTH, expand=True, padx=10, pady=10)
        self.output = Text(
            text_frame,
            wrap="word",
            background=PALETTE["panel_alt"],
            foreground=PALETTE["fg"],
            insertbackground=PALETTE["fg"],
            relief="flat",
            borderwidth=0,
            padx=10,
            pady=8,
            font=self._mono_font,
        )
        scroll = ttk.Scrollbar(
            text_frame, orient="vertical", command=self.output.yview
        )
        self.output.configure(yscrollcommand=scroll.set)
        scroll.pack(side=RIGHT, fill="y")
        self.output.pack(side=LEFT, fill=BOTH, expand=True)
        self.output.tag_configure("dim", foreground=PALETTE["fg_dim"])
        self.output.tag_configure("ok", foreground=PALETTE["ok"])
        self.output.tag_configure("warn", foreground=PALETTE["warn"])
        self.output.tag_configure(
            "cmd", foreground=PALETTE["accent"], font=self._mono_font
        )

    # ------------------------------------------------------------ chat tab

    def _build_chat_tab(self, parent: ttk.Frame) -> None:
        # Header with model picker
        header = ttk.Frame(parent, style="Panel.TFrame")
        header.pack(fill="x", padx=10, pady=(10, 6))
        ttk.Label(header, text="Local LLM (Ollama)", style="Title.TLabel").pack(
            side=LEFT
        )
        self.chat_status_var = StringVar(value="checking…")
        ttk.Label(
            header, textvariable=self.chat_status_var, style="Dim.TLabel"
        ).pack(side=LEFT, padx=10)

        picker = ttk.Frame(parent, style="Panel.TFrame")
        picker.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Label(picker, text="Model:", style="Dim.TLabel").pack(
            side=LEFT, padx=(0, 6)
        )
        self.chat_model_var = StringVar()
        self.chat_model_combo = ttk.Combobox(
            picker,
            textvariable=self.chat_model_var,
            state="readonly",
            width=30,
        )
        self.chat_model_combo.pack(side=LEFT, fill="x", expand=True)
        ttk.Button(
            picker, text="Refresh", command=self._refresh_models
        ).pack(side=LEFT, padx=4)

        # Conversation transcript
        body = ttk.Frame(parent, style="Panel.TFrame")
        body.pack(fill=BOTH, expand=True, padx=10, pady=(0, 6))
        self.chat_text = Text(
            body,
            wrap="word",
            background=PALETTE["panel_alt"],
            foreground=PALETTE["fg"],
            insertbackground=PALETTE["fg"],
            relief="flat",
            borderwidth=0,
            padx=10,
            pady=8,
        )
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.chat_text.yview)
        self.chat_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side=RIGHT, fill="y")
        self.chat_text.pack(side=LEFT, fill=BOTH, expand=True)
        self.chat_text.tag_configure(
            "user", foreground=PALETTE["accent"], font=(self._mono_font[0], 10, "bold")
        )
        self.chat_text.tag_configure(
            "assistant", foreground=PALETTE["fg"]
        )
        self.chat_text.tag_configure(
            "system", foreground=PALETTE["fg_dim"]
        )
        self.chat_text.tag_configure(
            "citation", foreground=PALETTE["accent2"]
        )
        self.chat_text.configure(state=DISABLED)

        # Input row
        inp = ttk.Frame(parent, style="Panel.TFrame")
        inp.pack(fill="x", padx=10, pady=(0, 10))
        self.chat_input_var = StringVar()
        self.chat_entry = ttk.Entry(inp, textvariable=self.chat_input_var)
        self.chat_entry.pack(side=LEFT, fill="x", expand=True, padx=(0, 6))
        self.chat_entry.bind("<Return>", lambda e: self._chat_send())
        self.chat_send_btn = ttk.Button(
            inp, text="Send", style="Accent.TButton", command=self._chat_send
        )
        self.chat_send_btn.pack(side=LEFT)
        ttk.Button(inp, text="Stop", command=self._chat_stop).pack(
            side=LEFT, padx=4
        )
        ttk.Button(inp, text="Clear", command=self._chat_clear).pack(
            side=LEFT, padx=4
        )

        # State
        self.ollama = OllamaClient()
        self.chat_history: list[dict] = []
        self.chat_stop_event = threading.Event()
        self.chat_thread: threading.Thread | None = None

        # Initial model probe (runs in background - don't block UI startup).
        threading.Thread(target=self._refresh_models_async, daemon=True).start()

    # ---------------------------------------------------------- chat actions

    def _refresh_models(self) -> None:
        threading.Thread(target=self._refresh_models_async, daemon=True).start()

    def _refresh_models_async(self) -> None:
        if not self.ollama.is_up():
            self.root.after(
                0,
                lambda: self.chat_status_var.set(
                    "Ollama not running on localhost:11434. "
                    "Install from https://ollama.com and `ollama pull qwen2.5:7b`."
                ),
            )
            return
        models = self.ollama.list_models()
        def apply():
            if models:
                self.chat_model_combo["values"] = models
                if not self.chat_model_var.get() or self.chat_model_var.get() not in models:
                    # Prefer a code-focused model if present.
                    preferred = next(
                        (m for m in models if "coder" in m.lower() or "code" in m.lower()),
                        models[0],
                    )
                    self.chat_model_var.set(preferred)
                self.chat_status_var.set(f"connected - {len(models)} model(s) available")
            else:
                self.chat_status_var.set(
                    "Ollama is up but no chat models installed. "
                    "Try `ollama pull qwen2.5:7b`."
                )
        self.root.after(0, apply)

    def _chat_clear(self) -> None:
        self.chat_history = []
        self.chat_text.configure(state=NORMAL)
        self.chat_text.delete("1.0", END)
        self.chat_text.configure(state=DISABLED)

    def _chat_stop(self) -> None:
        self.chat_stop_event.set()

    def _chat_append(self, text: str, tag: str | None = None) -> None:
        self.chat_text.configure(state=NORMAL)
        if tag:
            self.chat_text.insert(END, text, tag)
        else:
            self.chat_text.insert(END, text)
        self.chat_text.see(END)
        self.chat_text.configure(state=DISABLED)

    def _chat_send(self) -> None:
        if self.chat_thread and self.chat_thread.is_alive():
            messagebox.showinfo(
                "Busy", "An answer is still streaming. Click Stop first."
            )
            return
        question = self.chat_input_var.get().strip()
        if not question:
            return
        model = self.chat_model_var.get().strip()
        if not model:
            messagebox.showerror(
                "No model",
                "No Ollama model selected. Click Refresh, or run "
                "`ollama pull qwen2.5:7b`.",
            )
            return

        # Render the user turn.
        self._chat_append(f"\nYou: ", "user")
        self._chat_append(f"{question}\n")
        self.chat_input_var.set("")

        self.chat_stop_event = threading.Event()
        self.chat_thread = threading.Thread(
            target=self._chat_worker,
            args=(question, model),
            daemon=True,
        )
        self.chat_thread.start()

    def _chat_worker(self, question: str, model: str) -> None:
        # 1. Build grounding context from the graph.
        ctx_lines: list[str] = []
        path = None
        try:
            p = self.path_var.get().strip()
            if p and not is_url(p):
                path = Path(p).expanduser()
        except Exception:
            pass

        graph_query_out = self._run_graphify_capture(
            ["query", question, "--budget", "1500"], cwd=path
        )
        if graph_query_out:
            ctx_lines.append("=== Graph BFS context (from `graphify query`) ===")
            ctx_lines.append(graph_query_out.strip()[:4000])
            ctx_lines.append("")

        # Plus a slice of the GRAPH_REPORT.md for high-level concepts.
        if path:
            report = path / "graphify-out" / "GRAPH_REPORT.md"
            if report.exists():
                try:
                    text = report.read_text(encoding="utf-8")
                    ctx_lines.append("=== GRAPH_REPORT.md (excerpt) ===")
                    ctx_lines.append(text[:2500])
                except Exception:
                    pass

        context = "\n".join(ctx_lines).strip() or "(no graph loaded - answer from general knowledge)"

        system_prompt = (
            "You are a code-base analyst answering questions about a "
            "specific repository. Use ONLY the provided graph context to "
            "ground your answer. When you cite a node or file, name it "
            "explicitly. If the context does not contain the answer, say "
            "so plainly - do not invent file or function names."
        )
        user_msg = f"Repository graph context:\n{context}\n\nQuestion: {question}"

        # Track conversation: keep prior turns, but always re-inject the
        # context as the latest user message so the model stays grounded.
        msgs: list[dict] = [{"role": "system", "content": system_prompt}]
        for turn in self.chat_history:
            msgs.append(turn)
        msgs.append({"role": "user", "content": user_msg})

        # 2. Open the assistant turn in the transcript.
        self.root.after(0, lambda: self._chat_append("\nAssistant: ", "user"))

        full_reply: list[str] = []
        for chunk in self.ollama.stream_chat(
            model=model,
            messages=msgs,
            stop_event=self.chat_stop_event,
        ):
            full_reply.append(chunk)
            self.root.after(0, lambda c=chunk: self._chat_append(c, "assistant"))
            if self.chat_stop_event.is_set():
                break

        if not full_reply:
            self.root.after(
                0, lambda: self._chat_append("(no response)\n", "system")
            )

        # 3. Trim history (keep last 6 turns to stay under context window).
        self.chat_history.append({"role": "user", "content": question})
        self.chat_history.append({"role": "assistant", "content": "".join(full_reply)})
        if len(self.chat_history) > 12:
            self.chat_history = self.chat_history[-12:]
        self.root.after(0, lambda: self._chat_append("\n"))

    def _run_graphify_capture(
        self, args: list[str], cwd: Path | None, timeout: float = 30.0
    ) -> str:
        """Synchronously run a graphify subcommand and return stdout."""
        exe = graphify_executable()
        if not exe or not cwd or not cwd.exists():
            return ""
        try:
            r = subprocess.run(
                [exe, *args],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return r.stdout or ""
        except Exception:
            return ""

    # ---------------------------------------------------------- graph drawing

    def _style_axes(self) -> None:
        if not _MPL_OK:
            return
        self.ax.set_facecolor(PALETTE["panel"])
        for spine in self.ax.spines.values():
            spine.set_visible(False)
        self.ax.set_xticks([])
        self.ax.set_yticks([])

    def _graph_path(self) -> Path | None:
        path = self._selected_path(silent=True)
        if not path:
            return None
        return path / "graphify-out" / "graph.json"

    def _load_graph_into_view(self) -> None:
        if not (_MPL_OK and _NX_OK):
            return
        gp = self._graph_path()
        if not gp or not gp.exists():
            self.graph_meta_var.set(
                "No graph.json found. Click Build / Refresh first."
            )
            return
        try:
            data = json.loads(gp.read_text(encoding="utf-8"))
            G = nx.node_link_graph(data, edges="links")
        except Exception as exc:
            messagebox.showerror("Load failed", f"Could not parse graph.json:\n{exc}")
            return
        self.graph = G
        # Default meta - _render_graph may overwrite this with a cap notice.
        self.graph_meta_var.set(
            f"{G.number_of_nodes()} nodes · {G.number_of_edges()} edges · "
            f"loaded from {gp}"
        )
        self._render_graph(G)

    # Cap the inline matplotlib viewer at this many nodes. Beyond this the
    # layout solvers get slow and labels turn into mush - point users at the
    # upstream vis.js HTML instead.
    MAX_INLINE_NODES = 300

    def _render_graph(self, G) -> None:
        self.ax.clear()
        self._style_axes()
        if G.number_of_nodes() == 0:
            self.ax.text(
                0.5, 0.5, "Empty graph",
                color=PALETTE["fg_dim"],
                ha="center", va="center", transform=self.ax.transAxes,
            )
            self.canvas.draw_idle()
            return

        # Big graph? Subsample to the top-N most-connected nodes.
        full_nodes = G.number_of_nodes()
        full_edges = G.number_of_edges()
        capped = False
        if full_nodes > self.MAX_INLINE_NODES:
            capped = True
            # Keep the most central nodes plus their immediate neighbours,
            # so the rendering still tells you *where the action is*.
            degrees = sorted(G.degree, key=lambda x: x[1], reverse=True)
            keep = {n for n, _ in degrees[: self.MAX_INLINE_NODES]}
            G = G.subgraph(keep).copy()

        # spring_layout is pure-Python (no scipy needed) and fine up to a
        # few hundred nodes - which is what the cap above guarantees.
        pos = nx.spring_layout(G, seed=7, k=None, iterations=80)

        if capped:
            self.graph_meta_var.set(
                f"{full_nodes} nodes · {full_edges} edges · "
                f"showing top {G.number_of_nodes()} by degree - "
                f"click 'Open HTML' for the full visualization."
            )

        # edges
        for u, v in G.edges():
            x0, y0 = pos[u]
            x1, y1 = pos[v]
            self.ax.plot(
                [x0, x1], [y0, y1],
                color=PALETTE["edge"], linewidth=0.9, alpha=0.7, zorder=1,
            )

        # nodes - colored by community when present
        keys = list(G.nodes())
        xs = [pos[k][0] for k in keys]
        ys = [pos[k][1] for k in keys]
        colors = []
        for k in keys:
            comm = G.nodes[k].get("community", 0)
            colors.append(COMMUNITY_COLORS[int(comm) % len(COMMUNITY_COLORS)])
        sizes = [120 + 40 * G.degree(k) for k in keys]
        self.node_artist = self.ax.scatter(
            xs, ys,
            c=colors, s=sizes,
            edgecolors="#0b1220", linewidths=0.8,
            picker=True, pickradius=8, zorder=3,
        )
        self.node_keys = keys
        self.node_positions = pos

        # labels (cap to 60 to keep readable)
        if len(keys) <= 60:
            for k in keys:
                lbl = G.nodes[k].get("label", k)
                self.ax.text(
                    pos[k][0], pos[k][1] + 0.04,
                    lbl,
                    color=PALETTE["fg"],
                    fontsize=8, ha="center", va="bottom", zorder=4,
                )

        self.ax.margins(0.10)
        self.canvas.draw_idle()

    def _on_pick(self, event) -> None:
        if not self.node_keys or event.artist is not self.node_artist:
            return
        ind = event.ind
        if not len(ind):
            return
        key = self.node_keys[int(ind[0])]
        self.selected_node = key
        self._show_node_details(key)

    # ---------------------------------------------------------- right panel

    def _set_detail_placeholder(self) -> None:
        self.detail_text.configure(state=NORMAL)
        self.detail_text.delete("1.0", END)
        self.detail_text.insert(
            END,
            "Click a node in the graph to see its label, source file, "
            "community, and immediate neighbors here.",
        )
        self.detail_text.configure(state=DISABLED)

    def _show_node_details(self, key: str) -> None:
        if not self.graph:
            return
        attrs = dict(self.graph.nodes[key])
        label = attrs.get("label", key)
        community = attrs.get("community", "?")
        src = attrs.get("source_file", "?")
        loc = attrs.get("source_location", "")
        ftype = attrs.get("file_type", "?")

        neighbors = list(self.graph.neighbors(key))
        rels = []
        for n in neighbors[:20]:
            edata = self.graph.get_edge_data(key, n) or {}
            rel = edata.get("relation", "?")
            conf = edata.get("confidence", "")
            rels.append(f"  • {rel:<10} → {self.graph.nodes[n].get('label', n)} [{conf}]")

        lines = [
            f"Label:     {label}",
            f"Type:      {ftype}",
            f"Source:    {src}{(' ' + loc) if loc else ''}",
            f"Community: {community}",
            f"Degree:    {self.graph.degree(key)}",
            "",
            "Neighbors:",
            *(rels if rels else ["  (none)"]),
        ]
        self.detail_text.configure(state=NORMAL)
        self.detail_text.delete("1.0", END)
        self.detail_text.insert(END, "\n".join(lines))
        self.detail_text.configure(state=DISABLED)

    def _fill_query_with_selected(self) -> None:
        if self.selected_node and self.graph:
            self.query_var.set(self.graph.nodes[self.selected_node].get(
                "label", self.selected_node
            ))

    # ---------------------------------------------------------- actions

    def _browse(self) -> None:
        chosen = filedialog.askdirectory(
            initialdir=self.path_var.get() or str(Path.home())
        )
        if chosen:
            self.path_var.set(chosen)
            # Try auto-loading any existing graph for this folder.
            self._load_graph_into_view()

    def _browse_output(self) -> None:
        chosen = filedialog.askdirectory(
            title="Pick a custom output directory",
            initialdir=self.output_dir_var.get() or str(Path.home()),
        )
        if chosen:
            self.output_dir_var.set(chosen)

    def _selected_path(self, silent: bool = False) -> Path | None:
        p = self.path_var.get().strip()
        if not p:
            if not silent:
                messagebox.showwarning("No input", "Pick a folder or paste a git URL.")
            return None
        if is_url(p):
            if not silent:
                messagebox.showinfo(
                    "URL detected",
                    "That's a git URL. Click Build / Refresh to clone and graph it.",
                )
            return None
        path = Path(p).expanduser()
        # If the user pointed at the .git directory itself, walk up to the
        # repo root - graphify needs the working tree, not the metadata.
        if path.name == ".git" and path.is_dir():
            parent = path.parent
            if not silent:
                self._set_status(f"Using repo root {parent} instead of its .git/")
            self.path_var.set(str(parent))
            path = parent
        if not path.exists():
            if not silent:
                messagebox.showerror("Missing", f"Path does not exist:\n{path}")
            return None
        # Network-mounted paths work but warn about implications.
        if looks_like_network_path(str(path)):
            self._set_status(
                f"Network path detected ({path}); scans will be slower and "
                "graphify-out/ writes back to the share."
            )
        return path

    def _update_graph(self) -> None:
        target = self.path_var.get().strip()
        if not target:
            messagebox.showwarning("No input", "Pick a folder or paste a git URL.")
            return
        if is_url(target):
            self._clone_then_graph(target)
            return
        path = self._selected_path()
        if not path:
            return
        self._maybe_link_output(path)
        self._run_graphify(["update", str(path)], cwd=path, then_load=True)

    def _maybe_link_output(self, source: Path) -> None:
        """If the user picked a custom output directory, make graphify's
        hardcoded `<source>/graphify-out` point at it via a junction (Windows)
        or symlink (Unix). This is transparent to graphify."""
        custom = self.output_dir_var.get().strip()
        if not custom:
            return
        custom_path = Path(custom).expanduser().resolve()
        custom_path.mkdir(parents=True, exist_ok=True)
        link = (source / "graphify-out").resolve(strict=False)

        # If a real graphify-out already exists (not a link), bail rather
        # than risk losing files. The user should remove or move it first.
        if link.exists() and not link.is_symlink():
            try:
                # Junctions on Windows pass is_dir() but also is_junction() is
                # only available in 3.12+. Use a heuristic via os.readlink
                # falling back to an existence check.
                target_of = os.readlink(str(link)) if hasattr(os, "readlink") else None
            except OSError:
                target_of = None
            if not target_of:
                self._append(
                    f"warn: {link} is a real directory, leaving it alone "
                    "(remove it first if you want to use a custom output dir).\n",
                    "warn",
                )
                return

        # Replace any existing link.
        if link.exists() or link.is_symlink():
            try:
                if os.name == "nt":
                    # Junctions are removed via rmdir, symlinks via unlink.
                    if link.is_symlink():
                        link.unlink()
                    else:
                        os.rmdir(str(link))
                else:
                    link.unlink()
            except OSError as exc:
                self._append(f"warn: could not remove old link {link}: {exc}\n", "warn")
                return

        try:
            if os.name == "nt":
                # mklink /J makes a directory junction without admin.
                r = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link), str(custom_path)],
                    capture_output=True, text=True,
                )
                if r.returncode != 0:
                    raise OSError(r.stderr.strip() or "mklink failed")
            else:
                os.symlink(str(custom_path), str(link), target_is_directory=True)
            self._append(
                f"output: {link} -> {custom_path}\n", "ok"
            )
        except OSError as exc:
            self._append(
                f"warn: could not link {link} -> {custom_path}: {exc}\n"
                "Falling back to default location.\n",
                "warn",
            )

    def _clone_then_graph(self, url: str) -> None:
        """Clone (or pull) <url> into ~/.graphify/repos/..., then build the graph."""
        if self.proc and self.proc.poll() is None:
            messagebox.showinfo("Busy", "A graphify command is already running.")
            return
        if not shutil.which("git"):
            messagebox.showerror(
                "git not found",
                "`git` is required to clone remote repos.\n"
                "Install Git from https://git-scm.com/downloads and try again.",
            )
            return
        dest = derive_clone_dest(url)
        dest.parent.mkdir(parents=True, exist_ok=True)

        # Make the destination visible up-front - users were missing where
        # cloned repos and their graph output were landing.
        self._switch_to_output_tab()
        self._append(f"Cloning to: {dest}\n", "ok")
        self._append(
            f"Graph artifacts will land in: {dest / 'graphify-out'}\n", "dim"
        )

        already = dest.exists() and (dest / ".git").exists()
        if already:
            cmd = ["git", "-C", str(dest), "pull", "--ff-only"]
            cwd = dest
        else:
            # Clean up partial clones from previous failed attempts.
            if dest.exists() and not (dest / ".git").exists():
                shutil.rmtree(dest, ignore_errors=True)
            cmd = ["git", "clone", "--depth", "1", url, str(dest)]
            cwd = dest.parent

        self._append(f"$ {' '.join(shlex.quote(c) for c in cmd)}\n", "cmd")
        self._append(f"  (cwd: {cwd})\n", "dim")
        self._set_status("Cloning…" if not already else "Pulling latest…")

        # Stash the destination so the post-clone callback can chain `update`.
        self._post_clone_dest = dest
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
            self._append(f"[error] {exc}\n", "warn")
            self._set_status("Failed to start git.")
            return
        self._then_load = False
        self._then_clone_chain = True
        threading.Thread(
            target=self._reader_thread, args=(self.proc,), daemon=True
        ).start()

    def _watch(self) -> None:
        path = self._selected_path()
        if not path:
            return
        self._run_graphify(["watch", str(path)], cwd=path)

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

    def _show_in_files(self) -> None:
        """Open the current folder (and its graphify-out, if present) in the
        OS file manager. Useful so users can see exactly where artifacts land."""
        p = self.path_var.get().strip()
        if not p:
            messagebox.showwarning("No folder", "Pick a folder or paste a URL first.")
            return
        if is_url(p):
            target = derive_clone_dest(p)
        else:
            target = Path(p).expanduser()
        if not target.exists():
            messagebox.showinfo(
                "Not yet",
                f"Folder doesn't exist yet:\n{target}\n\n"
                f"It will be created on the next clone or build.",
            )
            return
        try:
            if os.name == "nt":
                os.startfile(str(target))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target)])
        except Exception as exc:
            messagebox.showerror("Failed", f"Could not open folder:\n{exc}")

    def _switch_to_output_tab(self) -> None:
        try:
            self.notebook.select(2)  # Output tab
        except Exception:
            pass

    def _stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self._set_status("Stopped.")
            except Exception as exc:
                self._set_status(f"Stop failed: {exc}")

    def _clear(self) -> None:
        self.output.delete("1.0", END)

    # ---------------------------------------------------------- subprocess

    def _run_graphify(
        self,
        args: list[str],
        cwd: Path,
        then_load: bool = False,
    ) -> None:
        if self.proc and self.proc.poll() is None:
            messagebox.showinfo("Busy", "A graphify command is already running.")
            return
        exe = graphify_executable()
        if not exe:
            messagebox.showerror(
                "graphify not found",
                "Could not locate the graphify CLI.\n\n"
                "Run Install-Windows.bat / Install-macOS.command / "
                "install-linux.sh first.",
            )
            return
        cmd = [exe, *args]
        self._append(f"$ {' '.join(shlex.quote(c) for c in cmd)}\n", "cmd")
        self._append(f"  (cwd: {cwd})\n", "dim")
        self._set_status("Running…")
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
            self._append(f"[error] {exc}\n", "warn")
            self._set_status("Failed to start graphify.")
            return
        self._then_load = then_load
        threading.Thread(
            target=self._reader_thread, args=(self.proc,), daemon=True
        ).start()

    def _reader_thread(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            self.q.put(line)
        proc.wait()
        self.q.put(f"[exit {proc.returncode}]\n")

    def _poll_output(self) -> None:
        try:
            while True:
                line = self.q.get_nowait()
                if line.startswith("[exit "):
                    ok = "0]" in line
                    self._append(line, "ok" if ok else "warn")
                    self._set_status("Done." if ok else "Finished with errors.")
                    if ok and getattr(self, "_then_clone_chain", False):
                        # git clone/pull succeeded → switch path_var to the
                        # cloned dir and run `graphify update` on it.
                        self._then_clone_chain = False
                        dest = getattr(self, "_post_clone_dest", None)
                        if dest:
                            self.path_var.set(str(dest))
                            self._run_graphify(
                                ["update", str(dest)], cwd=dest, then_load=True
                            )
                    elif ok and getattr(self, "_then_load", False):
                        self._then_load = False
                        self._load_graph_into_view()
                else:
                    self._append(line)
        except queue.Empty:
            pass
        self.root.after(80, self._poll_output)

    # ---------------------------------------------------------- helpers

    def _append(self, text: str, tag: str | None = None) -> None:
        self.output.configure(state=NORMAL)
        if tag:
            self.output.insert(END, text, tag)
        else:
            self.output.insert(END, text)
        self.output.see(END)

    def _set_status(self, msg: str) -> None:
        self.status_var.set(msg)


def main() -> int:
    root = Tk()
    GraphifyApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

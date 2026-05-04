"""Tkinter GUI for the graphify CLI.

Folder or git URL in, knowledge graph out. Click a node for details,
ask questions against a local Ollama model in the Chat tab.

Depends on graphifyy from PyPI (https://github.com/safishamsi/graphify).
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
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import urlparse
import tkinter as tk
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

# Required: networkx (graphify itself depends on it, so it's always
# available in the venv).
import networkx as nx


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

    def embed_one(
        self, model: str, text: str, timeout: float = 60.0
    ) -> list[float] | None:
        """Single embedding via /api/embeddings. Returns None on failure."""
        body = json.dumps({"model": model, "prompt": text}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.host}/api/embeddings",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.load(r)
            v = d.get("embedding")
            if isinstance(v, list) and v:
                return v
            return None
        except Exception:
            return None

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


# ============================================================ embedding RAG
#
# Plain-numpy embedding index over graph nodes. Built once after
# `graphify update`, cached to graphify-out/embeddings.npz, keyed by the
# SHA256 of graph.json. nomic-embed-text via Ollama, 768-dim float32.
#
# Used by the chat tab's Quality mode to recover semantic recall that the
# keyword-BFS in `graphify query` misses.

EMBED_MODEL_DEFAULT = "nomic-embed-text"
EMBED_INDEX_FILE = "embeddings.npz"


def _read_snippet(path: Path | None, src: str, line_no: int, span: int = 12) -> str:
    """Return ~`span` lines of source around a node's recorded location."""
    if not path or not src:
        return ""
    fp = (path / src).resolve()
    if not fp.exists() or not fp.is_file():
        return ""
    try:
        lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    start = max(0, line_no - 3)
    end = min(len(lines), line_no + span)
    return "\n".join(lines[start:end])


def _node_embedding_text(node_id: str, attrs: dict, repo: Path | None) -> str:
    """The string we feed to nomic-embed-text for a graph node.
    Label + source location + actual code snippet (when available)."""
    label = attrs.get("label", node_id)
    src = attrs.get("source_file", "")
    loc = str(attrs.get("source_location", "")).lstrip("L")
    line_no = int(loc) if loc.isdigit() else 0
    snippet = _read_snippet(repo, src, line_no) if line_no else ""
    parts = [str(label)]
    if src:
        parts.append(f"src: {src}{f' L{line_no}' if line_no else ''}")
    if snippet:
        parts.append(snippet)
    return "\n".join(parts)


def _graph_sha(graph_json_path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with graph_json_path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build_embedding_index(
    graph,
    repo: Path,
    model: str = EMBED_MODEL_DEFAULT,
    host: str = OLLAMA_HOST,
    workers: int = 4,
    progress=None,
    stop_event: threading.Event | None = None,
):
    """Embed every node in `graph`. Returns (vectors, node_ids, model).

    `progress(done, total)` is called periodically if provided.
    `stop_event` lets callers cancel mid-build; partial result is returned.
    """
    import numpy as np
    from concurrent.futures import ThreadPoolExecutor, as_completed

    client = OllamaClient(host)
    node_items = list(graph.nodes(data=True))
    total = len(node_items)
    if total == 0:
        return np.zeros((0, 0), dtype=np.float32), [], model

    # Probe dimensionality on the first call.
    first_text = _node_embedding_text(node_items[0][0], node_items[0][1], repo)
    first_vec = client.embed_one(model, first_text)
    if first_vec is None:
        raise RuntimeError(
            "Ollama embedding call failed. Is the model installed? "
            f"Try: ollama pull {model}"
        )
    dim = len(first_vec)
    vectors = np.zeros((total, dim), dtype=np.float32)
    vectors[0] = np.asarray(first_vec, dtype=np.float32)
    node_ids: list[str] = [node_items[0][0]]

    if progress:
        progress(1, total)

    def task(idx_item):
        idx, (nid, attrs) = idx_item
        if stop_event is not None and stop_event.is_set():
            return idx, nid, None
        text = _node_embedding_text(nid, attrs, repo)
        return idx, nid, client.embed_one(model, text)

    done = 1
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = [
            ex.submit(task, (i, item))
            for i, item in enumerate(node_items)
            if i > 0
        ]
        for fut in as_completed(futures):
            idx, nid, vec = fut.result()
            if vec is None:
                continue
            if idx >= len(node_ids):
                # Pad placeholder list - we'll index by position.
                node_ids.extend([""] * (idx + 1 - len(node_ids)))
            node_ids[idx] = nid
            vectors[idx] = np.asarray(vec, dtype=np.float32)
            done += 1
            if progress and done % 16 == 0:
                progress(done, total)
            if stop_event is not None and stop_event.is_set():
                break

    # Drop any empty rows from cancellation / failed embeddings.
    keep = [i for i, n in enumerate(node_ids) if n]
    vectors = vectors[keep]
    node_ids = [node_ids[i] for i in keep]
    if progress:
        progress(len(node_ids), total)
    return vectors, node_ids, model


def save_embedding_index(
    out_dir: Path, vectors, node_ids: list[str], model: str, graph_sha: str
) -> Path:
    import numpy as np
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / EMBED_INDEX_FILE
    np.savez(
        path,
        vectors=vectors,
        node_ids=np.array(node_ids, dtype=object),
        model=np.array(model),
        graph_sha=np.array(graph_sha),
    )
    return path


def load_embedding_index(out_dir: Path, expect_graph_sha: str | None = None):
    """Returns (vectors, node_ids, model) or None if missing/stale."""
    import numpy as np
    path = out_dir / EMBED_INDEX_FILE
    if not path.exists():
        return None
    try:
        d = np.load(path, allow_pickle=True)
    except Exception:
        return None
    if expect_graph_sha is not None:
        sha = str(d["graph_sha"])
        if sha != expect_graph_sha:
            return None
    vectors = d["vectors"]
    node_ids = list(d["node_ids"])
    model = str(d["model"])
    return vectors, node_ids, model


def cosine_topk(query_vec, vectors, k: int = 8):
    """Return list of (index, score) for top-k cosine sims, descending."""
    import numpy as np
    if vectors is None or len(vectors) == 0:
        return []
    q = np.asarray(query_vec, dtype=np.float32)
    qn = q / (np.linalg.norm(q) + 1e-12)
    norms = np.linalg.norm(vectors, axis=1) + 1e-12
    sims = (vectors @ qn) / norms
    k = min(k, len(sims))
    top = np.argpartition(-sims, k - 1)[:k]
    top = top[np.argsort(-sims[top])]
    return [(int(i), float(sims[i])) for i in top]


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
        # Focus mode: when set, the matplotlib pane shows only the
        # N-hop neighborhood of this node.
        self.focus_node_id: str | None = None
        self.focus_hops: int = 2
        # Job tracking for elapsed-time display + completion alerts.
        self._job_start: float | None = None
        self._job_label: str = ""
        self._alert_on_done: bool = False

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
        ttk.Button(
            actions, text="Focus on Selected",
            command=self._focus_in_vis,
        ).pack(side=LEFT, padx=4)
        ttk.Button(
            actions, text="Reset View",
            command=self._reset_focus_in_vis,
        ).pack(side=LEFT, padx=4)
        ttk.Button(actions, text="Open HTML", command=self._open_html).pack(
            side=LEFT, padx=4
        )
        ttk.Button(
            actions, text="Interactive Graph",
            command=self._open_interactive_view,
        ).pack(side=LEFT, padx=4)
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
        """Left pane: metadata + Interactive Graph control. The graph
        itself renders in a separate vis.js window (auto-launched when
        a graph loads)."""
        header = ttk.Label(parent, text="Knowledge Graph", style="Title.TLabel")
        header.pack(anchor="w", padx=10, pady=(8, 4))

        self.graph_meta_var = StringVar(
            value="No graph loaded. Pick a folder/URL above and Build."
        )
        ttk.Label(
            parent, textvariable=self.graph_meta_var, style="Dim.TLabel",
            wraplength=420,
        ).pack(anchor="w", padx=10, pady=(0, 8))

        sep = ttk.Frame(parent, style="Panel.TFrame", height=1)
        sep.pack(fill="x", padx=8, pady=(2, 8))

        self.viz_status_var = StringVar(
            value="Interactive Graph: not started"
        )
        ttk.Label(
            parent, text="Interactive Graph window",
            style="Title.TLabel",
        ).pack(anchor="w", padx=10, pady=(2, 2))
        ttk.Label(
            parent, textvariable=self.viz_status_var,
            style="Dim.TLabel", wraplength=420,
        ).pack(anchor="w", padx=10, pady=(0, 6))

        btn_row = ttk.Frame(parent, style="Panel.TFrame")
        btn_row.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(
            btn_row, text="Reopen Graph", style="Accent.TButton",
            command=self._open_interactive_view,
        ).pack(side=LEFT, padx=(0, 6))
        ttk.Button(
            btn_row, text="Focus on Selected (1-hop)",
            command=self._focus_in_vis,
        ).pack(side=LEFT, padx=4)
        ttk.Button(
            btn_row, text="Reset Focus",
            command=self._reset_focus_in_vis,
        ).pack(side=LEFT, padx=4)

        # Embed target: on Windows the vis.js subprocess gets reparented
        # into this frame via SetParent. On macOS/Linux the subprocess
        # opens as a separate window (this frame stays empty + shows a
        # short note).
        self.embed_frame = tk.Frame(
            parent, bg=PALETTE["panel_alt"], width=600, height=400,
        )
        self.embed_frame.pack(fill=BOTH, expand=True, padx=10, pady=(4, 10))
        self.embed_frame.pack_propagate(False)

        if os.name != "nt":
            ttk.Label(
                self.embed_frame,
                text=(
                    "The Interactive Graph opens in a separate native\n"
                    "window. Window-embedding is currently Windows-only;\n"
                    "macOS/Linux will keep the popup behaviour."
                ),
                style="Dim.TLabel", justify="center",
                background=PALETTE["panel_alt"],
            ).pack(expand=True, padx=20, pady=20)
        else:
            self._embed_placeholder = tk.Label(
                self.embed_frame,
                text="Interactive Graph will appear here once a graph is loaded.",
                bg=PALETTE["panel_alt"], fg=PALETTE["fg_dim"],
            )
            self._embed_placeholder.pack(expand=True)
            # State for the embedded window.
            self._vis_hwnd: int | None = None
            self._embed_poll_count = 0
            self.embed_frame.bind(
                "<Configure>", self._on_embed_configure,
            )

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
        browse_tab = ttk.Frame(nb, style="Panel.TFrame")
        insights_tab = ttk.Frame(nb, style="Panel.TFrame")
        chat_tab = ttk.Frame(nb, style="Panel.TFrame")
        output_tab = ttk.Frame(nb, style="Panel.TFrame")
        nb.add(details_tab, text="Details")
        nb.add(browse_tab, text="Browse")
        nb.add(insights_tab, text="Insights")
        nb.add(chat_tab, text="Chat (local LLM)")
        nb.add(output_tab, text="Output")

        self._build_details_tab(details_tab)
        self._build_browse_tab(browse_tab)
        self._build_insights_tab(insights_tab)
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

    # ----- Browse tab -----------------------------------------------------

    # Names that almost always indicate an entry point.
    _ENTRY_NAMES = {
        "main", "main()", "app", "app()", "application",
        "cli", "cli()", "run", "run()", "__main__",
    }

    def _build_browse_tab(self, parent: ttk.Frame) -> None:
        # Top: "Where does the code start?" card
        card = ttk.LabelFrame(parent, text="Where does the code start?")
        card.pack(fill="x", padx=10, pady=(10, 6))

        self.entry_var = StringVar(
            value="(load a graph and click Refresh to detect entry points)"
        )
        ttk.Label(
            card, textvariable=self.entry_var, style="Dim.TLabel",
            wraplength=400,
        ).pack(anchor="w", padx=8, pady=(4, 4))

        list_frame = ttk.Frame(card, style="Panel.TFrame")
        list_frame.pack(fill="x", padx=8, pady=(0, 8))
        self.entry_listbox = tk.Listbox(
            list_frame,
            height=5,
            background=PALETTE["panel_alt"],
            foreground=PALETTE["fg"],
            selectbackground=PALETTE["accent"],
            selectforeground="#0b1220",
            relief="flat",
            borderwidth=0,
            activestyle="none",
        )
        self.entry_listbox.pack(side=LEFT, fill="x", expand=True)
        self.entry_listbox.bind("<<ListboxSelect>>", self._on_entry_picked)
        # Map selection index -> node id
        self._entry_node_ids: list[str] = []

        # Middle: filesystem tree of files in the graph
        tree_frame = ttk.LabelFrame(parent, text="Files in this graph")
        tree_frame.pack(fill=BOTH, expand=True, padx=10, pady=6)

        inner = ttk.Frame(tree_frame, style="Panel.TFrame")
        inner.pack(fill=BOTH, expand=True, padx=4, pady=4)
        self.fs_tree = ttk.Treeview(inner, show="tree", height=14)
        sb = ttk.Scrollbar(inner, orient="vertical", command=self.fs_tree.yview)
        self.fs_tree.configure(yscrollcommand=sb.set)
        sb.pack(side=RIGHT, fill="y")
        self.fs_tree.pack(side=LEFT, fill=BOTH, expand=True)
        self.fs_tree.bind("<<TreeviewSelect>>", self._on_fs_picked)
        # Map tree-iid -> node id (only set on leaves that map to a node)
        self._fs_iid_to_node: dict[str, str] = {}

        # Bottom: Refresh + Mermaid export
        buttons = ttk.Frame(parent, style="Panel.TFrame")
        buttons.pack(fill="x", padx=10, pady=(6, 10))
        ttk.Button(
            buttons, text="Refresh from graph",
            command=self._refresh_browse_from_graph,
        ).pack(side=LEFT, padx=(0, 6))
        ttk.Button(
            buttons, text="Export Mermaid (overview)",
            style="Accent.TButton",
            command=self._export_mermaid_overview,
        ).pack(side=LEFT, padx=4)

    # ---- entry-point detection (pure function of the graph) -------------

    # Path segments that indicate the file is NOT a real entry point of
    # the library/app under analysis. We deprioritize these heavily.
    _DEPRIORITIZE_SEGMENTS = (
        "tests/", "test/", "docs/", "doc/", "examples/", "example/",
        "demo/", "demos/", "_test/", "_example/", "fixtures/",
    )

    def _detect_entry_points(self) -> list[dict]:
        """Find likely entry points. Returns scored, sorted list."""
        if not self.graph:
            return []
        out: list[dict] = []
        seen: set[str] = set()

        for nid, attrs in self.graph.nodes(data=True):
            label = (attrs.get("label") or "").strip()
            src = (attrs.get("source_file") or "").replace("\\", "/")
            loc = attrs.get("source_location", "")
            bare = label.replace("()", "").strip()

            score = 0
            reasons: list[str] = []

            base = src.rsplit("/", 1)[-1] if src else ""
            # Strong: path ends in __main__.py
            if base == "__main__.py":
                score += 60
                reasons.append("file is __main__.py")
            # Strong: function/class named like an entry
            if bare.lower() in self._ENTRY_NAMES or label.lower() in self._ENTRY_NAMES:
                score += 50
                reasons.append(f"named {label!r}")
            # Medium: path has cli/ or bin/ or main/ or scripts/ segment
            path_for_seg = f"/{src}/" if src else ""
            for seg in ("cli/", "bin/", "main/", "entrypoints/", "scripts/"):
                if f"/{seg}" in path_for_seg:
                    score += 15
                    reasons.append(f"path has /{seg.rstrip('/')}/")
                    break
            # Medium: in-degree 0 in directed graph (true source)
            try:
                if self.graph.is_directed() and self.graph.in_degree(nid) == 0 \
                        and self.graph.out_degree(nid) > 0:
                    score += 10
                    reasons.append("no incoming edges")
            except Exception:
                pass

            # Heavy deprioritize: tests, examples, docs aren't the real
            # entry points of the library being analyzed.
            for seg in self._DEPRIORITIZE_SEGMENTS:
                if seg in src:
                    score -= 40
                    reasons.append(f"in {seg.rstrip('/')}/")
                    break

            if score > 0 and nid not in seen:
                seen.add(nid)
                out.append({
                    "node_id": nid,
                    "label": label or nid,
                    "src": src,
                    "loc": loc,
                    "score": score,
                    "reason": "; ".join(reasons),
                })

        out.sort(key=lambda d: (-d["score"], d["src"]))
        return out[:10]

    def _refresh_browse_from_graph(self) -> None:
        """Populate the entry-point listbox and the filesystem tree."""
        # Entry points
        self.entry_listbox.delete(0, tk.END)
        self._entry_node_ids = []
        if not self.graph:
            self.entry_var.set(
                "Load or build a graph first - then click Refresh."
            )
        else:
            entries = self._detect_entry_points()
            if not entries:
                self.entry_var.set(
                    "No obvious entry points detected. The graph may be a "
                    "library with no clear `main`."
                )
            else:
                self.entry_var.set(
                    f"Found {len(entries)} candidate entry point(s). "
                    "Click one to highlight it."
                )
                for e in entries:
                    line = f"{e['label']}  -  {e['src']}  ({e['reason']})"
                    self.entry_listbox.insert(tk.END, line)
                    self._entry_node_ids.append(e["node_id"])

        # Filesystem tree
        for iid in self.fs_tree.get_children(""):
            self.fs_tree.delete(iid)
        self._fs_iid_to_node = {}
        if self.graph:
            self._populate_fs_tree()

    def _populate_fs_tree(self) -> None:
        """Build a directory -> file -> node hierarchy from graph nodes."""
        # Group nodes by source_file
        by_file: dict[str, list[tuple[str, str]]] = {}
        for nid, attrs in self.graph.nodes(data=True):
            src = (attrs.get("source_file") or "").replace("\\", "/")
            if not src:
                continue
            label = attrs.get("label") or nid
            by_file.setdefault(src, []).append((nid, label))

        # Insert directories on demand using a path -> iid cache.
        dir_iid: dict[str, str] = {"": ""}  # empty path is the root
        for src in sorted(by_file):
            parts = src.split("/")
            # Walk dirs
            cur_path = ""
            parent = ""
            for d in parts[:-1]:
                cur_path = f"{cur_path}/{d}" if cur_path else d
                if cur_path not in dir_iid:
                    new_iid = self.fs_tree.insert(
                        parent, "end", text=f"{d}/", open=False,
                    )
                    dir_iid[cur_path] = new_iid
                parent = dir_iid[cur_path]
            # Insert the file
            file_name = parts[-1]
            file_iid = self.fs_tree.insert(
                parent, "end",
                text=f"{file_name}  ({len(by_file[src])} nodes)",
                open=False,
            )
            # Insert nodes under it
            for nid, label in sorted(by_file[src], key=lambda t: t[1]):
                node_iid = self.fs_tree.insert(
                    file_iid, "end", text=f"  - {label}",
                )
                self._fs_iid_to_node[node_iid] = nid

    # ---- click handlers --------------------------------------------------

    def _on_entry_picked(self, _event=None) -> None:
        sel = self.entry_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx >= len(self._entry_node_ids):
            return
        nid = self._entry_node_ids[idx]
        self._select_and_highlight_node(nid)

    def _on_fs_picked(self, _event=None) -> None:
        sel = self.fs_tree.selection()
        if not sel:
            return
        nid = self._fs_iid_to_node.get(sel[0])
        if nid:
            self._select_and_highlight_node(nid)

    def _select_and_highlight_node(self, node_id: str) -> None:
        """Select a node: populate Details, highlight in matplotlib, and
        in vis.js if open."""
        if not self.graph or node_id not in self.graph:
            return
        self.selected_node = node_id
        self._show_node_details(node_id)
        # Switch to Details so user sees the result.
        try:
            self.notebook.select(0)
        except Exception:
            pass
        # Tell vis.js to highlight + zoom (no-op if not open)
        try:
            self._highlight_in_vis([node_id])
        except Exception:
            pass

    # ---- Mermaid export --------------------------------------------------

    def _export_mermaid_overview(self) -> None:
        """Open a scope chooser, then render the chosen subgraph as
        Mermaid and show it in a copy-friendly dialog."""
        if not self.graph:
            messagebox.showwarning(
                "No graph", "Load or build a graph first."
            )
            return
        self._show_mermaid_scope_dialog()

    def _show_mermaid_scope_dialog(self) -> None:
        """Pick: full graph / single community / single file /
        selected node's neighborhood."""
        from tkinter import Toplevel
        win = Toplevel(self.root)
        win.title("Mermaid: choose scope")
        win.geometry("520x300")
        try:
            win.configure(bg=PALETTE["bg"])
        except Exception:
            pass
        ttk.Label(
            win, text="What should the diagram show?",
            style="Title.TLabel",
        ).pack(anchor="w", padx=14, pady=(14, 8))

        scope_var = StringVar(value="full")
        for value, text in [
            ("full",  "Full graph (capped to top 30 by degree)"),
            ("comm",  "One community (high-level subsystem)"),
            ("file",  "One file (functions inside it)"),
            ("nbhd",  "Selected node's 1-hop neighborhood"),
        ]:
            ttk.Radiobutton(
                win, text=text, variable=scope_var, value=value,
            ).pack(anchor="w", padx=24, pady=2)

        # Optional argument input
        arg_var = StringVar()
        arg_label = ttk.Label(
            win,
            text="Argument (community id for 'community', file path for 'file' - leave blank for 'full' / 'nbhd')",
            style="Dim.TLabel", wraplength=480,
        )
        arg_label.pack(anchor="w", padx=14, pady=(10, 2))
        ttk.Entry(win, textvariable=arg_var).pack(
            fill="x", padx=14, pady=(0, 8)
        )

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=14, pady=(8, 14))

        def on_ok():
            scope = scope_var.get()
            arg = arg_var.get().strip()
            sub = self._mermaid_subgraph_for_scope(scope, arg)
            if sub is None:
                return  # error already shown
            text = self._graph_to_mermaid(sub, max_nodes=40)
            win.destroy()
            self._show_mermaid_dialog(text)

        ttk.Button(
            bar, text="Generate", style="Accent.TButton", command=on_ok,
        ).pack(side=LEFT)
        ttk.Button(bar, text="Cancel", command=win.destroy).pack(side=RIGHT)

    def _mermaid_subgraph_for_scope(self, scope: str, arg: str):
        """Resolve a scope string + arg to a networkx subgraph.
        Shows a messagebox and returns None on error."""
        G = self.graph
        if scope == "full":
            return G
        if scope == "nbhd":
            if not self.selected_node:
                messagebox.showwarning(
                    "No selection", "Pick a node first (Browse / Details)."
                )
                return None
            sub = self._n_hop_subgraph(self.selected_node, 1)
            if sub is None or sub.number_of_nodes() == 0:
                messagebox.showinfo(
                    "Empty", "Selected node has no neighbors."
                )
                return None
            return sub
        if scope == "comm":
            if not arg.isdigit():
                messagebox.showwarning(
                    "Need community id",
                    "Enter the community number (an integer). "
                    "Hover the matplotlib legend or check the Details "
                    "tab to find one.",
                )
                return None
            cid = int(arg)
            keep = [
                n for n, a in G.nodes(data=True)
                if int(a.get("community", -1) or -1) == cid
            ]
            if not keep:
                messagebox.showinfo(
                    "Empty", f"No nodes in community {cid}."
                )
                return None
            return G.subgraph(keep).copy()
        if scope == "file":
            if not arg:
                messagebox.showwarning(
                    "Need file path",
                    "Enter the file path (e.g. src/click/core.py)."
                )
                return None
            arg_norm = arg.replace("\\", "/")
            keep = [
                n for n, a in G.nodes(data=True)
                if (a.get("source_file") or "").replace("\\", "/") == arg_norm
            ]
            if not keep:
                messagebox.showinfo(
                    "Empty", f"No graph nodes found for file {arg}."
                )
                return None
            return G.subgraph(keep).copy()
        return None

    @staticmethod
    def _safe_mermaid_id(s: str) -> str:
        # Mermaid ids must be alphanumeric/_; squash everything else.
        s = re.sub(r"\W+", "_", str(s))
        return s[:50] or "n"

    def _graph_to_mermaid(self, G, max_nodes: int = 30) -> str:
        """Generate a `flowchart TD` block. Caps to `max_nodes` by degree
        so the result stays readable when copied into a doc."""
        if G.number_of_nodes() == 0:
            return "flowchart TD\n    empty[No nodes]"
        if G.number_of_nodes() > max_nodes:
            top = sorted(G.degree, key=lambda x: -x[1])[:max_nodes]
            keep = {n for n, _ in top}
            G = G.subgraph(keep).copy()

        lines = ["flowchart TD"]
        for n, attrs in G.nodes(data=True):
            label = str(attrs.get("label", n)).replace('"', "'")[:60]
            lines.append(f'    {self._safe_mermaid_id(n)}["{label}"]')
        for u, v, ed in G.edges(data=True):
            rel = (ed.get("relation") or "").strip()
            su, sv = self._safe_mermaid_id(u), self._safe_mermaid_id(v)
            if rel:
                lines.append(f"    {su} -->|{rel}| {sv}")
            else:
                lines.append(f"    {su} --> {sv}")
        return "\n".join(lines)

    def _show_mermaid_dialog(self, text: str) -> None:
        from tkinter import Toplevel
        win = Toplevel(self.root)
        win.title("Mermaid flowchart")
        win.geometry("680x520")
        try:
            win.configure(bg=PALETTE["bg"])
        except Exception:
            pass
        ttk.Label(
            win,
            text=(
                "Copy the block below and paste into a Mermaid renderer "
                "(GitHub, mermaid.live, Notion, etc.)."
            ),
            style="Dim.TLabel",
            wraplength=620,
        ).pack(anchor="w", padx=12, pady=(12, 4))

        body = ttk.Frame(win)
        body.pack(fill=BOTH, expand=True, padx=12, pady=(0, 4))
        txt = Text(
            body,
            wrap="none",
            background=PALETTE["panel_alt"],
            foreground=PALETTE["fg"],
            font=self._mono_font,
            relief="flat",
            borderwidth=0,
            padx=10,
            pady=8,
        )
        sb_y = ttk.Scrollbar(body, orient="vertical", command=txt.yview)
        sb_x = ttk.Scrollbar(body, orient="horizontal", command=txt.xview)
        txt.configure(yscrollcommand=sb_y.set, xscrollcommand=sb_x.set)
        sb_y.pack(side=RIGHT, fill="y")
        sb_x.pack(side="bottom", fill="x")
        txt.pack(side=LEFT, fill=BOTH, expand=True)
        txt.insert("1.0", text)
        txt.configure(state="normal")  # leave editable so the user can trim

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=12, pady=(4, 12))

        def copy_to_clipboard():
            self.root.clipboard_clear()
            self.root.clipboard_append(txt.get("1.0", "end-1c"))
            self.root.update()
        ttk.Button(
            bar, text="Copy to clipboard", style="Accent.TButton",
            command=copy_to_clipboard,
        ).pack(side=LEFT)
        ttk.Button(bar, text="Close", command=win.destroy).pack(side=RIGHT)

    # ===== Insights tab ===================================================
    #
    # Auto-computed structural metrics (pure networkx, no LLM):
    #   - top-N by PageRank, betweenness centrality
    #   - articulation points (single-node SPOFs)
    #   - bridges (single-edge SPOFs)
    #   - simple cycles up to length 4
    #   - modularity score on the existing community partition
    # Cached to <repo>/graphify-out/insights.json keyed by SHA(graph.json).

    INSIGHTS_FILE = "insights.json"

    def _build_insights_tab(self, parent: ttk.Frame) -> None:
        header = ttk.Frame(parent, style="Panel.TFrame")
        header.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(header, text="Structural Insights", style="Title.TLabel").pack(
            side=LEFT
        )
        self.insights_status_var = StringVar(value="(load a graph to compute)")
        ttk.Label(
            header, textvariable=self.insights_status_var,
            style="Dim.TLabel",
        ).pack(side=LEFT, padx=10)

        ttk.Button(
            header, text="Recompute",
            command=lambda: self._compute_insights(force=True),
        ).pack(side=RIGHT)

        body = ttk.Frame(parent, style="Panel.TFrame")
        body.pack(fill=BOTH, expand=True, padx=10, pady=(4, 10))
        self.insights_text = Text(
            body,
            wrap="word",
            background=PALETTE["panel_alt"],
            foreground=PALETTE["fg"],
            insertbackground=PALETTE["fg"],
            relief="flat",
            borderwidth=0,
            padx=10,
            pady=8,
            cursor="arrow",
        )
        sb = ttk.Scrollbar(body, orient="vertical", command=self.insights_text.yview)
        self.insights_text.configure(yscrollcommand=sb.set)
        sb.pack(side=RIGHT, fill="y")
        self.insights_text.pack(side=LEFT, fill=BOTH, expand=True)

        # Tags for styled output + clickable nodes/edges
        self.insights_text.tag_configure(
            "section",
            foreground=PALETTE["accent"],
            font=(self._mono_font[0], 11, "bold"),
            spacing3=4,
        )
        self.insights_text.tag_configure(
            "summary", foreground=PALETTE["fg"], spacing3=6,
        )
        self.insights_text.tag_configure(
            "dim", foreground=PALETTE["fg_dim"],
        )
        self.insights_text.tag_configure(
            "node",
            foreground=PALETTE["accent2"],
            underline=True,
        )
        self.insights_text.tag_bind(
            "node", "<Enter>",
            lambda e: self.insights_text.config(cursor="hand2"),
        )
        self.insights_text.tag_bind(
            "node", "<Leave>",
            lambda e: self.insights_text.config(cursor="arrow"),
        )

        # Map (start_index, end_index) of each clickable span to a node id
        self._insights_click_map: list[tuple[str, str, str]] = []
        self.insights_text.tag_bind(
            "node", "<Button-1>", self._on_insights_click,
        )
        self._set_insights_placeholder()

        self.insights_thread: threading.Thread | None = None

    def _set_insights_placeholder(self) -> None:
        self.insights_text.configure(state=NORMAL)
        self.insights_text.delete("1.0", END)
        self.insights_text.insert(
            END,
            "Load a graph (Build / Refresh) and the insights compute "
            "automatically. Cached to graphify-out/insights.json so "
            "subsequent loads are instant.\n",
            "dim",
        )
        self.insights_text.configure(state=DISABLED)

    def _insights_path(self) -> Path | None:
        path = self._selected_path(silent=True)
        if not path:
            return None
        return path / "graphify-out" / self.INSIGHTS_FILE

    def _compute_insights(self, force: bool = False) -> None:
        """Trigger insights compute. Loads from cache when SHA matches and
        not forced; otherwise spawns a background thread to compute fresh."""
        if not self.graph:
            return
        if self.insights_thread and self.insights_thread.is_alive():
            self.insights_status_var.set("Already computing - please wait...")
            return
        cache = self._insights_path()
        if not cache:
            return
        gj = cache.parent / "graph.json"
        if not gj.exists():
            return
        sha = _graph_sha(gj)
        # Cache hit?
        if not force and cache.exists():
            try:
                data = json.loads(cache.read_text(encoding="utf-8"))
            except Exception:
                data = None
            if data and data.get("graph_sha") == sha:
                self.insights_status_var.set(
                    f"loaded from cache ({data.get('graph_sha', '')[:8]})"
                )
                self._render_insights(data)
                return
        # Cache miss - compute fresh in worker
        self.insights_status_var.set("Computing insights...")
        self.insights_thread = threading.Thread(
            target=self._compute_insights_worker,
            args=(sha, cache),
            daemon=True,
        )
        self.insights_thread.start()

    def _compute_insights_worker(self, sha: str, cache_path: Path) -> None:
        try:
            data = self._compute_insights_data(sha)
        except Exception as exc:
            self.root.after(
                0,
                lambda e=exc: self.insights_status_var.set(
                    f"compute failed: {e}"
                ),
            )
            return
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(data, indent=2), encoding="utf-8",
            )
        except Exception:
            pass  # cache failure is non-fatal
        self.root.after(
            0,
            lambda: (
                self.insights_status_var.set(
                    f"computed in {data['_compute_secs']:.1f}s "
                    f"(cached: {self.INSIGHTS_FILE})"
                ),
                self._render_insights(data),
            ),
        )

    @staticmethod
    def _pagerank_pure(G, alpha: float = 0.85,
                        max_iter: int = 100, tol: float = 1e-6) -> dict:
        """Pure-numpy PageRank. Avoids the scipy dep modern nx.pagerank
        wants. Power iteration on a dense column-stochastic matrix; fine
        up to a few thousand nodes."""
        import numpy as np
        nodes = list(G.nodes())
        n = len(nodes)
        if n == 0:
            return {}
        idx = {nid: i for i, nid in enumerate(nodes)}
        M = np.zeros((n, n), dtype=np.float64)
        for u, v in G.edges():
            i, j = idx[u], idx[v]
            M[j, i] += 1.0
            if not G.is_directed():
                M[i, j] += 1.0
        col_sums = M.sum(axis=0)
        # Dangling nodes (no out-edges) - distribute uniformly.
        dangling = col_sums == 0
        col_sums[dangling] = 1.0
        M /= col_sums
        if dangling.any():
            M[:, dangling] = 1.0 / n
        teleport = (1.0 - alpha) / n
        pr = np.full(n, 1.0 / n, dtype=np.float64)
        for _ in range(max_iter):
            new_pr = alpha * (M @ pr) + teleport
            if np.abs(new_pr - pr).sum() < tol:
                pr = new_pr
                break
            pr = new_pr
        return {nodes[i]: float(pr[i]) for i in range(n)}

    def _compute_insights_data(self, graph_sha: str) -> dict:
        """Pure networkx + numpy. Heavy: betweenness is O(V*E), expect
        a few seconds on a ~1500-node graph."""
        import time as _t
        t0 = _t.time()
        G = self.graph
        N, E = G.number_of_nodes(), G.number_of_edges()

        # PageRank using our pure-numpy implementation.
        pr = self._pagerank_pure(G)
        pr_top = sorted(pr.items(), key=lambda x: -x[1])[:10]

        # Betweenness centrality. Approximated for speed on large graphs.
        if N > 800:
            btw = nx.betweenness_centrality(G, k=200, seed=7, normalized=True)
        else:
            btw = nx.betweenness_centrality(G, normalized=True)
        btw_top = sorted(btw.items(), key=lambda x: -x[1])[:10]

        # Articulation points + bridges work on undirected.
        UG = G.to_undirected() if G.is_directed() else G
        articulation = list(nx.articulation_points(UG))
        bridges = list(nx.bridges(UG))

        # Short cycles via cycle_basis (undirected) - keep <=4
        try:
            cb = nx.cycle_basis(UG)
            short_cycles = [c for c in cb if 3 <= len(c) <= 4][:30]
        except Exception:
            short_cycles = []

        # Modularity over existing community partition.
        modularity_score = None
        try:
            buckets: dict[int, list[str]] = {}
            for n, a in G.nodes(data=True):
                c = a.get("community")
                if c is None:
                    continue
                buckets.setdefault(int(c), []).append(n)
            comms = [set(v) for v in buckets.values() if v]
            if comms:
                modularity_score = nx.community.modularity(UG, comms)
        except Exception:
            modularity_score = None

        density = nx.density(UG)
        components = sorted(
            (len(c) for c in nx.connected_components(UG)),
            reverse=True,
        )[:10]

        def label(n: str) -> str:
            return G.nodes[n].get("label", n)

        return {
            "graph_sha": graph_sha,
            "n_nodes": N,
            "n_edges": E,
            "density": float(density),
            "components_top": components,
            "modularity": modularity_score,
            "pagerank_top": [
                {"id": n, "label": label(n), "score": float(s)}
                for n, s in pr_top
            ],
            "betweenness_top": [
                {"id": n, "label": label(n), "score": float(s)}
                for n, s in btw_top
            ],
            "articulation_points": [
                {"id": n, "label": label(n)} for n in articulation[:30]
            ],
            "bridges": [
                {"a": u, "b": v, "label_a": label(u), "label_b": label(v)}
                for u, v in bridges[:30]
            ],
            "short_cycles": [
                [{"id": n, "label": label(n)} for n in cyc]
                for cyc in short_cycles
            ],
            "_compute_secs": float(_t.time() - t0),
        }

    def _render_insights(self, data: dict) -> None:
        self._insights_click_map = []
        self.insights_text.configure(state=NORMAL)
        self.insights_text.delete("1.0", END)

        # ----- summary -----
        N = data.get("n_nodes", 0)
        E = data.get("n_edges", 0)
        dens = data.get("density", 0.0)
        mod = data.get("modularity")
        comps = data.get("components_top", [])
        summary = (
            f"{N} nodes, {E} edges, density {dens:.4f}\n"
            f"Connected components (top sizes): "
            f"{', '.join(str(s) for s in comps) or '(none)'}\n"
        )
        if mod is not None:
            summary += f"Modularity of community partition: {mod:.3f}\n"
        self._insights_section("Summary", summary)

        # ----- top by PageRank -----
        self._insights_section_header("Top 10 by PageRank")
        for r in data.get("pagerank_top", []):
            self._insights_clickable_node(
                f"  {r['score']:.4f}  ", r["id"], r["label"],
            )
            self.insights_text.insert(END, "\n")

        # ----- top by betweenness -----
        self._insights_section_header("Top 10 by betweenness centrality")
        for r in data.get("betweenness_top", []):
            self._insights_clickable_node(
                f"  {r['score']:.4f}  ", r["id"], r["label"],
            )
            self.insights_text.insert(END, "\n")

        # ----- articulation points -----
        ap = data.get("articulation_points", [])
        self._insights_section_header(
            f"Articulation points ({len(ap)} total) - removing one of these "
            "disconnects part of the graph"
        )
        if not ap:
            self.insights_text.insert(END, "  (none)\n", "dim")
        for r in ap[:20]:
            self._insights_clickable_node("  ", r["id"], r["label"])
            self.insights_text.insert(END, "\n")
        if len(ap) > 20:
            self.insights_text.insert(
                END, f"  ... +{len(ap) - 20} more\n", "dim",
            )

        # ----- bridges -----
        br = data.get("bridges", [])
        self._insights_section_header(
            f"Bridges ({len(br)} total) - critical edges; removing one "
            "disconnects part of the graph"
        )
        if not br:
            self.insights_text.insert(END, "  (none)\n", "dim")
        for r in br[:20]:
            self.insights_text.insert(END, "  ")
            self._insights_clickable_node("", r["a"], r["label_a"])
            self.insights_text.insert(END, "  -->  ")
            self._insights_clickable_node("", r["b"], r["label_b"])
            self.insights_text.insert(END, "\n")
        if len(br) > 20:
            self.insights_text.insert(
                END, f"  ... +{len(br) - 20} more\n", "dim",
            )

        # ----- short cycles -----
        cycs = data.get("short_cycles", [])
        self._insights_section_header(
            f"Short cycles ({len(cycs)} of length 3-4) - circular dependencies"
        )
        if not cycs:
            self.insights_text.insert(END, "  (none)\n", "dim")
        for cyc in cycs[:15]:
            self.insights_text.insert(END, "  ")
            for i, item in enumerate(cyc):
                self._insights_clickable_node("", item["id"], item["label"])
                if i < len(cyc) - 1:
                    self.insights_text.insert(END, " -> ")
            self.insights_text.insert(END, "\n")
        if len(cycs) > 15:
            self.insights_text.insert(
                END, f"  ... +{len(cycs) - 15} more\n", "dim",
            )

        self.insights_text.configure(state=DISABLED)

    def _insights_section(self, title: str, body: str) -> None:
        self.insights_text.insert(END, f"{title}\n", "section")
        self.insights_text.insert(END, body, "summary")
        self.insights_text.insert(END, "\n")

    def _insights_section_header(self, title: str) -> None:
        self.insights_text.insert(END, f"\n{title}\n", "section")

    def _insights_clickable_node(
        self, prefix: str, node_id: str, label: str
    ) -> None:
        if prefix:
            self.insights_text.insert(END, prefix)
        start = self.insights_text.index(END + "-1c")
        self.insights_text.insert(END, label, "node")
        end = self.insights_text.index(END + "-1c")
        # Track this span for click dispatch.
        self._insights_click_map.append((start, end, node_id))

    def _on_insights_click(self, event) -> None:
        idx = self.insights_text.index(f"@{event.x},{event.y}")
        for start, end, nid in self._insights_click_map:
            if self.insights_text.compare(start, "<=", idx) and \
               self.insights_text.compare(idx, "<", end):
                self._select_and_highlight_node(nid)
                return

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
            width=22,
        )
        self.chat_model_combo.pack(side=LEFT, fill="x", expand=True)
        ttk.Button(
            picker, text="Refresh", command=self._refresh_models
        ).pack(side=LEFT, padx=4)

        # Fast vs quality retrieval mode.
        mode_row = ttk.Frame(parent, style="Panel.TFrame")
        mode_row.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Label(mode_row, text="Retrieval:", style="Dim.TLabel").pack(
            side=LEFT, padx=(0, 6)
        )
        # Default Fast - it had better refusal/must-hit numbers in the
        # initial eval. Quality is now competitive once the embedding
        # index is built; see docs/CHAT_EVAL.md.
        self.chat_mode_var = StringVar(value="fast")
        ttk.Radiobutton(
            mode_row, text="Fast (BFS + snippets)",
            variable=self.chat_mode_var, value="fast",
        ).pack(side=LEFT, padx=4)
        ttk.Radiobutton(
            mode_row, text="Quality (planner ∪ BFS ∪ embed)",
            variable=self.chat_mode_var, value="quality",
        ).pack(side=LEFT, padx=4)

        # Embedding index controls.
        idx_row = ttk.Frame(parent, style="Panel.TFrame")
        idx_row.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Label(idx_row, text="Index:", style="Dim.TLabel").pack(
            side=LEFT, padx=(0, 6)
        )
        self.embed_status_var = StringVar(value="not built")
        ttk.Label(
            idx_row, textvariable=self.embed_status_var, style="Dim.TLabel"
        ).pack(side=LEFT, padx=(0, 6))
        ttk.Button(
            idx_row, text="Build Embedding Index",
            command=self._build_embed_index,
        ).pack(side=LEFT, padx=4)
        ttk.Button(
            idx_row, text="Cancel build",
            command=lambda: self.embed_stop_event.set(),
        ).pack(side=LEFT, padx=4)

        # Input row - PACK FIRST AND ANCHOR TO BOTTOM, so when the window
        # gets squeezed the transcript shrinks but the input stays put.
        inp = ttk.Frame(parent, style="Panel.TFrame")
        inp.pack(side="bottom", fill="x", padx=10, pady=(0, 10))
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

        # Conversation transcript fills the remaining space above the input.
        body = ttk.Frame(parent, style="Panel.TFrame")
        body.pack(side="top", fill=BOTH, expand=True, padx=10, pady=(0, 6))
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

        # State
        self.ollama = OllamaClient()
        self.chat_history: list[dict] = []
        self.chat_stop_event = threading.Event()
        self.chat_thread: threading.Thread | None = None
        # Cross-thread queue for the background model probe - the poller
        # drains it on the main thread so we never touch Tk from a worker.
        self._model_probe_result: queue.Queue = queue.Queue()

        # Embedding index state (numpy ndarray, parallel list of node ids).
        self.embed_vectors = None
        self.embed_node_ids: list[str] = []
        self.embed_model: str = ""
        self.embed_thread: threading.Thread | None = None
        self.embed_stop_event = threading.Event()

        # Initial model probe (runs in background - don't block UI startup).
        threading.Thread(target=self._refresh_models_async, daemon=True).start()

    # ---------------------------------------------------------- chat actions

    # ---------------------------------------------------------- embedding idx

    def _try_load_embed_index(self) -> None:
        """Look for graphify-out/embeddings.npz next to the loaded graph and
        load it if its SHA matches the current graph.json. Called whenever a
        new graph is loaded into the view."""
        path = self._selected_path(silent=True)
        if not path or not self.graph:
            return
        gj = path / "graphify-out" / "graph.json"
        if not gj.exists():
            return
        try:
            sha = _graph_sha(gj)
            cached = load_embedding_index(
                path / "graphify-out", expect_graph_sha=sha
            )
        except Exception:
            cached = None
        if cached is None:
            self.embed_vectors = None
            self.embed_node_ids = []
            self.embed_model = ""
            self.embed_status_var.set(
                f"not built ({self.graph.number_of_nodes()} nodes pending)"
            )
            return
        vectors, node_ids, model = cached
        self.embed_vectors = vectors
        self.embed_node_ids = node_ids
        self.embed_model = model
        self.embed_status_var.set(
            f"loaded - {len(node_ids)} vecs ({model})"
        )

    def _build_embed_index(self) -> None:
        if self.embed_thread and self.embed_thread.is_alive():
            messagebox.showinfo("Busy", "Embedding build already running.")
            return
        if not self.graph:
            messagebox.showwarning(
                "No graph",
                "Load or build a graph first - the index needs nodes to embed.",
            )
            return
        path = self._selected_path()
        if not path:
            return
        if not self.ollama.is_up():
            messagebox.showerror(
                "Ollama not running",
                "Ollama is required for embeddings. Start it and run "
                "`ollama pull nomic-embed-text`.",
            )
            return
        self.embed_stop_event = threading.Event()
        self.embed_thread = threading.Thread(
            target=self._build_embed_worker,
            args=(path,),
            daemon=True,
        )
        self.embed_thread.start()

    def _build_embed_worker(self, path: Path) -> None:
        gj = path / "graphify-out" / "graph.json"
        try:
            sha = _graph_sha(gj)
        except Exception as exc:
            self.root.after(
                0,
                lambda: self.embed_status_var.set(f"build failed: {exc}"),
            )
            return

        def progress(done: int, total: int):
            self.root.after(
                0,
                lambda d=done, t=total: self.embed_status_var.set(
                    f"building {d}/{t}..."
                ),
            )

        try:
            vectors, node_ids, model = build_embedding_index(
                self.graph, path,
                workers=4,
                progress=progress,
                stop_event=self.embed_stop_event,
            )
        except Exception as exc:
            self.root.after(
                0,
                lambda e=exc: self.embed_status_var.set(f"build failed: {e}"),
            )
            return

        try:
            save_embedding_index(
                path / "graphify-out", vectors, node_ids, model, sha
            )
        except Exception as exc:
            self.root.after(
                0,
                lambda e=exc: self.embed_status_var.set(
                    f"saved failed: {e} (in-memory only)"
                ),
            )

        def apply():
            self.embed_vectors = vectors
            self.embed_node_ids = node_ids
            self.embed_model = model
            self.embed_status_var.set(
                f"built - {len(node_ids)} vecs ({model})"
            )
        self.root.after(0, apply)

    def _embed_topk_node_ids(
        self, query: str, k: int = 8
    ) -> list[str]:
        if self.embed_vectors is None or len(self.embed_node_ids) == 0:
            return []
        if not self.embed_model:
            return []
        qv = self.ollama.embed_one(self.embed_model, query)
        if qv is None:
            return []
        top = cosine_topk(qv, self.embed_vectors, k=k)
        return [self.embed_node_ids[i] for i, _ in top]

    def _refresh_models(self) -> None:
        threading.Thread(target=self._refresh_models_async, daemon=True).start()

    def _refresh_models_async(self) -> None:
        # Worker thread - do NOT touch Tk widgets from here. Push the result
        # onto a queue; the main-thread poller picks it up.
        if not self.ollama.is_up():
            self._model_probe_result.put(("status", (
                "Ollama not running on localhost:11434. "
                "Install from https://ollama.com and `ollama pull qwen2.5:7b`."
            )))
            return
        models = self.ollama.list_models()
        if models:
            self._model_probe_result.put(("models", models))
        else:
            self._model_probe_result.put(("status", (
                "Ollama is up but no chat models installed. "
                "Try `ollama pull qwen2.5:7b`."
            )))

    def _drain_model_probe(self) -> None:
        """Called from the main-thread poll tick - safe to touch Tk."""
        try:
            while True:
                kind, payload = self._model_probe_result.get_nowait()
                if kind == "status":
                    self.chat_status_var.set(payload)
                elif kind == "models":
                    self.chat_model_combo["values"] = payload
                    if (
                        not self.chat_model_var.get()
                        or self.chat_model_var.get() not in payload
                    ):
                        preferred = next(
                            (
                                m
                                for m in payload
                                if "coder" in m.lower() or "code" in m.lower()
                            ),
                            payload[0],
                        )
                        self.chat_model_var.set(preferred)
                    self.chat_status_var.set(
                        f"connected - {len(payload)} model(s) available"
                    )
        except queue.Empty:
            pass

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
        """Build context and stream an answer. Two retrieval modes:

        quality - 2-stage two-stage: planner picks node ids, drill
                  reads their source + neighbours, then answer.
        fast    - skip the planner; BFS slice + source snippets only.
        """
        path: Path | None = None
        try:
            p = self.path_var.get().strip()
            if p and not is_url(p):
                path = Path(p).expanduser()
        except Exception:
            pass

        mode = self.chat_mode_var.get()
        report_excerpt = self._read_report_excerpt(path, limit=1500)

        # BFS slice always runs - it's primary in fast mode and a guaranteed
        # baseline in quality mode (so quality is never worse than fast).
        bfs_out = self._run_graphify_capture(
            ["query", question, "--budget", "1200"], cwd=path
        )
        bfs_picks = self._node_ids_from_bfs(bfs_out)

        planner_picks: list[str] = []
        embed_picks: list[str] = []
        if mode == "quality":
            toc, all_ids = self._build_graph_toc(path)
            if toc and all_ids:
                planner_picks = self._plan_retrieval(
                    model=model,
                    question=question,
                    toc=toc,
                    report=report_excerpt,
                    valid_ids=all_ids,
                )
            # Embedding top-K (no-op if no index loaded).
            embed_picks = self._embed_topk_node_ids(question, k=8)

        # In quality mode: union planner ∪ BFS ∪ embed for the broadest
        # recall. In fast mode: BFS only.
        if mode == "quality":
            seen: set[str] = set()
            picked: list[str] = []
            for nid in (*planner_picks, *bfs_picks, *embed_picks):
                if nid not in seen:
                    seen.add(nid)
                    picked.append(nid)
                if len(picked) >= 12:
                    break
        else:
            picked = bfs_picks[:10]

        snippets = self._snippets_for_node_ids(picked, path) if picked else []
        bfs_snippets = self._collect_source_snippets(bfs_out, path)

        def announce():
            if mode == "fast":
                self._chat_append("\n[fast: BFS picked ", "system")
                self._chat_append(
                    ", ".join(picked[:8]) if picked else "(nothing)",
                    "citation",
                )
                self._chat_append("]\n", "system")
            elif picked:
                self._chat_append(
                    f"\n[quality: planner {len(planner_picks)} ∪ BFS "
                    f"{len(bfs_picks)} ∪ embed {len(embed_picks)} → ",
                    "system",
                )
                self._chat_append(", ".join(picked[:8]), "citation")
                self._chat_append("]\n", "system")
            else:
                self._chat_append(
                    "\n[no graph context available for this question]\n",
                    "system",
                )
        self.root.after(0, announce)

        # If the Interactive Graph window is open, highlight the picked
        # nodes there too. No-op when the subprocess isn't running.
        if picked:
            self._highlight_in_vis(picked)

        # ---------- Stage C: build context and stream the answer -----------
        ctx_lines: list[str] = []
        if snippets:
            ctx_lines.append("=== Sources picked by retrieval planner ===")
            ctx_lines.extend(snippets)
            ctx_lines.append("")
        if bfs_out:
            ctx_lines.append("=== BFS context (from `graphify query`) ===")
            ctx_lines.append(bfs_out.strip()[:2500])
            ctx_lines.append("")
        if bfs_snippets and not snippets:
            ctx_lines.append("=== BFS source snippets (fallback) ===")
            ctx_lines.extend(bfs_snippets)
            ctx_lines.append("")
        if report_excerpt:
            ctx_lines.append("=== GRAPH_REPORT.md (excerpt) ===")
            ctx_lines.append(report_excerpt[:1500])

        context = "\n".join(ctx_lines).strip() or (
            "(no graph context - the user has not built a graph yet)"
        )

        system_prompt = (
            "You answer questions about ONE specific code repository "
            "using ONLY the context block below.\n\n"
            "Rules:\n"
            "1. If the answer isn't directly supported by the context, "
            "reply exactly: 'I don't know based on the graph.' Do not "
            "guess.\n"
            "2. Cite every factual claim with [node_id] from the context.\n"
            "3. Never invent file names, function names, or relations.\n"
            "4. Be concise: 1-3 sentences plus a short bullet list if "
            "multiple items apply.\n"
            "5. Do not summarize these rules back to the user."
        )
        user_msg = f"Context:\n{context}\n\nQuestion: {question}"

        msgs: list[dict] = [{"role": "system", "content": system_prompt}]
        for turn in self.chat_history[-4:]:
            msgs.append(turn)
        msgs.append({"role": "user", "content": user_msg})

        self.root.after(0, lambda: self._chat_append("\nAssistant: ", "user"))

        full_reply: list[str] = []
        for chunk in self.ollama.stream_chat(
            model=model,
            messages=msgs,
            stop_event=self.chat_stop_event,
            options={
                "temperature": 0.1,
                "top_p": 0.9,
                "num_ctx": 8192,
            },
        ):
            full_reply.append(chunk)
            self.root.after(0, lambda c=chunk: self._chat_append(c, "assistant"))
            if self.chat_stop_event.is_set():
                break

        if not full_reply:
            self.root.after(
                0, lambda: self._chat_append("(no response)\n", "system")
            )

        self.chat_history.append({"role": "user", "content": question})
        self.chat_history.append(
            {"role": "assistant", "content": "".join(full_reply)}
        )
        if len(self.chat_history) > 8:
            self.chat_history = self.chat_history[-8:]
        self.root.after(0, lambda: self._chat_append("\n"))

    # ------------------------------------------------------------- TOC + plan

    def _build_graph_toc(
        self, path: Path | None, max_per_community: int = 12
    ) -> tuple[str, set[str]]:
        """Produce a compact 'table of contents' of the loaded graph for the
        planner. Returns (toc_text, set_of_valid_node_ids)."""
        if not self.graph:
            return "", set()
        G = self.graph
        # Group node ids by community.
        by_comm: dict[int, list[str]] = {}
        for nid, attrs in G.nodes(data=True):
            c = int(attrs.get("community", 0) or 0)
            by_comm.setdefault(c, []).append(nid)

        lines: list[str] = []
        valid: set[str] = set()
        for c in sorted(by_comm):
            members = by_comm[c]
            # Top-N per community by degree.
            members.sort(key=lambda n: -G.degree(n))
            members = members[:max_per_community]
            lines.append(f"-- community {c} ({len(by_comm[c])} nodes total) --")
            for nid in members:
                attrs = G.nodes[nid]
                label = attrs.get("label", nid)
                src = attrs.get("source_file", "")
                deg = G.degree(nid)
                lines.append(
                    f"  {nid}  label={label!r}  src={src}  degree={deg}"
                )
                valid.add(nid)
        return "\n".join(lines), valid

    def _read_report_excerpt(self, path: Path | None, limit: int) -> str:
        if not path:
            return ""
        report = path / "graphify-out" / "GRAPH_REPORT.md"
        if not report.exists():
            return ""
        try:
            return report.read_text(encoding="utf-8")[:limit]
        except OSError:
            return ""

    def _plan_retrieval(
        self,
        model: str,
        question: str,
        toc: str,
        report: str,
        valid_ids: set[str],
    ) -> list[str]:
        """Single non-streaming LLM call that returns 3-6 node ids to read."""
        plan_prompt = (
            "You route questions to nodes in a code graph. Each TOC entry "
            "looks like:\n"
            "    <node_id>  label='<label>'  src=<file>  degree=<n>\n"
            "Pick 3-6 node_ids whose source code most likely contains the "
            "answer. Take the first whitespace-separated token on each "
            "line - NOT the label, NOT the src.\n\n"
            "Output ONLY a JSON object, no prose, no code fences:\n"
            '{"nodes": ["node_id_1", "node_id_2", "node_id_3"]}\n\n'
            "Example: if a TOC line is\n"
            "    cluster_run_leiden  label='run_leiden'  src=cluster.py "
            "degree=8\n"
            "then a valid pick is \"cluster_run_leiden\"."
        )
        # Include last 2 turns so follow-ups stay on-topic.
        history_lines: list[str] = []
        for turn in self.chat_history[-4:]:
            history_lines.append(f"{turn['role']}: {turn['content'][:300]}")
        history_block = "\n".join(history_lines)

        user_msg = (
            f"Graph table of contents:\n{toc[:6000]}\n\n"
            f"Graph summary:\n{report[:800]}\n\n"
            f"Conversation so far:\n{history_block or '(none)'}\n\n"
            f"Question: {question}"
        )
        msgs = [
            {"role": "system", "content": plan_prompt},
            {"role": "user", "content": user_msg},
        ]
        # Re-use stream_chat but collect to one string.
        local_stop = threading.Event()
        chunks: list[str] = []
        for c in self.ollama.stream_chat(
            model=model,
            messages=msgs,
            stop_event=local_stop,
            options={"temperature": 0.0, "num_ctx": 8192},
        ):
            chunks.append(c)
            if self.chat_stop_event.is_set():
                local_stop.set()
                break
        raw = "".join(chunks).strip()
        return self._parse_plan(raw, valid_ids)

    def _parse_plan(self, raw: str, valid_ids: set[str]) -> list[str]:
        """Extract a JSON list of node ids; tolerate fenced code or junk.

        Falls back to fuzzy matching: if the model emits a label or src
        path instead of the canonical node id, map it back."""
        m = re.search(r"\{[^{}]*\"nodes\"[^{}]*\}", raw, re.DOTALL)
        if not m:
            return []
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
        candidates = obj.get("nodes") or []
        if not isinstance(candidates, list):
            return []

        out: list[str] = []
        seen: set[str] = set()
        for x in candidates:
            if not isinstance(x, str):
                continue
            mapped = self._match_to_node_id(x, valid_ids)
            for nid in mapped:
                if nid not in seen:
                    seen.add(nid)
                    out.append(nid)
                if len(out) >= 6:
                    return out
        return out

    def _match_to_node_id(self, x: str, valid_ids: set[str]) -> list[str]:
        """Try exact id, then label, then source-file suffix match."""
        if x in valid_ids:
            return [x]
        if not self.graph:
            return []
        # Label match (exact, case-insensitive).
        x_low = x.lower()
        by_label: list[str] = []
        by_src: list[str] = []
        # Normalise path separators so models that emit forward slashes
        # match graph nodes whose src has backslashes (or vice versa).
        x_norm = x.replace("\\", "/").lower()
        for nid in valid_ids:
            attrs = self.graph.nodes.get(nid, {})
            label = str(attrs.get("label", "")).lower()
            src = str(attrs.get("source_file", "")).replace("\\", "/").lower()
            if label == x_low or label.rstrip("()") == x_low.rstrip("()"):
                by_label.append(nid)
            elif src and (src == x_norm or src.endswith("/" + x_norm) or x_norm.endswith("/" + src)):
                by_src.append(nid)
        # Prefer label matches; if none, fall back to src matches (cap to 2
        # so a single "models.py" match doesn't dominate the picks).
        return by_label[:2] if by_label else by_src[:2]

    def _snippets_for_node_ids(
        self, ids: list[str], path: Path | None
    ) -> list[str]:
        """Read source snippets for picked nodes + their direct neighbours."""
        if not ids or not self.graph or not path:
            return []
        seen: set[tuple[str, int]] = set()
        out: list[str] = []
        # Include 1-hop neighbours so the model gets call/contains context.
        widened: list[str] = list(ids)
        for nid in ids:
            for n in list(self.graph.neighbors(nid))[:3]:
                if n not in widened:
                    widened.append(n)
                if len(widened) >= 14:
                    break
        for nid in widened:
            attrs = self.graph.nodes.get(nid, {})
            src = attrs.get("source_file", "")
            loc = str(attrs.get("source_location", "")).lstrip("L")
            if not src or not loc:
                continue
            try:
                line_no = int(loc)
            except ValueError:
                continue
            key = (src, line_no)
            if key in seen:
                continue
            seen.add(key)
            file_path = (path / src).resolve()
            if not file_path.exists() or not file_path.is_file():
                continue
            try:
                lines = file_path.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
            except OSError:
                continue
            start = max(0, line_no - 3)
            end = min(len(lines), line_no + 12)
            snippet = "\n".join(
                f"  {i + 1:4d}: {lines[i]}" for i in range(start, end)
            )
            out.append(f"-- [{nid}] {src} L{line_no} --\n{snippet}")
            if len(out) >= 8:
                break
        return out

    # Pulls "[src=<file> loc=L<line>]" tuples out of `graphify query` output
    # and reads ~12 lines around each one. Capped at 6 snippets to keep the
    # prompt lean.
    _NODE_LINE_RE = re.compile(
        r"NODE\s+(.+?)\s+\[src=(?P<src>[^\s]+)\s+loc=(?P<loc>L?\d+)"
    )

    def _node_ids_from_bfs(self, bfs_out: str) -> list[str]:
        """Map NODE lines from `graphify query` output back to graph node
        ids by (source_file, source_location) lookup."""
        if not bfs_out or not self.graph:
            return []
        # Build a (src, loc) -> id index once.
        index: dict[tuple[str, str], str] = {}
        for nid, attrs in self.graph.nodes(data=True):
            src = str(attrs.get("source_file", ""))
            loc = str(attrs.get("source_location", "")).lstrip("L")
            if src and loc:
                index[(src, loc)] = nid
        out: list[str] = []
        seen: set[str] = set()
        for m in self._NODE_LINE_RE.finditer(bfs_out):
            key = (m.group("src"), m.group("loc").lstrip("L"))
            nid = index.get(key)
            if nid and nid not in seen:
                seen.add(nid)
                out.append(nid)
        return out

    def _collect_source_snippets(
        self, query_out: str, path: Path | None
    ) -> list[str]:
        if not query_out or not path:
            return []
        seen: set[tuple[str, str]] = set()
        out: list[str] = []
        for m in self._NODE_LINE_RE.finditer(query_out):
            src = m.group("src")
            loc = m.group("loc").lstrip("L")
            key = (src, loc)
            if key in seen:
                continue
            seen.add(key)
            try:
                line_no = int(loc)
            except ValueError:
                continue
            file_path = (path / src).resolve()
            if not file_path.exists() or not file_path.is_file():
                continue
            try:
                lines = file_path.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
            except OSError:
                continue
            start = max(0, line_no - 3)
            end = min(len(lines), line_no + 10)
            snippet = "\n".join(
                f"  {i + 1:4d}: {lines[i]}" for i in range(start, end)
            )
            out.append(f"-- {src} L{line_no} --\n{snippet}")
            if len(out) >= 6:
                break
        return out

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

    def _graph_path(self) -> Path | None:
        path = self._selected_path(silent=True)
        if not path:
            return None
        return path / "graphify-out" / "graph.json"

    def _load_graph_into_view(self) -> None:
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
            messagebox.showerror(
                "Load failed",
                f"Could not parse graph.json:\n{exc}",
            )
            return
        self.graph = G
        self.graph_meta_var.set(
            f"{G.number_of_nodes()} nodes / {G.number_of_edges()} edges - "
            f"loaded from {gp.name}"
        )

        # Try to attach a previously-built embedding index (no-op if missing
        # or stale by SHA).
        try:
            self._try_load_embed_index()
        except Exception:
            pass
        # Populate the Browse tab (entry points + filesystem tree).
        try:
            self._refresh_browse_from_graph()
        except Exception:
            pass
        # Trigger insights compute (cached if SHA matches; else background).
        try:
            self._compute_insights(force=False)
        except Exception:
            pass
        # Auto-launch the Interactive Graph window unless a vis subprocess
        # is already running and pointing at the same graph.
        try:
            self._auto_launch_interactive_view()
        except Exception:
            pass

    # ---- focus mode (delegates to vis.js subprocess via IPC) ------------

    def _n_hop_subgraph(self, center: str, hops: int):
        """Return the subgraph induced by `center` and its <=hops neighbours.
        Used both for the vis.js focus IPC and the scoped Mermaid export."""
        if not self.graph or center not in self.graph:
            return None
        keep: set[str] = {center}
        frontier = {center}
        for _ in range(max(0, hops)):
            next_frontier: set[str] = set()
            for n in frontier:
                next_frontier.update(self.graph.neighbors(n))
            next_frontier -= keep
            keep |= next_frontier
            frontier = next_frontier
            if not frontier:
                break
        return self.graph.subgraph(keep).copy()

    def _focus_in_vis(self) -> None:
        """Tell the Interactive Graph window to focus the camera on the
        selected node's 1-hop neighbourhood."""
        if not self.graph:
            messagebox.showwarning("No graph", "Load or build a graph first.")
            return
        if not self.selected_node:
            messagebox.showinfo(
                "No selection",
                "Click a node (in Browse, Insights, or the Interactive "
                "window) first, then 'Focus on Selected'.",
            )
            return
        sub = self._n_hop_subgraph(self.selected_node, 1)
        if sub is None or sub.number_of_nodes() == 0:
            messagebox.showinfo(
                "Empty",
                "Selected node has no neighbours within 1 hop.",
            )
            return
        ids = [n for n in sub.nodes()]
        if not self._vis_send({"cmd": "focus", "ids": ids}):
            # Vis window not running; open it then queue the focus.
            self._open_interactive_view()
            self.root.after(
                4500,
                lambda i=ids: self._vis_send({"cmd": "focus", "ids": i}),
            )

    def _reset_focus_in_vis(self) -> None:
        if not self._vis_send({"cmd": "reset_focus"}):
            return  # no vis window; nothing to reset

    def _auto_launch_interactive_view(self) -> None:
        """Spawn the Interactive Graph window if it isn't already up. Quiet
        no-op if pywebview is missing - the user can click 'Reopen Graph'
        and get the install hint message."""
        proc = getattr(self, "vis_proc", None)
        if proc and proc.poll() is None:
            # Already running - just refresh the highlight.
            return
        # Defer slightly so the GUI's own paint settles first.
        self.root.after(150, self._open_interactive_view_quiet)

    def _open_interactive_view_quiet(self) -> None:
        """Same as _open_interactive_view but swallows the 'pywebview not
        installed' popup - we surface that in the left-pane status line
        instead so the user isn't blocked by a modal."""
        path = self._selected_path(silent=True)
        if not path:
            return
        html = path / "graphify-out" / "graph.html"
        if not html.exists():
            self.viz_status_var.set(
                "graph.html not found yet - run Build / Refresh"
            )
            return
        try:
            r = subprocess.run(
                [sys.executable, "-c", "import webview"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                self.viz_status_var.set(
                    "pywebview not installed; install via "
                    "Install-*.bat / .command / .sh and reopen Graphify"
                )
                return
        except Exception as exc:
            self.viz_status_var.set(f"pywebview probe failed: {exc}")
            return
        self._open_interactive_view()
        # On Windows: try to reparent the popup into the left-pane embed
        # frame. Falls through silently if anything goes wrong (popup
        # remains visible as a separate window in that case).
        if os.name == "nt":
            self._embed_poll_count = 0
            self.root.after(500, self._embed_poll_tick)

    # ---- Win32 window embedding (Windows-only) --------------------------
    #
    # The pywebview subprocess opens its own top-level window. We find it
    # by title via EnumWindows, strip its decorations, and SetParent it
    # into the Tk embed frame. Resize is forwarded via the embed frame's
    # Configure event.

    @staticmethod
    def _find_window_by_title(substr: str) -> int | None:
        if os.name != "nt":
            return None
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        EnumWindowsProc = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM,
        )
        found = {"hwnd": None}

        def cb(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buf, 512)
            if substr.lower() in buf.value.lower():
                found["hwnd"] = int(hwnd)
                return False  # stop enumeration
            return True

        user32.EnumWindows(EnumWindowsProc(cb), 0)
        return found["hwnd"]

    def _embed_poll_tick(self) -> None:
        """Poll for the vis.js window and embed it once it appears."""
        if os.name != "nt":
            return
        if self._vis_hwnd is not None:
            return  # already embedded
        proc = getattr(self, "vis_proc", None)
        if not proc or proc.poll() is not None:
            return  # subprocess died; give up
        self._embed_poll_count += 1
        hwnd = self._find_window_by_title("Graphify - Interactive")
        if hwnd:
            try:
                self._embed_hwnd_in_frame(hwnd)
                self._vis_hwnd = hwnd
                self.viz_status_var.set("Interactive Graph: embedded")
                # Hide the placeholder text once embedded.
                if hasattr(self, "_embed_placeholder"):
                    try:
                        self._embed_placeholder.pack_forget()
                    except Exception:
                        pass
                return
            except Exception as exc:
                self.viz_status_var.set(
                    f"embedding failed: {exc} - using popup window"
                )
                return
        # Not yet up. Keep polling for ~15 seconds.
        if self._embed_poll_count < 75:
            self.root.after(200, self._embed_poll_tick)

    def _embed_hwnd_in_frame(self, child_hwnd: int) -> None:
        """Reparent `child_hwnd` into `self.embed_frame` and strip
        decorations. Windows-only (caller checks os.name)."""
        import ctypes
        user32 = ctypes.windll.user32
        GWL_STYLE       = -16
        WS_CAPTION      = 0x00C00000
        WS_THICKFRAME   = 0x00040000
        WS_SYSMENU      = 0x00080000
        WS_MINIMIZEBOX  = 0x00020000
        WS_MAXIMIZEBOX  = 0x00010000
        WS_CHILD        = 0x40000000
        WS_VISIBLE      = 0x10000000
        WS_POPUP        = 0x80000000
        SWP_NOZORDER    = 0x0004
        SWP_FRAMECHANGED = 0x0020
        SWP_SHOWWINDOW  = 0x0040

        # GetWindowLongPtrW / SetWindowLongPtrW are 64-bit safe; fall
        # back to 32-bit variants if not present (very old Windows).
        get_long = getattr(user32, "GetWindowLongPtrW", None) \
            or user32.GetWindowLongW
        set_long = getattr(user32, "SetWindowLongPtrW", None) \
            or user32.SetWindowLongW

        # Strip caption + thick frame + sys menu, set as child window.
        style = get_long(child_hwnd, GWL_STYLE)
        new_style = (
            (style
             & ~WS_CAPTION & ~WS_THICKFRAME & ~WS_SYSMENU
             & ~WS_MINIMIZEBOX & ~WS_MAXIMIZEBOX & ~WS_POPUP)
            | WS_CHILD | WS_VISIBLE
        )
        set_long(child_hwnd, GWL_STYLE, new_style)

        # Reparent: SetParent(child, parent_hwnd_of_tk_frame).
        parent_hwnd = self.embed_frame.winfo_id()
        user32.SetParent(child_hwnd, parent_hwnd)

        # Resize to fill the frame.
        self.embed_frame.update_idletasks()
        w = max(1, self.embed_frame.winfo_width())
        h = max(1, self.embed_frame.winfo_height())
        user32.SetWindowPos(
            child_hwnd, 0, 0, 0, w, h,
            SWP_FRAMECHANGED | SWP_NOZORDER | SWP_SHOWWINDOW,
        )

    def _on_embed_configure(self, event) -> None:
        """Resize the embedded child to match the embed frame."""
        if os.name != "nt":
            return
        hwnd = getattr(self, "_vis_hwnd", None)
        if not hwnd:
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32
            SWP_NOZORDER = 0x0004
            user32.SetWindowPos(
                hwnd, 0, 0, 0, max(1, event.width), max(1, event.height),
                SWP_NOZORDER,
            )
        except Exception:
            pass

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

    def _node_breadcrumb(self, key: str) -> str:
        """Render a path-style breadcrumb: Codebase > dir/ > file > label."""
        if not self.graph or key not in self.graph:
            return "Codebase"
        attrs = self.graph.nodes[key]
        src = (attrs.get("source_file") or "").replace("\\", "/")
        label = attrs.get("label") or key
        parts = ["Codebase"]
        if src:
            segs = src.split("/")
            for d in segs[:-1]:
                parts.append(d + "/")
            parts.append(segs[-1])
        parts.append(label)
        return "  >  ".join(parts)

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
            rels.append(f"  - {rel:<10} -> {self.graph.nodes[n].get('label', n)} [{conf}]")

        lines = [
            self._node_breadcrumb(key),
            "",
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
        self._begin_job(
            label="cloning" if not already else "pulling latest",
            alert=False,
        )

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

    # ---- interactive (vis.js via pywebview subprocess) -------------------

    def _open_interactive_view(self) -> None:
        """Open the Interactive Graph, or - if it's already running -
        reset its state to the full graph view (clear focus + highlight
        + fit camera). The 'Reopen Graph' button uses this so it works
        as an unstick / reset whether the window is closed or not."""
        if getattr(self, "vis_proc", None) and self.vis_proc.poll() is None:
            # Already running: act as a "reset to full view".
            self._vis_send({"cmd": "reset_focus"})
            self._vis_send({"cmd": "clear_highlight"})
            self._vis_send({"cmd": "fit"})
            self.viz_status_var.set("Interactive Graph: reset to full view")
            return
        path = self._selected_path()
        if not path:
            return
        html = path / "graphify-out" / "graph.html"
        if not html.exists():
            messagebox.showinfo(
                "Not built",
                f"No graph.html yet. Build the graph first.\n\nLooked at:\n{html}",
            )
            return
        script = APP_DIR / "graphify_vis_window.py"
        if not script.exists():
            messagebox.showerror(
                "Missing helper",
                f"graphify_vis_window.py not found next to the GUI.",
            )
            return
        # Probe pywebview availability before spawning - friendlier error.
        try:
            r = subprocess.run(
                [sys.executable, "-c", "import webview"],
                capture_output=True, text=True, timeout=10,
            )
        except Exception as exc:
            messagebox.showerror("pywebview check failed", str(exc))
            return
        if r.returncode != 0:
            messagebox.showerror(
                "pywebview not installed",
                "The Interactive Graph view needs pywebview.\n\n"
                "Install it with:\n"
                "    .venv/Scripts/pip install pywebview\n\n"
                "On Windows it uses Edge WebView2 (already in Win10+).\n"
                "On macOS it uses WKWebView (built in).\n"
                "On Linux you may also need: sudo apt install python3-gi "
                "gir1.2-webkit2-4.0",
            )
            return
        try:
            self.vis_proc = subprocess.Popen(
                [sys.executable, str(script), str(html)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            messagebox.showerror("Failed to start", f"{exc}")
            return
        self._set_status("Interactive Graph: launching...")
        threading.Thread(
            target=self._vis_event_loop, daemon=True
        ).start()

    def _vis_event_loop(self) -> None:
        """Read JSON events from the subprocess and dispatch to the main
        thread via root.after."""
        proc = self.vis_proc
        if not proc or not proc.stdout:
            return
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            ev = msg.get("event")
            if ev == "ready":
                self.root.after(
                    0, lambda: self._set_status("Interactive Graph: ready")
                )
            elif ev == "click":
                nid = msg.get("id")
                if nid:
                    self.root.after(
                        0, lambda i=nid: self._select_node_from_vis(i)
                    )
            elif ev == "double_click":
                nid = msg.get("id")
                if nid:
                    self.root.after(
                        0, lambda i=nid: self._select_node_from_vis(i)
                    )
        # Subprocess exited.
        def _on_exit():
            self._set_status("Interactive Graph: closed")
            try:
                self.viz_status_var.set("Interactive Graph: closed")
            except Exception:
                pass
            # Clear embedded HWND state so a future _open_interactive_view
            # can re-embed cleanly.
            if hasattr(self, "_vis_hwnd"):
                self._vis_hwnd = None
            if hasattr(self, "_embed_placeholder"):
                try:
                    self._embed_placeholder.pack(expand=True)
                except Exception:
                    pass
        self.root.after(0, _on_exit)

    def _select_node_from_vis(self, node_id: str) -> None:
        """Surface a node clicked in the vis.js window in the Details tab."""
        if not self.graph or node_id not in self.graph:
            return
        self.selected_node = node_id
        self._show_node_details(node_id)
        try:
            # Switch focus to the Details tab so the user sees the result.
            self.notebook.select(0)
        except Exception:
            pass

    def _vis_send(self, payload: dict) -> bool:
        """Send a JSON command to the subprocess. Returns False if not
        running. Safe to call from any chat-worker thread."""
        proc = getattr(self, "vis_proc", None)
        if not proc or proc.poll() is not None or not proc.stdin:
            return False
        try:
            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()
            return True
        except (OSError, BrokenPipeError):
            return False

    def _highlight_in_vis(self, node_ids: list[str]) -> None:
        if node_ids:
            self._vis_send({"cmd": "highlight", "ids": list(node_ids)})

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
        self._begin_job(label=f"graphify {args[0] if args else ''}", alert=True)
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
        # 1. Drain any subprocess output the reader thread parked.
        try:
            while True:
                line = self.q.get_nowait()
                if line.startswith("[exit "):
                    ok = "0]" in line
                    self._append(line, "ok" if ok else "warn")
                    self._end_job(ok)
                    if ok and getattr(self, "_then_clone_chain", False):
                        # git clone/pull succeeded; switch path_var to the
                        # cloned dir and run `graphify update` on it.
                        self._then_clone_chain = False
                        dest = getattr(self, "_post_clone_dest", None)
                        if dest:
                            self.path_var.set(str(dest))
                            self._maybe_link_output(dest)
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

        # 2. If a job is running, refresh the elapsed-time line.
        if self._job_start is not None:
            self._tick_timer()

        # 3. Pick up any model-probe results posted by background threads.
        self._drain_model_probe()

        self.root.after(80, self._poll_output)

    # ----------------------------------------------------- job timer + alerts

    def _begin_job(self, label: str, alert: bool) -> None:
        self._job_start = time.monotonic()
        self._job_label = label
        self._alert_on_done = alert
        self._tick_timer()

    def _tick_timer(self) -> None:
        if self._job_start is None:
            return
        elapsed = time.monotonic() - self._job_start
        m, s = divmod(int(elapsed), 60)
        self._set_status(
            f"{self._job_label}... working - elapsed {m}:{s:02d}"
        )

    def _end_job(self, ok: bool) -> None:
        if self._job_start is None:
            self._set_status("Done." if ok else "Finished with errors.")
            return
        elapsed = time.monotonic() - self._job_start
        m, s = divmod(int(elapsed), 60)
        msg = (
            f"Done in {m}:{s:02d}." if ok
            else f"Finished with errors after {m}:{s:02d}."
        )
        self._set_status(msg)
        self._job_start = None
        if self._alert_on_done:
            self._fire_alert(ok, m, s)
        self._alert_on_done = False

    def _fire_alert(self, ok: bool, m: int, s: int) -> None:
        # Beep through the OS bell; ignored if unavailable.
        try:
            if os.name == "nt":
                import winsound
                winsound.MessageBeep(
                    winsound.MB_OK if ok else winsound.MB_ICONHAND
                )
            else:
                # Cross-platform terminal bell - works in most setups.
                sys.stdout.write("\a")
                sys.stdout.flush()
        except Exception:
            pass
        # Bring the window forward briefly to draw attention.
        try:
            self.root.bell()
            self.root.after(0, lambda: self.root.attributes("-topmost", True))
            self.root.after(
                400, lambda: self.root.attributes("-topmost", False)
            )
        except Exception:
            pass

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
    # Honored by CI: open the window, schedule destruction, exit cleanly.
    if os.environ.get("GRAPHIFY_TEST_AUTOQUIT"):
        root.after(800, root.destroy)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

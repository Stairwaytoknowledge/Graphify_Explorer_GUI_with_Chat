"""Autocomplete popup for the "Ask the graph" query box.

Two sources are merged into a single ranked list:

  1. Static command shapes (`graphify query`, `graphify explain`,
     `graphify path`). These show up when the user starts typing
     something that looks like a CLI invocation, with a short
     description that tells them the equivalent button.

  2. Symbols extracted from the loaded graph: every node label, with
     its source file, file_type, community, and degree. These show
     up the moment the user types a fragment that matches any node.

Scoring is a deterministic small-rule system (prefix > token-prefix
> substring > source-file substring), multi-token aware. No fuzzy
matching in v1; the corpus is small and exact-substring is what
users expect.

Path-with-pipe (Path A|B) smart mode is intentionally OUT of v1.
The popup filters on the whole input string, so when the user is
typing the second symbol of a Path query the matches will go quiet
until they finish the term. By design.

The popup is a Toplevel with overrideredirect=True so it has no
window decorations and can be positioned right below the Entry. All
colors come from the active palette so dark / light / cyberpunk
themes all work without per-mode special cases.
"""

from __future__ import annotations

import re
import tkinter as tk
from tkinter import ttk
from typing import Callable, NamedTuple, Sequence


# ----------------------------------------------------------------- candidates


class Candidate(NamedTuple):
    """A single suggestion in the popup.

    `value` is what gets pasted into the Entry when the user accepts.
    `description` is rendered to the right in dim text.
    `kind` is one of "command" / "symbol" - used only for grouping
    and a small style hint, not for scoring.
    `match_target` is the lowercased text the scorer matches against
    (label for symbols, the value itself for commands).
    `match_source_file` is the lowercased source file (symbols only)
    used for the secondary substring score.
    """
    value: str
    description: str
    kind: str
    match_target: str
    match_source_file: str = ""


# Static command shapes. Always included in the candidate pool.
COMMAND_CANDIDATES: tuple[Candidate, ...] = (
    Candidate(
        value="graphify query",
        description="BFS subgraph traversal (= Query button)",
        kind="command",
        match_target="graphify query",
    ),
    Candidate(
        value="graphify explain",
        description="Print one node's metadata + neighbors (= Explain button)",
        kind="command",
        match_target="graphify explain",
    ),
    Candidate(
        value="graphify path",
        description="Shortest path between two named nodes (= Path A|B button)",
        kind="command",
        match_target="graphify path",
    ),
)


def build_graph_candidates(graph) -> list[Candidate]:
    """Build the symbol candidate list from a networkx graph.

    Called once after `_load_graph_into_view` and cached. Filtering
    on each keystroke is then a single linear pass over this list.
    """
    if graph is None:
        return []
    out: list[Candidate] = []
    for nid, attrs in graph.nodes(data=True):
        # Drop blank labels rather than falling back to the internal
        # node id - exposing internal ids as autocomplete suggestions
        # would be noise (the user can never type one usefully).
        label = (attrs.get("label") or "").strip()
        if not label:
            continue
        source_file = (attrs.get("source_file") or "").replace("\\", "/")
        file_type = str(attrs.get("file_type") or "").strip()
        community = attrs.get("community")
        try:
            degree = graph.degree(nid)
        except Exception:
            degree = 0
        parts: list[str] = []
        if source_file:
            parts.append(source_file)
        if file_type:
            parts.append(file_type)
        if community is not None and str(community) != "":
            parts.append(f"comm {community}")
        parts.append(f"deg {degree}")
        out.append(Candidate(
            value=label,
            description=" · ".join(parts),
            kind="symbol",
            match_target=label.lower(),
            match_source_file=source_file.lower(),
        ))
    return out


# --------------------------------------------------------------------- scorer


# Snake_case / kebab-case / dotted-name / camelCase boundary regex.
# Used to score "token-prefix" matches: typing `auth` should hit
# `verify_auth_token` because `auth` starts the second token.
_TOKEN_BOUNDARY = re.compile(r"[_\-./ ]|(?=[A-Z])")


def _tokens(s: str) -> list[str]:
    if not s:
        return []
    return [t.lower() for t in _TOKEN_BOUNDARY.split(s) if t]


def score(query: str, candidate: Candidate) -> float:
    """Return a score >= 0. Zero means "doesn't match, drop it".

    Multi-token query: every token must contribute > 0 or the whole
    candidate scores zero. This avoids "matched one out of three" noise.

    Per-token rules, taking the max of the applicable ones:
        +5  query is a prefix of the label
        +3  query is a prefix of any sub-token (snake / camel boundary)
        +1  query is a substring of the label
        +0.5 query is a substring of the source file (symbols only)
    """
    if not query:
        return 0.0
    q = query.strip().lower()
    if not q:
        return 0.0
    target = candidate.match_target
    src = candidate.match_source_file
    target_tokens = _tokens(candidate.value)

    total = 0.0
    for tok in q.split():
        per = 0.0
        if target.startswith(tok):
            per = max(per, 5.0)
        if any(t.startswith(tok) for t in target_tokens):
            per = max(per, 3.0)
        if tok in target:
            per = max(per, 1.0)
        if src and tok in src:
            per = max(per, 0.5)
        if per == 0.0:
            return 0.0  # all tokens must contribute
        total += per
    # Tiny tiebreaker: shorter labels win at equal score, so an exact
    # `unet_3d_blocks` ranks above `unet_3d_blocks_helper`.
    total -= len(candidate.value) / 10000.0
    return total


def rank(
    query: str,
    candidates: Sequence[Candidate],
    top_n: int = 15,
) -> list[Candidate]:
    """Score, filter, sort, return at most `top_n` candidates."""
    if not query:
        return []
    scored: list[tuple[float, Candidate]] = []
    for c in candidates:
        s = score(query, c)
        if s > 0.0:
            scored.append((s, c))
    scored.sort(key=lambda x: -x[0])
    return [c for _, c in scored[:top_n]]


# ----------------------------------------------------------------------- popup


class AutocompletePopup:
    """Borderless suggestion popup attached to a tk.Entry widget.

    Usage:
        popup = AutocompletePopup(
            root,
            entry,
            candidate_provider=lambda: COMMAND_CANDIDATES + graph_cands,
            on_select=lambda c: query_var.set(c.value),
        )
        popup.apply_palette(PALETTE)   # call again on theme switches

    The popup is created once and reused (show/hide via wm_withdraw)
    so we don't pay window-creation cost on every keystroke.
    """

    DEBOUNCE_MS = 120
    MAX_VISIBLE_ROWS = 12

    def __init__(
        self,
        master: tk.Misc,
        entry: tk.Widget,
        candidate_provider: Callable[[], Sequence[Candidate]],
        on_select: Callable[[Candidate], None],
    ) -> None:
        self.master = master
        self.entry = entry
        self.candidate_provider = candidate_provider
        self.on_select = on_select
        self._top: tk.Toplevel | None = None
        self._tree: ttk.Treeview | None = None
        self._items: list[Candidate] = []
        self._after_id: str | None = None
        self._palette: dict[str, str] | None = None
        self._suppress_show = False  # set true while we programmatically
                                     # update query_var to avoid re-popup

        # Bindings on the entry. KeyRelease is the trigger; navigation
        # keys steal focus into the popup.
        entry.bind("<KeyRelease>", self._on_key_release, add="+")
        entry.bind("<Down>", self._on_down, add="+")
        entry.bind("<Up>", self._on_up_in_entry, add="+")
        entry.bind("<Return>", self._on_return, add="+")
        entry.bind("<Escape>", self._on_escape, add="+")
        entry.bind("<FocusOut>", self._on_focus_out, add="+")

    # ---- lifecycle -------------------------------------------------------

    def _ensure_top(self) -> None:
        if self._top is not None:
            return
        top = tk.Toplevel(self.master)
        top.wm_overrideredirect(True)
        try:
            top.wm_attributes("-topmost", True)
        except tk.TclError:
            pass
        top.withdraw()
        cols = ("desc",)
        tree = ttk.Treeview(
            top,
            columns=cols,
            show="tree headings",
            height=self.MAX_VISIBLE_ROWS,
            selectmode="browse",
            style="Autocomplete.Treeview",
        )
        tree.heading("#0", text="")
        tree.heading("desc", text="")
        tree.column("#0", width=240, anchor="w", stretch=True)
        tree.column("desc", width=420, anchor="w", stretch=True)
        tree.pack(fill="both", expand=True)
        tree.bind("<Return>", self._on_tree_return, add="+")
        tree.bind("<Double-Button-1>", self._on_tree_return, add="+")
        tree.bind("<Escape>", self._on_escape, add="+")
        # Up arrow at the top of the list pops focus back into the Entry.
        tree.bind("<KeyPress-Up>", self._on_tree_up, add="+")
        self._top = top
        self._tree = tree
        if self._palette:
            self.apply_palette(self._palette)

    def _position(self) -> None:
        if not self._top:
            return
        self.entry.update_idletasks()
        x = self.entry.winfo_rootx()
        y = self.entry.winfo_rooty() + self.entry.winfo_height() + 2
        w = max(360, self.entry.winfo_width())
        h = min(
            self.MAX_VISIBLE_ROWS, max(1, len(self._items)),
        ) * 22 + 6  # rough row height; Treeview scrollbar handles overflow
        self._top.wm_geometry(f"{w}x{h}+{x}+{y}")

    def apply_palette(self, palette: dict[str, str]) -> None:
        """Re-color popup widgets to match the active theme.

        Called once after construction and whenever the GUI theme
        changes. Safe to call before _ensure_top runs - we cache the
        palette and apply on next show.
        """
        self._palette = palette
        if not self._top or not self._tree:
            return
        try:
            self._top.configure(bg=palette["panel"])
        except tk.TclError:
            pass
        style = ttk.Style()
        style.configure(
            "Autocomplete.Treeview",
            background=palette["panel_alt"],
            foreground=palette["fg"],
            fieldbackground=palette["panel_alt"],
            borderwidth=0,
            rowheight=22,
        )
        style.map(
            "Autocomplete.Treeview",
            background=[("selected", palette["accent"])],
            foreground=[("selected", palette["on_accent"])],
        )

    # ---- show / hide -----------------------------------------------------

    def hide(self) -> None:
        if self._top is not None:
            try:
                self._top.withdraw()
            except tk.TclError:
                pass

    def is_visible(self) -> bool:
        if self._top is None:
            return False
        try:
            return bool(self._top.winfo_viewable())
        except tk.TclError:
            return False

    def _request_show(self) -> None:
        # Debounce: cancel any pending refresh, schedule a new one.
        if self._after_id is not None:
            try:
                self.master.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        self._after_id = self.master.after(self.DEBOUNCE_MS, self._refresh_now)

    def _refresh_now(self) -> None:
        self._after_id = None
        query = self.entry.get() if isinstance(self.entry, (tk.Entry, ttk.Entry)) \
            else getattr(self.entry, "get", lambda: "")()
        query = (query or "").strip()
        if not query:
            self.hide()
            return
        try:
            cands = self.candidate_provider()
        except Exception:
            cands = []
        items = rank(query, cands, top_n=self.MAX_VISIBLE_ROWS * 2)
        if not items:
            self.hide()
            return
        self._items = items[: self.MAX_VISIBLE_ROWS * 2]
        self._ensure_top()
        self._populate()
        self._position()
        try:
            self._top.deiconify()
        except tk.TclError:
            pass

    def _populate(self) -> None:
        if not self._tree:
            return
        for child in self._tree.get_children():
            self._tree.delete(child)
        for i, c in enumerate(self._items):
            tag = c.kind  # "command" or "symbol" - lets us style if we want
            self._tree.insert(
                "", "end",
                iid=f"row-{i}",
                text=c.value,
                values=(c.description,),
                tags=(tag,),
            )

    # ---- key handlers ----------------------------------------------------

    def _on_key_release(self, event) -> str | None:
        if self._suppress_show:
            return None
        # Navigation keys are handled separately - don't trigger refresh.
        if event.keysym in ("Up", "Down", "Return", "Escape", "Tab",
                            "Shift_L", "Shift_R", "Control_L", "Control_R",
                            "Alt_L", "Alt_R", "Meta_L", "Meta_R"):
            return None
        self._request_show()
        return None

    def _on_down(self, _event) -> str:
        if not self.is_visible() or not self._items:
            return "break"
        try:
            self._tree.focus_set()
            first = "row-0"
            self._tree.selection_set(first)
            self._tree.focus(first)
        except tk.TclError:
            pass
        return "break"

    def _on_up_in_entry(self, _event) -> str | None:
        # Pre-empts default Up behavior in Entry only when popup is up.
        if self.is_visible() and self._items:
            try:
                self._tree.focus_set()
                last = f"row-{len(self._items) - 1}"
                self._tree.selection_set(last)
                self._tree.focus(last)
            except tk.TclError:
                pass
            return "break"
        return None

    def _on_tree_up(self, _event) -> str | None:
        # When the user is at row 0 and presses Up again, pop back to entry.
        if not self._tree:
            return None
        sel = self._tree.selection()
        if sel and sel[0] == "row-0":
            self.entry.focus_set()
            return "break"
        return None

    def _on_return(self, _event) -> str | None:
        # On the Entry itself: if the popup is up with one or more items,
        # accept the top-scored item. Otherwise, let Return pass through
        # so the user's Enter still triggers the form's default action.
        if self.is_visible() and self._items:
            self._accept(self._items[0])
            return "break"
        return None

    def _on_tree_return(self, _event) -> str | None:
        if not self._tree:
            return None
        sel = self._tree.selection()
        if not sel:
            return None
        idx = int(sel[0].split("-", 1)[1])
        if 0 <= idx < len(self._items):
            self._accept(self._items[idx])
        return "break"

    def _on_escape(self, _event) -> str | None:
        if self.is_visible():
            self.hide()
            self.entry.focus_set()
            return "break"
        return None

    def _on_focus_out(self, _event) -> None:
        # Defer slightly so a click on the popup itself can register
        # before we dismiss. The click handler will refocus the popup.
        try:
            self.master.after(150, self._maybe_hide_after_focus_loss)
        except Exception:
            self.hide()

    def _maybe_hide_after_focus_loss(self) -> None:
        try:
            focused = self.master.focus_get()
        except (tk.TclError, KeyError):
            focused = None
        # If focus is now anywhere inside the popup, keep it open.
        if self._top and focused is not None:
            w = focused
            while w is not None:
                if w is self._top:
                    return
                try:
                    w = w.master
                except AttributeError:
                    break
        self.hide()

    # ---- accept ----------------------------------------------------------

    def _accept(self, candidate: Candidate) -> None:
        self._suppress_show = True
        try:
            self.on_select(candidate)
        finally:
            # Tk fires KeyRelease for the Return key after on_select; allow
            # the next user-typed keystroke to re-show the popup.
            try:
                self.master.after(0, lambda: setattr(self, "_suppress_show", False))
            except Exception:
                self._suppress_show = False
        self.hide()
        self.entry.focus_set()
        # Position the cursor at end of the inserted value.
        try:
            self.entry.icursor("end")
        except tk.TclError:
            pass

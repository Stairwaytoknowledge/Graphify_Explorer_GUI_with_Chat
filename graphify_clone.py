"""Partial + sparse git clone helpers.

This module is the single place that knows how Graphify Explorer fetches
remote git repositories. Rationale:

* `git clone --filter=blob:none` defers blob downloads until git actually
  needs them. Combined with sparse-checkout, blobs that don't match the
  source-extension allow-list never get pulled at all. On a polyglot
  repo with `node_modules`, build artifacts or large binary assets this
  is the difference between a 5 GB clone and a 50 MB working tree.

* Sparse-checkout uses gitignore-style patterns (non-cone mode) so we
  can filter by extension across every subdirectory.

* Whenever git is too old for those features we fall back to a plain
  shallow clone, no behavior change.

The helpers also own cache management for `~/.graphify/repos/` so the
GUI's "Clear cached repos" button has a single implementation.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, NamedTuple
from urllib.parse import urlparse

# Minimum git versions for the features we use.
_MIN_PARTIAL_CLONE = (2, 19)   # --filter=blob:none
_MIN_SPARSE_CHECKOUT = (2, 25)  # `git sparse-checkout` porcelain command

# Suppress the brief console-window flash that Windows shows when we
# spawn child git.exe processes. 0 on Mac/Linux, no-op.
_NO_CONSOLE_FLAGS = (
    getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
)


# Source-file extensions graphify can parse. Patterns are matched by
# git sparse-checkout (non-cone), so they work at any depth in the tree.
# Keep this list inclusive: a missed file means a missing graph node;
# a wrongly-included file is harmless (graphify just ignores it).
SOURCE_EXTENSIONS: tuple[str, ...] = (
    # python
    "py", "pyi",
    # javascript / typescript
    "js", "jsx", "mjs", "cjs", "ts", "tsx",
    # jvm
    "java", "kt", "kts", "scala", "groovy",
    # go / rust / c-family
    "go", "rs", "c", "h", "cc", "cpp", "cxx", "hpp", "hh", "hxx",
    # .net
    "cs", "fs", "vb",
    # other compiled
    "swift", "m", "mm", "rb", "php", "lua", "pl", "pm",
    # scripting / shell
    "sh", "bash", "zsh", "ps1", "bat", "cmd",
    # functional
    "ex", "exs", "erl", "hrl", "hs", "elm", "ml", "mli",
    # data / config that often holds code-shaped declarations
    "sql", "proto", "graphql", "gql",
)


class GitVersion(NamedTuple):
    major: int
    minor: int
    patch: int

    def supports(self, required: tuple[int, int]) -> bool:
        return (self.major, self.minor) >= required


def detect_git_version() -> GitVersion | None:
    """Return the local git version, or None if git isn't on PATH."""
    if not shutil.which("git"):
        return None
    try:
        r = subprocess.run(
            ["git", "--version"],
            capture_output=True, text=True, timeout=5,
            creationflags=_NO_CONSOLE_FLAGS,
        )
    except OSError:
        return None
    if r.returncode != 0:
        return None
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", r.stdout or "")
    if not m:
        return None
    return GitVersion(
        int(m.group(1)),
        int(m.group(2)),
        int(m.group(3) or 0),
    )


def supports_partial_clone(v: GitVersion | None = None) -> bool:
    v = v if v is not None else detect_git_version()
    return bool(v and v.supports(_MIN_PARTIAL_CLONE))


def supports_sparse_checkout(v: GitVersion | None = None) -> bool:
    v = v if v is not None else detect_git_version()
    return bool(v and v.supports(_MIN_SPARSE_CHECKOUT))


def cache_root() -> Path:
    """Local clone cache. Mirrors graphify's own ~/.graphify/repos layout."""
    return Path.home() / ".graphify" / "repos"


def derive_clone_dest(url: str) -> Path:
    """Pick a deterministic local clone directory for `url`.

    Returns ~/.graphify/repos/<owner>/<repo>. Repeat clones of the same
    URL land in the same place so we can `git pull` rather than
    re-download.
    """
    home = cache_root()
    s = url.strip()
    if s.startswith("git@"):
        m = re.match(r"git@([^:]+):([^/]+)/(.+?)(?:\.git)?$", s)
        if m:
            _host, owner, repo = m.groups()
            return home / owner / repo
    parsed = urlparse(s)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 2:
        owner = parts[-2]
        repo = parts[-1]
        if repo.endswith(".git"):
            repo = repo[:-4]
        return home / owner / repo
    return home / "unknown" / (parsed.netloc or "repo")


def cache_size_bytes(root: Path | None = None) -> int:
    """Total bytes used by the clone cache. Returns 0 if the cache is empty."""
    base = root if root is not None else cache_root()
    if not base.exists():
        return 0
    total = 0
    for dirpath, _dirs, files in os.walk(base):
        for f in files:
            try:
                total += (Path(dirpath) / f).stat().st_size
            except OSError:
                # File disappeared mid-walk, antivirus lock, etc. Skip it.
                continue
    return total


def cache_repo_count(root: Path | None = None) -> int:
    """How many repos live in the cache. <owner>/<repo> dirs at depth 2."""
    base = root if root is not None else cache_root()
    if not base.exists():
        return 0
    count = 0
    try:
        for owner in base.iterdir():
            if not owner.is_dir():
                continue
            for repo in owner.iterdir():
                if repo.is_dir():
                    count += 1
    except OSError:
        return count
    return count


def clear_cache(root: Path | None = None) -> int:
    """Remove the entire clone cache. Returns the number of bytes freed.

    Always safe to call on a missing or partially populated cache.
    """
    base = root if root is not None else cache_root()
    if not base.exists():
        return 0
    freed = cache_size_bytes(base)
    shutil.rmtree(base, ignore_errors=True)
    return freed


def format_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    units = ["KB", "MB", "GB", "TB"]
    val = float(n) / 1024.0
    for unit in units:
        if val < 1024 or unit == units[-1]:
            return f"{val:.1f} {unit}"
        val /= 1024.0
    return f"{n} B"


def build_partial_sparse_commands(
    url: str,
    dest: Path,
    extensions: Iterable[str] = SOURCE_EXTENSIONS,
    git_version: GitVersion | None = None,
) -> list[list[str]]:
    """Return the ordered list of git invocations for a partial+sparse clone.

    The exact commands depend on git version:

    * git >= 2.25 with partial-clone support: a non-cone sparse clone
      that filters blobs and only materializes files matching the
      extension allow-list.
    * git >= 2.19 (filter, no porcelain sparse-checkout): partial clone
      with manual sparse-checkout config.
    * older git: plain shallow clone, full working tree.

    All commands use absolute paths and are safe to pass to
    `subprocess.Popen` without shell expansion.
    """
    v = git_version if git_version is not None else detect_git_version()
    dest_str = str(dest)

    has_partial = supports_partial_clone(v)
    has_sparse = supports_sparse_checkout(v)

    if not has_partial:
        # Old git: best we can do is a shallow clone.
        return [["git", "clone", "--depth", "1", url, dest_str]]

    patterns = [f"*.{ext}" for ext in extensions]

    # Pre-clone setup: --no-checkout so we can configure sparse-checkout
    # before any blobs are materialized.
    cmds: list[list[str]] = [
        [
            "git", "clone",
            "--filter=blob:none",
            "--depth", "1",
            "--no-checkout",
            url, dest_str,
        ],
    ]

    if has_sparse:
        # `git sparse-checkout init/set` is the modern porcelain command.
        # `--no-cone` lets us use gitignore-style patterns including `*.ext`
        # which match files at any depth.
        cmds.append(
            ["git", "-C", dest_str, "sparse-checkout", "init", "--no-cone"]
        )
        cmds.append(
            ["git", "-C", dest_str, "sparse-checkout", "set", *patterns]
        )
    else:
        # Older git supports sparse-checkout via config + writing the
        # patterns into .git/info/sparse-checkout. We do the config part
        # here; the file write is handled by the GUI before `git checkout`
        # runs (see write_sparse_patterns).
        cmds.append(
            ["git", "-C", dest_str, "config", "core.sparseCheckout", "true"]
        )

    cmds.append(["git", "-C", dest_str, "checkout"])
    return cmds


def write_sparse_patterns(
    dest: Path,
    extensions: Iterable[str] = SOURCE_EXTENSIONS,
) -> None:
    """Write the sparse-checkout pattern file for older git versions.

    Newer git uses the porcelain `sparse-checkout set` command which
    handles this internally. Only call this when the porcelain isn't
    available (git < 2.25).
    """
    info_dir = dest / ".git" / "info"
    info_dir.mkdir(parents=True, exist_ok=True)
    patterns = [f"*.{ext}" for ext in extensions]
    (info_dir / "sparse-checkout").write_text(
        "\n".join(patterns) + "\n",
        encoding="utf-8",
    )


def is_partial_clone(dest: Path) -> bool:
    """Detect whether `dest` was created with --filter=blob:none.

    Used by the pull path: a previously-full clone won't suddenly become
    partial if we add the flag, so we treat the two cases differently
    only for reporting purposes.
    """
    config = dest / ".git" / "config"
    if not config.exists():
        return False
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "remote.origin.partialclonefilter" in text or "filter = blob:none" in text


def build_pull_command(dest: Path) -> list[str]:
    """Cheap fast-forward pull for an existing clone."""
    return ["git", "-C", str(dest), "pull", "--ff-only"]

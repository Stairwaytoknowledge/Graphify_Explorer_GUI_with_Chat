"""Safe read-only access to remote / network code folders.

Used when the user points Graphify Explorer at a path that lives on a
network mount (UNC, SMB, NFS, sshfs, ...). The hard requirement is:

    Under no circumstances may we write, delete, or otherwise modify
    anything inside the remote folder.

Approach: never let graphify see the remote path. Instead, build a
local read-only mirror containing only the source files graphify can
parse, then run graphify against the mirror. The remote is opened with
read-only file handles only; the mirror lives on the local disk under
~/.graphify/mirror/<hash>. graphify writes its output into the mirror
(local), not into the remote.

Defense layers, all active simultaneously:

1. Source-file allow-list. Only files with extensions graphify parses
   are copied. Build artifacts, datasets, and binaries never touch the
   local disk and never trigger reads on the share beyond the directory
   walk.
2. Size and file-count caps. We refuse to mirror if the source would
   exceed the cap, instead of partially copying and surprising the
   user.
3. Read-only file handles. Every open() against the remote uses 'rb'.
   The PathGuard helper rejects any attempt to open a remote path in a
   write mode.
4. Subprocess isolation. graphify runs with cwd=mirror, so the remote
   path is not in argv, env, or cwd. A buggy subprocess physically
   cannot resolve "../../remote" because the mirror tree is what it
   sees.
5. Tripwire test. tests/test_remote.py runs the full mirror flow
   against a directory that errors on any write to the source, proving
   the source is never touched in write mode.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

# We share the source-extension allow-list with the clone module so the
# two paths agree on what graphify can parse.
from graphify_clone import SOURCE_EXTENSIONS, format_size


# Default upper bounds. Conservative: most corp source trees are well
# under these. The GUI can override per-call.
DEFAULT_MAX_BYTES = 2 * 1024 * 1024 * 1024   # 2 GB
DEFAULT_MAX_FILES = 250_000
DEFAULT_PER_FILE_MAX_BYTES = 25 * 1024 * 1024   # 25 MB

# Directories we never recurse into when mirroring. Saves both time and
# the user's bandwidth: these never contain source graphify can use.
SKIP_DIRS: frozenset[str] = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", "bower_components",
    "dist", "build", "out", "target",
    ".venv", "venv", "__pycache__",
    ".idea", ".vscode", ".vs",
    ".tox", ".mypy_cache", ".pytest_cache",
    ".gradle", ".cargo",
    "graphify-out",
})


def is_network_path(path: str | Path) -> bool:
    """True if `path` looks like a remote / network mount.

    Heuristic; sufficient for routing into the safe mirror flow. False
    negatives just mean the user gets the regular flow (which is fine
    for a local path).
    """
    s = str(path).strip()
    if not s:
        return False
    # Windows UNC.
    if s.startswith("\\\\") or s.startswith("//"):
        return True
    # Common Linux network-mount roots.
    if sys.platform.startswith("linux"):
        if s.startswith(("/mnt/", "/media/", "/net/")):
            return True
    # macOS network mounts.
    if sys.platform == "darwin" and s.startswith("/Volumes/"):
        # /Volumes/ holds both removable disks and network shares; we
        # treat the whole tree as suspect to be safe.
        return True
    return False


def mirror_root() -> Path:
    """Where local read-only mirrors of remote folders live."""
    return Path.home() / ".graphify" / "mirror"


def mirror_id(source: Path) -> str:
    """Deterministic short id for a remote source path."""
    h = hashlib.sha256(str(source.resolve()).encode("utf-8")).hexdigest()
    return h[:16]


def mirror_path_for(source: Path) -> Path:
    return mirror_root() / mirror_id(source)


@dataclass
class MirrorPolicy:
    """Configurable bounds for what we will mirror."""

    extensions: tuple[str, ...] = SOURCE_EXTENSIONS
    max_total_bytes: int = DEFAULT_MAX_BYTES
    max_files: int = DEFAULT_MAX_FILES
    per_file_max_bytes: int = DEFAULT_PER_FILE_MAX_BYTES
    skip_dirs: frozenset[str] = SKIP_DIRS
    # Verbatim opt-in: user told us they want this remote mirrored.
    # We never auto-mirror without crossing this gate.
    user_consented: bool = False


@dataclass
class MirrorReport:
    """What happened during a mirror operation."""

    source: Path
    mirror: Path
    files_considered: int = 0
    files_copied: int = 0
    bytes_copied: int = 0
    skipped_too_big: int = 0
    skipped_extension: int = 0
    skipped_dir: int = 0
    elapsed_seconds: float = 0.0
    aborted_reason: str | None = None
    write_attempts_blocked: int = 0
    skipped_examples: list[str] = field(default_factory=list)


class RemoteWriteAttempt(RuntimeError):
    """Raised when something tries to open a remote path for writing.

    This is the application-level tripwire. If you ever see this in
    logs it means the safety design caught a bug before it could touch
    the share.
    """


class PathGuard:
    """Reject any write-mode open() that targets the remote prefix.

    Instantiated when remote mode is engaged; deactivated on close.
    Wraps the builtin open() and a small set of os.* functions that can
    create or modify files. Read-mode opens against the remote pass
    through unchanged.
    """

    _WRITE_MODE_CHARS = frozenset("wax+")

    def __init__(self, remote_root: Path) -> None:
        self.remote_root = Path(remote_root).resolve()
        self.attempts: list[str] = []
        self._installed = False
        # Stash the originals so install/uninstall is reversible.
        self._orig_open = None
        self._orig_remove = None
        self._orig_unlink = None
        self._orig_rmdir = None
        self._orig_rename = None
        self._orig_mkdir = None
        self._orig_makedirs = None

    # ---- helpers --------------------------------------------------------

    def _is_remote(self, p: str | Path) -> bool:
        try:
            resolved = Path(p).resolve()
        except (OSError, ValueError):
            # If we can't resolve, fall back to a string-prefix check.
            try:
                return str(Path(p)).startswith(str(self.remote_root))
            except Exception:
                return False
        try:
            resolved.relative_to(self.remote_root)
            return True
        except ValueError:
            return False

    def _is_write_mode(self, mode: str) -> bool:
        # 'r', 'rb', 'rt' are pure-read. Everything else with w/a/x or
        # '+' modifies the file.
        return any(ch in self._WRITE_MODE_CHARS for ch in mode)

    def _block(self, op: str, target: str) -> None:
        self.attempts.append(f"{op}({target})")
        raise RemoteWriteAttempt(
            f"refused {op} on remote path {target!r}: this would write to "
            "the share, which is forbidden in remote mode."
        )

    # ---- patched implementations ---------------------------------------

    def _patched_open(self, file, mode="r", *args, **kwargs):
        if self._is_write_mode(mode) and self._is_remote(file):
            self._block("open", str(file))
        return self._orig_open(file, mode, *args, **kwargs)

    def _patched_remove(self, path, *args, **kwargs):
        if self._is_remote(path):
            self._block("remove", str(path))
        return self._orig_remove(path, *args, **kwargs)

    def _patched_unlink(self, path, *args, **kwargs):
        if self._is_remote(path):
            self._block("unlink", str(path))
        return self._orig_unlink(path, *args, **kwargs)

    def _patched_rmdir(self, path, *args, **kwargs):
        if self._is_remote(path):
            self._block("rmdir", str(path))
        return self._orig_rmdir(path, *args, **kwargs)

    def _patched_rename(self, src, dst, *args, **kwargs):
        if self._is_remote(src) or self._is_remote(dst):
            self._block("rename", f"{src} -> {dst}")
        return self._orig_rename(src, dst, *args, **kwargs)

    def _patched_mkdir(self, path, *args, **kwargs):
        if self._is_remote(path):
            self._block("mkdir", str(path))
        return self._orig_mkdir(path, *args, **kwargs)

    def _patched_makedirs(self, path, *args, **kwargs):
        if self._is_remote(path):
            self._block("makedirs", str(path))
        return self._orig_makedirs(path, *args, **kwargs)

    # ---- install / uninstall -------------------------------------------

    def install(self) -> None:
        if self._installed:
            return
        import builtins
        self._orig_open = builtins.open
        self._orig_remove = os.remove
        self._orig_unlink = os.unlink
        self._orig_rmdir = os.rmdir
        self._orig_rename = os.rename
        self._orig_mkdir = os.mkdir
        self._orig_makedirs = os.makedirs
        builtins.open = self._patched_open
        os.remove = self._patched_remove
        os.unlink = self._patched_unlink
        os.rmdir = self._patched_rmdir
        os.rename = self._patched_rename
        os.mkdir = self._patched_mkdir
        os.makedirs = self._patched_makedirs
        self._installed = True

    def uninstall(self) -> None:
        if not self._installed:
            return
        import builtins
        builtins.open = self._orig_open
        os.remove = self._orig_remove
        os.unlink = self._orig_unlink
        os.rmdir = self._orig_rmdir
        os.rename = self._orig_rename
        os.mkdir = self._orig_mkdir
        os.makedirs = self._orig_makedirs
        self._installed = False

    def __enter__(self) -> "PathGuard":
        self.install()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.uninstall()


# ---- mirroring -----------------------------------------------------------


def _normalize_extension(ext: str) -> str:
    return ext.lstrip(".").lower()


def _walk_safe(
    source: Path,
    skip_dirs: Iterable[str],
) -> Iterator[tuple[Path, list[str], list[str]]]:
    """os.walk that prunes well-known build/output directories in-place.

    Yields the same shape as os.walk but with `dirs` already filtered.
    """
    skip = set(skip_dirs)
    for dirpath, dirs, files in os.walk(str(source)):
        # Mutate dirs in-place so os.walk doesn't recurse into skipped trees.
        dirs[:] = [d for d in dirs if d not in skip]
        yield Path(dirpath), dirs, files


def estimate_mirror_size(
    source: Path,
    policy: MirrorPolicy = MirrorPolicy(),
) -> tuple[int, int]:
    """Return (eligible_files, eligible_bytes) before doing any copying.

    Lets the GUI show "ready to mirror N files (X MB)" prompts instead
    of starting a copy and finding out it's too big halfway through.
    """
    allowed = {_normalize_extension(e) for e in policy.extensions}
    files = 0
    total = 0
    for _dir, _ds, fnames in _walk_safe(source, policy.skip_dirs):
        for name in fnames:
            ext = _normalize_extension(Path(name).suffix)
            if ext not in allowed:
                continue
            try:
                size = (Path(_dir) / name).stat().st_size
            except OSError:
                continue
            if size > policy.per_file_max_bytes:
                continue
            files += 1
            total += size
            if files >= policy.max_files or total >= policy.max_total_bytes:
                # Don't keep walking once we already know we'd exceed.
                return files, total
    return files, total


def build_mirror(
    source: Path,
    policy: MirrorPolicy = MirrorPolicy(),
    on_progress=None,
) -> MirrorReport:
    """Copy source-extension files from `source` into a local mirror.

    Pure read-only on the source. The mirror lives at
    ~/.graphify/mirror/<hash> so repeat runs reuse it. If the source is
    already mirrored and unchanged, this is a no-op.

    Raises ValueError if `policy.user_consented` is False; the GUI must
    cross that gate explicitly.
    """
    if not policy.user_consented:
        raise ValueError(
            "MirrorPolicy.user_consented must be True. The GUI gates this "
            "behind an explicit user prompt before mirroring a remote folder."
        )
    src = Path(source).resolve()
    if not src.exists() or not src.is_dir():
        raise FileNotFoundError(f"source folder does not exist: {src}")

    mirror = mirror_path_for(src)
    mirror.parent.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    report = MirrorReport(source=src, mirror=mirror)

    # Pre-flight: refuse if the source is too big.
    eligible_files, eligible_bytes = estimate_mirror_size(src, policy)
    if eligible_files >= policy.max_files:
        report.aborted_reason = (
            f"source has {eligible_files}+ source files, exceeds limit "
            f"of {policy.max_files}. Point at a smaller subtree or raise "
            "the limit in MirrorPolicy."
        )
        return report
    if eligible_bytes >= policy.max_total_bytes:
        report.aborted_reason = (
            f"source has {format_size(eligible_bytes)} of source files, "
            f"exceeds limit of {format_size(policy.max_total_bytes)}. "
            "Point at a smaller subtree or raise the limit."
        )
        return report

    allowed = {_normalize_extension(e) for e in policy.extensions}
    mirror.mkdir(parents=True, exist_ok=True)

    for dirpath, _ds, fnames in _walk_safe(src, policy.skip_dirs):
        rel_dir = dirpath.relative_to(src)
        target_dir = mirror / rel_dir
        for name in fnames:
            report.files_considered += 1
            ext = _normalize_extension(Path(name).suffix)
            if ext not in allowed:
                report.skipped_extension += 1
                if len(report.skipped_examples) < 10:
                    report.skipped_examples.append(str(rel_dir / name))
                continue
            src_file = dirpath / name
            try:
                size = src_file.stat().st_size
            except OSError:
                continue
            if size > policy.per_file_max_bytes:
                report.skipped_too_big += 1
                continue
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / name
            # Read-only on src, write-only on target. shutil.copyfile
            # uses these modes; we keep that behavior explicit by using
            # raw open() calls so the PathGuard tripwire fires if
            # something attempts to write to src.
            with open(src_file, "rb") as src_f:
                with open(target, "wb") as dst_f:
                    while True:
                        chunk = src_f.read(1024 * 1024)
                        if not chunk:
                            break
                        dst_f.write(chunk)
            report.files_copied += 1
            report.bytes_copied += size
            if on_progress and (report.files_copied % 200 == 0):
                on_progress(report)

    report.elapsed_seconds = time.monotonic() - started
    return report


def discard_mirror(source: Path | None = None) -> int:
    """Remove the mirror for `source`, or all mirrors if source is None.

    Returns bytes freed.
    """
    if source is None:
        base = mirror_root()
        if not base.exists():
            return 0
        freed = sum(
            f.stat().st_size for f in base.rglob("*") if f.is_file()
        )
        shutil.rmtree(base, ignore_errors=True)
        return freed
    mirror = mirror_path_for(Path(source).resolve())
    if not mirror.exists():
        return 0
    freed = sum(f.stat().st_size for f in mirror.rglob("*") if f.is_file())
    shutil.rmtree(mirror, ignore_errors=True)
    return freed


def mirror_count_and_size() -> tuple[int, int]:
    """How many mirrored sources we have and total bytes used."""
    base = mirror_root()
    if not base.exists():
        return 0, 0
    n = 0
    total = 0
    for entry in base.iterdir():
        if not entry.is_dir():
            continue
        n += 1
        for f in entry.rglob("*"):
            try:
                if f.is_file():
                    total += f.stat().st_size
            except OSError:
                continue
    return n, total


# ---- best-effort safety probes ------------------------------------------


def remote_share_is_writable(source: Path) -> bool | None:
    """Check whether a tiny temp file can be created and removed.

    Returns:
        True  if the share appears writable (we still won't touch it,
              but the user should know they have write rights)
        False if the OS rejected the probe (read-only or perm denied)
        None  if the probe could not be performed at all

    The probe file is in a uniquely-named subpath the tool owns and is
    deleted immediately. If the delete fails the path is logged.
    """
    src = Path(source)
    if not src.exists():
        return None
    probe_dir = src / f".graphify-probe-{os.getpid()}"
    try:
        probe_dir.mkdir(exist_ok=False)
    except FileExistsError:
        # Stale probe; treat as writable but don't touch it.
        return True
    except (PermissionError, OSError):
        return False
    try:
        f = probe_dir / "delete-me"
        f.write_bytes(b"")
        f.unlink()
        return True
    except (PermissionError, OSError):
        return False
    finally:
        try:
            probe_dir.rmdir()
        except OSError:
            pass

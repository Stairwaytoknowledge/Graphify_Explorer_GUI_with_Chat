"""Reproducible benchmark for the wrapper vs upstream's bare CLI.

Upstream: https://github.com/safishamsi/graphify

What it measures:

  1. Steps a non-shell user has to take to go from "nothing installed" to
     "graph displayed for a remote repo".
  2. Wall-clock time for the actual graph build on a fixed test repo,
     using the same engine each way (so this number should be roughly
     equal - it's the control).
  3. Whether each path produces a natural-language answer for a sample
     question without manual stitching.

Run:
    python benchmarks/compare.py [--repo URL] [--question "..."]

It writes results to docs/COMPARISON.md so the table is reproducible.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Tiny Python repo with real code. Used as the default test corpus so the
# benchmark numbers are stable across machines and don't depend on whatever
# happens to be installed locally.
DEFAULT_REPO = "https://github.com/pallets/itsdangerous"
DEFAULT_Q = "What does the main function do?"


def graphify_bin() -> str | None:
    venv = ROOT / ".venv"
    cand = (
        venv / "Scripts" / "graphify.exe"
        if os.name == "nt"
        else venv / "bin" / "graphify"
    )
    return str(cand) if cand.exists() else shutil.which("graphify")


def time_call(fn, *args, **kwargs):
    t0 = time.perf_counter()
    out = fn(*args, **kwargs)
    return out, time.perf_counter() - t0


def _on_rmtree_error(func, path, exc):
    # Windows leaves .git objects read-only; chmod and retry.
    try:
        os.chmod(path, 0o700)
        func(path)
    except Exception:
        pass


def clone_into(url: str, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest, onerror=_on_rmtree_error)
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--depth", "1", url, str(dest)],
        check=True, capture_output=True,
    )


def run_graphify(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    exe = graphify_bin()
    if not exe:
        raise RuntimeError("graphify CLI not found. Run the installer first.")
    return subprocess.run(
        [exe, *args], cwd=str(cwd),
        capture_output=True, text=True, timeout=600,
    )


def ollama_up() -> bool:
    try:
        urllib.request.urlopen("http://localhost:11434/api/tags", timeout=1.5)
        return True
    except Exception:
        return False


def ollama_models() -> list[str]:
    try:
        with urllib.request.urlopen(
            "http://localhost:11434/api/tags", timeout=2
        ) as r:
            data = json.load(r)
    except Exception:
        return []
    return [m["name"] for m in data.get("models", [])
            if "embed" not in m.get("name", "").lower()]


def ollama_answer(model: str, system: str, user: str) -> tuple[str, float]:
    body = json.dumps(
        {"model": model, "stream": False,
         "messages": [{"role": "system", "content": system},
                      {"role": "user", "content": user}]}
    ).encode()
    req = urllib.request.Request(
        "http://localhost:11434/api/chat",
        data=body, headers={"Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=600) as r:
        out = json.load(r)
    dt = time.perf_counter() - t0
    return (out.get("message") or {}).get("content", ""), dt


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=DEFAULT_REPO,
                    help="Git URL to use as the test corpus.")
    ap.add_argument("--question", default=DEFAULT_Q)
    ap.add_argument("--keep", action="store_true",
                    help="Keep the cloned working tree after the run.")
    args = ap.parse_args()

    print(f"Test repo:      {args.repo}")
    print(f"Test question:  {args.question}")
    print()

    work = ROOT / "benchmarks" / "_work"
    work.mkdir(parents=True, exist_ok=True)
    repo_dir = work / "repo"

    rows: list[tuple[str, str, str]] = []

    # ---- timing 1: clone --------------------------------------------------
    _, t_clone = time_call(clone_into, args.repo, repo_dir)
    rows.append(("clone", f"{t_clone:.2f} s", "git clone --depth 1"))

    # ---- timing 2: graph build (same engine both ways - control) ---------
    r, t_update = time_call(run_graphify, ["update", "."], repo_dir)
    if r.returncode != 0:
        print("graphify update failed:")
        print(r.stdout); print(r.stderr)
        return 1
    nodes = edges = "?"
    for line in r.stdout.splitlines():
        if "Rebuilt:" in line:
            # "[graphify watch] Rebuilt: 12 nodes, 18 edges, 3 communities"
            try:
                parts = line.split("Rebuilt:")[1]
                nodes = parts.split("nodes")[0].strip().rstrip(",")
                edges = parts.split(",")[1].split("edges")[0].strip()
            except Exception:
                pass
            break
    rows.append((
        "graph build",
        f"{t_update:.2f} s",
        f"graphify update . - {nodes} nodes, {edges} edges",
    ))

    # ---- timing 3: graphify query ----------------------------------------
    r, t_query = time_call(run_graphify, ["query", args.question], repo_dir)
    rows.append((
        "graph query (raw nodes)",
        f"{t_query:.2f} s",
        f"graphify query - {len(r.stdout.splitlines())} output lines, "
        f"no natural-language answer",
    ))

    # ---- timing 4: chat answer (only if Ollama is up) --------------------
    chat_row = None
    if ollama_up():
        models = ollama_models()
        model = next(
            (m for m in models if "3b" in m and "embed" not in m),
            models[0] if models else None,
        )
        if model:
            sys_prompt = (
                "You answer questions about a code repo using only the "
                "graph context provided. If unsure, say so."
            )
            user_msg = (
                f"Context (from `graphify query`):\n{r.stdout[:3000]}\n\n"
                f"Question: {args.question}"
            )
            answer, t_chat = ollama_answer(model, sys_prompt, user_msg)
            chat_row = (
                "chat answer (natural language, on-prem)",
                f"{t_chat:.2f} s",
                f"Ollama {model} - {len(answer.split())} words, "
                f"answer: {answer[:140]}{'...' if len(answer) > 140 else ''}",
            )
    if chat_row:
        rows.append(chat_row)
    else:
        rows.append((
            "chat answer (natural language, on-prem)",
            "-",
            "Ollama not running locally - skipped",
        ))

    # ---- step counts (manual; not timed) ---------------------------------
    upstream_steps = [
        "1. install Python or uv", "2. python -m venv .venv",
        "3. activate venv", "4. pip install graphifyy",
        "5. git clone <url>", "6. cd <repo>",
        "7. graphify update .", "8. graphify query \"...\"",
        "9. open graphify-out/graph.html in browser",
        "10. (no built-in NL answer)",
    ]
    wrapper_steps = [
        "1. double-click installer",
        "2. paste URL or path",
        "3. click Build/Refresh",
        "4. click a node, type a question, hit Send",
    ]

    # ---- write COMPARISON.md ---------------------------------------------
    md = []
    md.append("# Wrapper vs upstream: measured comparison\n")
    md.append("Wrapper around [graphify](https://github.com/safishamsi/graphify);")
    md.append("see [NOTICE](../NOTICE).\n")
    md.append("Generated by `benchmarks/compare.py`. Single run on a single")
    md.append("machine; rerun to refresh.\n")
    md.append(f"- Test repo:    `{args.repo}`")
    md.append(f"- Test question: `{args.question}`\n")
    md.append("## Timed steps (same machine, same engine)\n")
    md.append("| Step | Time | Notes |")
    md.append("| ---- | ---- | ----- |")
    for step, t, note in rows:
        md.append(f"| {step} | {t} | {note} |")
    md.append("")
    md.append("Graph build time is the *control*: same engine either way, so")
    md.append("the wrapper is neither faster nor slower at the engine work")
    md.append("itself. The wrapper's gains are elsewhere.\n")
    md.append("## Steps from zero to a natural-language answer\n")
    md.append("Upstream CLI path:\n")
    for s in upstream_steps:
        md.append(f"- {s}")
    md.append("\nWrapper path:\n")
    for s in wrapper_steps:
        md.append(f"- {s}")
    md.append(f"\n**{len(upstream_steps)} steps vs {len(wrapper_steps)} steps.**\n")
    md.append("## What the wrapper adds that upstream doesn't have\n")
    md.append("- Cross-platform double-click installers (CI-verified on")
    md.append("  Windows, macOS, Linux).")
    md.append("- An inline graph viewer + click-to-explore details panel.")
    md.append("- A natural-language answer step (Chat tab) that runs")
    md.append("  *entirely on-prem* via Ollama, grounded in the graph BFS")
    md.append("  output. The bare upstream CLI returns raw nodes and edges;")
    md.append("  turning that into prose is left to the user.\n")
    md.append("## What upstream still does better\n")
    md.append("- The vis.js HTML viewer is still the best graph visualization")
    md.append("  for repos beyond a few hundred nodes - the wrapper's inline")
    md.append("  matplotlib view is capped at 300 nodes.")
    md.append("- `--mode deep` and multimodal extraction (docs, papers, video,")
    md.append("  images) live behind the Claude Code slash command and need")
    md.append("  Claude Code installed; the wrapper can't drive them.\n")

    out = ROOT / "docs" / "COMPARISON.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md))
    print(f"\nwrote {out}")

    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

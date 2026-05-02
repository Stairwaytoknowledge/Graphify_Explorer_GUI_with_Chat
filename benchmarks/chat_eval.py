"""Small evaluation harness for the chat feature.

The graph and BFS results come from upstream graphify
(https://github.com/safishamsi/graphify). The chat layer is what this
wrapper adds and what this eval measures.

Runs a fixed question set against a fixed repo in both Fast and Quality
retrieval modes, with one or more local Ollama models. Reports:

- latency (median, p90)
- citation rate (claims with [bracket] tokens)
- valid-citation rate (citations that map to a real file in the repo)
- refusal rate on out-of-scope questions (should be high)
- spurious-refusal rate on in-scope questions (should be near zero)

Run from repo root:
    python benchmarks/chat_eval.py
    python benchmarks/chat_eval.py --models qwen2.5:7b llama3.2:3b
    python benchmarks/chat_eval.py --modes quality
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Use the wrapper's own helpers so the eval reflects what the GUI does.
import graphify_gui as g  # noqa: E402

# A fixed test repo with real Python source. Eval results are only
# comparable if everyone runs against the same corpus.
TEST_REPO_DIR = Path.home() / ".graphify" / "repos" / "safishamsi" / "graphify"

# (question, in_scope, must_cite_any_of)
# - in_scope=False questions should be refused.
# - must_cite_any_of: optional - if set, an "ideal" answer mentions at least
#   one of these tokens (file or node label).
QUESTIONS: list[tuple[str, bool, list[str]]] = [
    # In-scope
    ("Where is community detection implemented?",     True,  ["cluster"]),
    ("What does build_from_json do?",                 True,  ["build_from_json", "build"]),
    ("Which file handles tree-sitter extraction?",    True,  ["extract"]),
    ("Where is the SHA256 cache implemented?",        True,  ["cache"]),
    ("How are graph reports generated?",              True,  ["report"]),
    ("Which module is responsible for clustering?",   True,  ["cluster"]),
    # Out-of-scope (should refuse)
    ("What does this repo do with kubernetes pods?",  False, []),
    ("How does the websocket reconnection work?",     False, []),
    ("What is the user's bank account number?",       False, []),
    ("Which CI provider does this codebase deploy through?", False, []),
]


def ollama_chat_once(
    model: str, system: str, user: str, options: dict | None = None
) -> tuple[str, float]:
    body = json.dumps(
        {
            "model": model,
            "stream": False,
            "options": options or {"temperature": 0.1, "num_ctx": 8192},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
    ).encode()
    req = urllib.request.Request(
        "http://localhost:11434/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            d = json.load(r)
    except Exception as exc:
        return f"[error: {exc}]", time.perf_counter() - t0
    return (d.get("message") or {}).get("content", ""), time.perf_counter() - t0


def graph_query(repo: Path, question: str) -> str:
    exe = g.graphify_executable()
    if not exe:
        return ""
    try:
        import subprocess
        r = subprocess.run(
            [exe, "query", question, "--budget", "1200"],
            cwd=str(repo), capture_output=True, text=True, timeout=60,
        )
        return r.stdout or ""
    except Exception:
        return ""


def system_prompt() -> str:
    return (
        "You answer questions about ONE specific code repository "
        "using ONLY the context block below.\n\n"
        "Rules:\n"
        "1. If the answer isn't directly supported by the context, "
        "reply exactly: 'I don't know based on the graph.' Do not guess.\n"
        "2. Cite every factual claim with [node_id] or [filename] "
        "from the context.\n"
        "3. Never invent file names, function names, or relations.\n"
        "4. Be concise: 1-3 sentences plus a short bullet list if "
        "multiple items apply."
    )


CITATION_RE = re.compile(r"\[([^\[\]\n]{2,80})\]")
REFUSAL_PHRASES = (
    "i don't know based on the graph",
    "i do not know based on the graph",
)


def is_refusal(text: str) -> bool:
    low = text.lower()
    return any(p in low for p in REFUSAL_PHRASES)


def all_repo_tokens(repo: Path) -> set[str]:
    """Collect file basenames and node ids/labels from graph.json so we can
    check whether a citation is 'real'."""
    tokens: set[str] = set()
    gj = repo / "graphify-out" / "graph.json"
    if not gj.exists():
        return tokens
    data = json.loads(gj.read_text(encoding="utf-8"))
    for node in data.get("nodes", []):
        for k in ("id", "label", "norm_label"):
            v = node.get(k)
            if isinstance(v, str):
                tokens.add(v)
        src = node.get("source_file")
        if isinstance(src, str):
            tokens.add(src)
            tokens.add(Path(src).name)
    return tokens


def score_one(
    answer: str, in_scope: bool, must_cite: list[str], repo_tokens: set[str]
) -> dict:
    refused = is_refusal(answer)
    citations = CITATION_RE.findall(answer)
    real_cites = sum(1 for c in citations if c in repo_tokens
                     or any(c.endswith(t) or t.endswith(c) for t in repo_tokens))
    must_hit = (
        all(any(t.lower() in answer.lower() for t in must_cite)
            for _ in [None])
        if must_cite else True
    )
    return {
        "refused": refused,
        "n_citations": len(citations),
        "n_real_citations": real_cites,
        "must_hit": must_hit,
        "answer_chars": len(answer),
        "answer_words": len(answer.split()),
    }


# ---------------------------------------------------------------- modes


def run_fast_mode(
    app, model: str, repo: Path, question: str
) -> tuple[str, float]:
    """Single-call BFS-only mode."""
    bfs_out = graph_query(repo, question)
    snippets = app._collect_source_snippets(bfs_out, repo)
    ctx_lines: list[str] = []
    if bfs_out:
        ctx_lines.append("=== BFS context ===")
        ctx_lines.append(bfs_out.strip()[:3000])
    if snippets:
        ctx_lines.append("=== Source snippets ===")
        ctx_lines.extend(snippets)
    context = "\n".join(ctx_lines).strip() or "(no context)"
    user = f"Context:\n{context}\n\nQuestion: {question}"
    return ollama_chat_once(model, system_prompt(), user)


def run_quality_mode(
    app, model: str, repo: Path, question: str
) -> tuple[str, float, list[str]]:
    """Two-stage planner + drill + answer, with BFS + embedding union
    (matches the GUI's _chat_worker behaviour)."""
    bfs_out = graph_query(repo, question)
    bfs_picks = app._node_ids_from_bfs(bfs_out)

    toc, valid = app._build_graph_toc(repo)
    rep = app._read_report_excerpt(repo, 1500)
    t_plan = time.perf_counter()
    planner_picks = app._plan_retrieval(
        model=model, question=question, toc=toc,
        report=rep, valid_ids=valid,
    )
    plan_secs = time.perf_counter() - t_plan
    embed_picks = app._embed_topk_node_ids(question, k=8)

    # Union: planner ∪ BFS ∪ embed (dedup, cap 12).
    seen: set[str] = set()
    picked: list[str] = []
    for nid in (*planner_picks, *bfs_picks, *embed_picks):
        if nid not in seen:
            seen.add(nid)
            picked.append(nid)
        if len(picked) >= 12:
            break

    snippets = app._snippets_for_node_ids(picked, repo)
    bfs_snippets = app._collect_source_snippets(bfs_out, repo)

    ctx_lines: list[str] = []
    if snippets:
        ctx_lines.append("=== Picked node sources (planner ∪ BFS) ===")
        ctx_lines.extend(snippets)
    if bfs_out:
        ctx_lines.append("=== BFS context ===")
        ctx_lines.append(bfs_out.strip()[:2500])
    if bfs_snippets and not snippets:
        ctx_lines.append("=== BFS snippets (fallback) ===")
        ctx_lines.extend(bfs_snippets)
    context = "\n".join(ctx_lines).strip() or "(no context)"
    user = f"Context:\n{context}\n\nQuestion: {question}"
    answer, t_ans = ollama_chat_once(model, system_prompt(), user)
    return answer, plan_secs + t_ans, picked


# ---------------------------------------------------------------- runner


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+",
                    default=["qwen2.5:7b", "llama3.2:3b"])
    ap.add_argument("--modes", nargs="+", default=["fast", "quality"])
    ap.add_argument("--repo", type=Path, default=TEST_REPO_DIR)
    ap.add_argument("--out", type=Path,
                    default=ROOT / "docs" / "CHAT_EVAL.md")
    args = ap.parse_args()

    if not args.repo.exists():
        print(f"Test repo not found at {args.repo}", file=sys.stderr)
        print("Either run the GUI once on https://github.com/safishamsi/graphify",
              file=sys.stderr)
        print("or pass --repo /path/to/some/python/repo", file=sys.stderr)
        return 2

    # Ensure we have a graph.json to retrieve from.
    if not (args.repo / "graphify-out" / "graph.json").exists():
        print(f"No graphify-out/ in {args.repo}; running update...")
        import subprocess
        subprocess.run(
            [g.graphify_executable(), "update", "."],
            cwd=str(args.repo), check=True,
        )

    repo_tokens = all_repo_tokens(args.repo)
    print(f"repo_tokens cached: {len(repo_tokens)}")

    # One hidden Tk root for the whole eval, so we don't churn graph load
    # state per question.
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    app = g.GraphifyApp(root)
    app.path_var.set(str(args.repo))
    app._load_graph_into_view()

    rows: list[dict] = []
    try:
        for model in args.models:
            for mode in args.modes:
                print(f"\n=== {model} / {mode} ===", flush=True)
                for q, in_scope, must in QUESTIONS:
                    t0 = time.perf_counter()
                    if mode == "fast":
                        answer, _ = run_fast_mode(app, model, args.repo, q)
                        picked = []
                    else:
                        answer, _, picked = run_quality_mode(
                            app, model, args.repo, q
                        )
                    latency = time.perf_counter() - t0
                    s = score_one(answer, in_scope, must, repo_tokens)
                    row = {
                        "model": model,
                        "mode": mode,
                        "question": q,
                        "in_scope": in_scope,
                        "latency_s": round(latency, 2),
                        "picked_n": len(picked),
                        **s,
                        "answer_first_120": answer.replace("\n", " ")[:120],
                    }
                    rows.append(row)
                    print(
                        f"  [{'inS' if in_scope else 'OOS'}] "
                        f"{latency:5.1f}s "
                        f"{'REF' if s['refused'] else 'ANS':3}  "
                        f"cite={s['n_citations']:>2}/{s['n_real_citations']:>2}  "
                        f"hit={'Y' if s['must_hit'] and in_scope else 'N'}  "
                        f"{q[:60]}", flush=True
                    )
    finally:
        root.destroy()

    # ---------- aggregate -------------------------------------------------
    def agg(filter_fn):
        sub = [r for r in rows if filter_fn(r)]
        if not sub:
            return None
        lat = [r["latency_s"] for r in sub]
        return {
            "n": len(sub),
            "lat_med": statistics.median(lat),
            "lat_p90": sorted(lat)[max(0, int(len(lat) * 0.9) - 1)],
            "refusal_rate": sum(1 for r in sub if r["refused"]) / len(sub),
            "avg_citations": statistics.mean(
                r["n_citations"] for r in sub
            ),
            "avg_real_citations": statistics.mean(
                r["n_real_citations"] for r in sub
            ),
            "valid_citation_rate": (
                statistics.mean(
                    (r["n_real_citations"] / r["n_citations"])
                    if r["n_citations"] else 0
                    for r in sub
                )
            ),
            "must_hit_rate": (
                statistics.mean(1 if r["must_hit"] else 0 for r in sub)
            ),
        }

    cells: list[tuple[str, dict | None]] = []
    for model in args.models:
        for mode in args.modes:
            for scope, name in [
                (True, "in-scope"),
                (False, "out-of-scope"),
            ]:
                cells.append((
                    f"{model} / {mode} / {name}",
                    agg(lambda r, mo=model, md=mode, sc=scope:
                        r["model"] == mo and r["mode"] == md
                        and r["in_scope"] == sc),
                ))

    # ---------- write report ----------------------------------------------
    md: list[str] = []
    md.append("# Chat eval results\n")
    md.append("Wrapper around [graphify](https://github.com/safishamsi/graphify);")
    md.append("see [NOTICE](../NOTICE).\n")
    md.append("Generated by `benchmarks/chat_eval.py`. Single run on a single")
    md.append("machine; rerun for fresh numbers.\n")
    md.append(f"- Test repo: `{args.repo}`")
    md.append(f"- Questions: {len(QUESTIONS)} "
              f"({sum(1 for _, sc, _ in QUESTIONS if sc)} in-scope, "
              f"{sum(1 for _, sc, _ in QUESTIONS if not sc)} out-of-scope)")
    md.append(f"- Models: {', '.join(args.models)}")
    md.append(f"- Modes: {', '.join(args.modes)}\n")

    md.append("## Summary\n")
    md.append("| cell | n | lat_med | lat_p90 | refuse | "
              "cites_total | cites_real | valid_cite_% | must_hit_% |")
    md.append("|---|---|---|---|---|---|---|---|---|")
    for label, a in cells:
        if not a:
            md.append(f"| {label} | 0 | - | - | - | - | - | - | - |")
            continue
        md.append(
            f"| {label} | {a['n']} | "
            f"{a['lat_med']:.1f}s | {a['lat_p90']:.1f}s | "
            f"{a['refusal_rate']*100:.0f}% | "
            f"{a['avg_citations']:.1f} | "
            f"{a['avg_real_citations']:.1f} | "
            f"{a['valid_citation_rate']*100:.0f}% | "
            f"{a['must_hit_rate']*100:.0f}% |"
        )
    md.append("")
    md.append("Reading guide:\n")
    md.append("- For **in-scope** rows: refusal_rate should be near 0% "
              "(you want answers, not refusals). cites_real and "
              "valid_cite_% should be high. must_hit_% should be high.")
    md.append("- For **out-of-scope** rows: refusal_rate should be near "
              "100% (the model should refuse questions the graph can't "
              "answer). cites_total should be 0.\n")

    md.append("## Per-question detail\n")
    md.append("| model | mode | scope | q | latency | refuse | cites/real | "
              "answer (first 120 chars) |")
    md.append("|---|---|---|---|---|---|---|---|")
    for r in rows:
        md.append(
            f"| {r['model']} | {r['mode']} | "
            f"{'in' if r['in_scope'] else 'OOS'} | "
            f"{r['question'][:50]} | "
            f"{r['latency_s']}s | "
            f"{'yes' if r['refused'] else 'no'} | "
            f"{r['n_citations']}/{r['n_real_citations']} | "
            f"{r['answer_first_120']} |"
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(md), encoding="utf-8")
    print(f"\nwrote {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

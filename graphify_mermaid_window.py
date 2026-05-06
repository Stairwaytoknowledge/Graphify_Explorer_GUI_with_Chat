"""Live Mermaid renderer subprocess.

Spawned by graphify_gui.py to render Mermaid diagrams that update as
the user clicks nodes in the main graph view. Communicates with the
parent over stdin/stdout using newline-delimited JSON.

Commands accepted on stdin:
    {"cmd": "render", "text": "flowchart TD\\n  A --> B\\n"}
    {"cmd": "clear"}
    {"cmd": "close"}

Events emitted on stdout:
    {"event": "ready"}
    {"event": "rendered", "ok": true|false, "error": "..."}
"""

from __future__ import annotations

import html
import json
import os
import sys
import threading
from pathlib import Path

import webview

# Use a CDN URL for Mermaid by default. Falls back to a local vendored
# copy if present (place mermaid.min.js next to this script for offline).
SCRIPT_DIR = Path(__file__).resolve().parent
VENDORED = SCRIPT_DIR / "mermaid.min.js"
MERMAID_SRC = (
    f"<script src='file://{VENDORED.as_posix()}'></script>"
    if VENDORED.exists()
    else "<script src='https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js'></script>"
)

# Theme is set by the GUI parent via this env var. "default" = light,
# "dark" = dark. Falls back to dark which matches the previous behavior.
MERMAID_THEME = os.environ.get("GRAPHIFY_MERMAID_THEME", "dark")
if MERMAID_THEME not in ("default", "dark", "neutral", "forest", "base"):
    MERMAID_THEME = "dark"

# Page background and text colors track the theme so the surrounding
# chrome (status bar, explanation panel) doesn't clash with the diagram.
if MERMAID_THEME == "default":
    THEME_BG = "#f6f8fb"
    THEME_PANEL = "#eef1f7"
    THEME_PANEL_ALT = "#e2e7f0"
    THEME_FG = "#1a2233"
    THEME_FG_DIM = "#5b6678"
    THEME_ACCENT = "#1e7fb8"
    THEME_BORDER = "#aab4c3"
    THEME_ERR = "#c0392b"
else:
    THEME_BG = "#162033"
    THEME_PANEL = "#101826"
    THEME_PANEL_ALT = "#1d2a40"
    THEME_FG = "#e7ecf3"
    THEME_FG_DIM = "#9aa6b8"
    THEME_ACCENT = "#5ac6ff"
    THEME_BORDER = "#1d2a40"
    THEME_ERR = "#ff7a90"

PAGE_HTML = f"""<!doctype html>
<html lang='en'>
<head>
<meta charset='utf-8'>
<title>Graphify - Mermaid</title>
{MERMAID_SRC}
<style>
  html, body {{
    margin: 0;
    padding: 0;
    height: 100%;
    background: {THEME_BG};
    color: {THEME_FG};
    font-family: -apple-system, Segoe UI, Roboto, sans-serif;
    font-size: 12px;
    overflow: hidden;
  }}
  body {{
    display: flex;
    flex-direction: column;
    height: 100vh;
  }}
  #status {{
    padding: 8px 14px;
    color: {THEME_FG_DIM};
    border-bottom: 1px solid {THEME_BORDER};
    background: {THEME_PANEL};
    position: sticky;
    top: 0;
    z-index: 10;
  }}
  #legend {{
    padding: 6px 14px;
    color: {THEME_FG_DIM};
    font-size: 11px;
    border-bottom: 1px solid {THEME_BORDER};
    background: {THEME_PANEL};
  }}
  #legend code {{
    background: {THEME_PANEL_ALT};
    color: {THEME_ACCENT};
    padding: 1px 4px;
    border-radius: 3px;
  }}
  #status, #legend {{ flex: 0 0 auto; }}
  #stage-wrap {{
    position: relative;
    overflow: hidden;
    flex: 1 1 auto;
    min-height: 200px;
    cursor: grab;
    background: {THEME_BG};
  }}
  #explanation {{
    flex: 0 0 auto;
    max-height: 32%;
    overflow-y: auto;
    border-top: 1px solid {THEME_BORDER};
    padding: 8px 14px;
    background: {THEME_PANEL};
    color: {THEME_FG};
    font-size: 11.5px;
    line-height: 1.45;
  }}
  #explanation h4 {{
    margin: 0 0 6px 0;
    font-size: 11px;
    color: {THEME_ACCENT};
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }}
  #explanation .empty {{
    color: {THEME_FG_DIM};
    font-style: italic;
  }}
  #explanation ul {{
    margin: 4px 0 4px 18px;
    padding: 0;
  }}
  #explanation li {{ margin: 1px 0; }}
  #explanation code {{
    background: {THEME_PANEL_ALT};
    color: {THEME_ACCENT};
    padding: 1px 4px;
    border-radius: 3px;
  }}
  #stage-wrap.grabbing {{ cursor: grabbing; }}
  #stage {{
    position: absolute;
    top: 0;
    left: 0;
    padding: 10px;
    transform-origin: 0 0;
    will-change: transform;
  }}
  #stage svg {{
    max-width: none;
    height: auto;
    background: transparent;
    user-select: none;
  }}
  #ctrl-hint {{
    position: absolute;
    right: 10px;
    bottom: 10px;
    padding: 4px 8px;
    background: rgba(16, 24, 38, 0.85);
    color: {THEME_FG_DIM};
    border: 1px solid {THEME_BORDER};
    border-radius: 4px;
    font-size: 10px;
    pointer-events: none;
    z-index: 5;
  }}
  /* Diagram label fill follows the active theme. */
  #stage .nodeLabel, #stage .edgeLabel, #stage text {{
    fill: {THEME_FG} !important;
    color: {THEME_FG} !important;
  }}
  #stage .edgeLabel {{
    background: {THEME_BG} !important;
  }}
  .empty {{
    padding: 40px 14px;
    color: {THEME_FG_DIM};
    text-align: center;
  }}
  .err {{
    padding: 14px;
    color: {THEME_ERR};
    white-space: pre-wrap;
    font-family: Consolas, Menlo, monospace;
    font-size: 11px;
  }}
</style>
</head>
<body>
<div id='status'>waiting...</div>
<div id='legend'>
  edges: <code>--></code> calls &nbsp;
  <code>-.-></code> imports &nbsp;
  <code>...></code> contains &nbsp;
  <code>==></code> inherits &nbsp;
  <code>-.-</code> references
</div>
<div id='stage-wrap'>
  <div id='stage'>
    <div class='empty'>
      Click a node in the main graph to render its 1-hop Mermaid here.
    </div>
  </div>
  <div id='ctrl-hint'>scroll: zoom &middot; drag: pan &middot; dbl-click: reset</div>
</div>
<div id='explanation'>
  <h4>About this diagram</h4>
  <div id='explanation-body' class='empty'>
    Pick a node to see a plain-language summary of what the diagram
    shows. The summary is built from the graph data (no LLM), so it is
    factually correct.
  </div>
</div>
<script>
  // Initialize Mermaid with a dark theme. We render imperatively via
  // mermaid.render() to keep diagrams swappable.
  mermaid.initialize({{
    startOnLoad: false,
    theme: '{MERMAID_THEME}',
    themeVariables: {{
      darkMode: {('false' if MERMAID_THEME == 'default' else 'true')},
      background: '{THEME_BG}',
      primaryColor: '{THEME_PANEL_ALT}',
      primaryTextColor: '{THEME_FG}',
      primaryBorderColor: '{THEME_BORDER}',
      lineColor: '{THEME_ACCENT}',
      secondaryColor: '{THEME_BG}',
      tertiaryColor: '{THEME_PANEL}'
    }},
    flowchart: {{ curve: 'basis', useMaxWidth: true }}
  }});

  let _seq = 0;
  function setStatus(s) {{ document.getElementById('status').textContent = s; }}

  window.gx_render = async function(text, status, explanation) {{
    setStatus(status || 'rendering...');
    const stageEl = document.getElementById('stage');
    stageEl.innerHTML = '';
    const id = 'gx-' + (++_seq);
    try {{
      const {{svg}} = await mermaid.render(id, text);
      stageEl.innerHTML = svg;
      // Reset pan/zoom so every new diagram starts framed.
      if (typeof window.gx_reset_view === 'function') window.gx_reset_view();
      setStatus(status || 'ready');
      if (window.pywebview) window.pywebview.api.on_rendered(true, '');
    }} catch (e) {{
      stageEl.innerHTML = '<pre class=err>' + (e.message || String(e)) + '</pre>';
      setStatus('render error');
      if (window.pywebview) window.pywebview.api.on_rendered(false, String(e));
    }}
    // Update the bottom explanation panel. `explanation` is HTML built
    // by Python from the subgraph - factually correct by construction.
    const expl = document.getElementById('explanation-body');
    if (expl) {{
      if (explanation && explanation.length) {{
        expl.classList.remove('empty');
        expl.innerHTML = explanation;
      }} else {{
        expl.classList.add('empty');
        expl.textContent = 'No explanation available for this diagram.';
      }}
    }}
  }};

  window.gx_clear = function() {{
    document.getElementById('stage').innerHTML =
      '<div class=empty>Click a node to render its diagram.</div>';
    setStatus('cleared');
    resetTransform();
  }};

  // ---- pan + zoom on the rendered SVG ---------------------------------
  // Wraps the #stage div with translate+scale CSS transforms. Wheel
  // zooms anchored at the cursor; drag pans; double-click resets.
  let _scale = 1, _tx = 0, _ty = 0;
  let _dragOrigin = null;
  const stage = document.getElementById('stage');
  const wrap = document.getElementById('stage-wrap');

  function applyTransform() {{
    stage.style.transform =
      'translate(' + _tx + 'px,' + _ty + 'px) scale(' + _scale + ')';
  }}

  function resetTransform() {{
    _scale = 1; _tx = 0; _ty = 0;
    applyTransform();
  }}
  window.gx_reset_view = resetTransform;

  wrap.addEventListener('wheel', function (e) {{
    e.preventDefault();
    const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
    const rect = wrap.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    // Keep the point under the cursor stationary.
    _tx = x - (x - _tx) * factor;
    _ty = y - (y - _ty) * factor;
    _scale *= factor;
    if (_scale < 0.05) _scale = 0.05;
    if (_scale > 25)   _scale = 25;
    applyTransform();
  }}, {{ passive: false }});

  wrap.addEventListener('mousedown', function (e) {{
    if (e.button !== 0) return;
    _dragOrigin = {{ x: e.clientX - _tx, y: e.clientY - _ty }};
    wrap.classList.add('grabbing');
    e.preventDefault();
  }});

  window.addEventListener('mousemove', function (e) {{
    if (!_dragOrigin) return;
    _tx = e.clientX - _dragOrigin.x;
    _ty = e.clientY - _dragOrigin.y;
    applyTransform();
  }});

  window.addEventListener('mouseup', function () {{
    _dragOrigin = null;
    wrap.classList.remove('grabbing');
  }});

  wrap.addEventListener('dblclick', function (e) {{
    e.preventDefault();
    resetTransform();
  }});

  window.addEventListener('load', function () {{
    setStatus('ready');
    // NOTE: don't call pywebview.api.on_ready() here. At window 'load'
    // pywebview's bridge may not yet be injected. The Python side calls
    // on_ready from its own on_loaded hook (which fires after the bridge
    // is ready), so this listener only needs to update the status text.
  }});
</script>
</body>
</html>
"""


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


class Api:
    def on_ready(self):
        emit({"event": "ready"})

    def on_rendered(self, ok, err):
        emit({"event": "rendered", "ok": bool(ok), "error": str(err or "")})


def main() -> int:
    # Write the page to a temp file - pywebview can load file:// URLs.
    out_dir = SCRIPT_DIR
    page_path = out_dir / "_mermaid_view.html"
    page_path.write_text(PAGE_HTML, encoding="utf-8")

    api = Api()
    window = webview.create_window(
        "Graphify - Mermaid",
        page_path.as_uri(),
        width=560,
        height=720,
        x=1380,
        y=80,
        resizable=True,
        js_api=api,
    )

    def stdin_loop():
        for raw in sys.stdin:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            cmd = msg.get("cmd")
            if cmd == "render":
                text = msg.get("text") or ""
                status = msg.get("status") or "ready"
                explanation = msg.get("explanation") or ""
                # JSON-escape each field for safe JS injection.
                payload = json.dumps(text)
                stat_payload = json.dumps(status)
                expl_payload = json.dumps(explanation)
                window.evaluate_js(
                    "window.gx_render && gx_render("
                    f"{payload}, {stat_payload}, {expl_payload})"
                )
            elif cmd == "clear":
                window.evaluate_js("window.gx_clear && gx_clear()")
            elif cmd == "close":
                window.destroy()
                return

    def on_loaded():
        # pywebview calls this AFTER the page is fully loaded AND the
        # JS bridge is injected, so window.pywebview.api is guaranteed
        # to exist here. Signal ready from this side (more reliable
        # than the page's own 'load' event, which can fire before the
        # bridge is set up).
        try:
            window.evaluate_js(
                "if (window.pywebview && window.pywebview.api) "
                "window.pywebview.api.on_ready();"
            )
        except Exception:
            pass
        threading.Thread(target=stdin_loop, daemon=True).start()

    webview.start(on_loaded, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())

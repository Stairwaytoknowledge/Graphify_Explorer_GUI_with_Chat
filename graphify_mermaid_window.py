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
    background: #162033;
    color: #e7ecf3;
    font-family: -apple-system, Segoe UI, Roboto, sans-serif;
    font-size: 12px;
    overflow: auto;
  }}
  #status {{
    padding: 8px 14px;
    color: #9aa6b8;
    border-bottom: 1px solid #1d2a40;
    background: #101826;
    position: sticky;
    top: 0;
    z-index: 10;
  }}
  #legend {{
    padding: 6px 14px;
    color: #9aa6b8;
    font-size: 11px;
    border-bottom: 1px solid #1d2a40;
    background: #101826;
  }}
  #legend code {{
    background: #1d2a40;
    color: #5ac6ff;
    padding: 1px 4px;
    border-radius: 3px;
  }}
  #stage {{
    padding: 10px;
  }}
  #stage svg {{
    max-width: 100%;
    height: auto;
    background: transparent;
  }}
  /* Light text against dark background */
  #stage .nodeLabel, #stage .edgeLabel, #stage text {{
    fill: #e7ecf3 !important;
    color: #e7ecf3 !important;
  }}
  #stage .edgeLabel {{
    background: #162033 !important;
  }}
  .empty {{
    padding: 40px 14px;
    color: #9aa6b8;
    text-align: center;
  }}
  .err {{
    padding: 14px;
    color: #ff7a90;
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
<div id='stage'>
  <div class='empty'>
    Click a node in the main graph to render its 1-hop Mermaid here.
  </div>
</div>
<script>
  // Initialize Mermaid with a dark theme. We render imperatively via
  // mermaid.render() to keep diagrams swappable.
  mermaid.initialize({{
    startOnLoad: false,
    theme: 'dark',
    themeVariables: {{
      darkMode: true,
      background: '#162033',
      primaryColor: '#1d2a40',
      primaryTextColor: '#e7ecf3',
      primaryBorderColor: '#33415a',
      lineColor: '#5ac6ff',
      secondaryColor: '#162033',
      tertiaryColor: '#101826'
    }},
    flowchart: {{ curve: 'basis', useMaxWidth: true }}
  }});

  let _seq = 0;
  function setStatus(s) {{ document.getElementById('status').textContent = s; }}

  window.gx_render = async function(text, status) {{
    setStatus(status || 'rendering...');
    const stage = document.getElementById('stage');
    stage.innerHTML = '';
    const id = 'gx-' + (++_seq);
    try {{
      const {{svg}} = await mermaid.render(id, text);
      stage.innerHTML = svg;
      setStatus(status || 'ready');
      if (window.pywebview) window.pywebview.api.on_rendered(true, '');
    }} catch (e) {{
      stage.innerHTML = '<pre class=err>' + (e.message || String(e)) + '</pre>';
      setStatus('render error');
      if (window.pywebview) window.pywebview.api.on_rendered(false, String(e));
    }}
  }};

  window.gx_clear = function() {{
    document.getElementById('stage').innerHTML =
      '<div class=empty>Click a node to render its diagram.</div>';
    setStatus('cleared');
  }};

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
                # JSON-escape the text for safe JS injection.
                payload = json.dumps(text)
                stat_payload = json.dumps(status)
                window.evaluate_js(
                    f"window.gx_render && gx_render({payload}, {stat_payload})"
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

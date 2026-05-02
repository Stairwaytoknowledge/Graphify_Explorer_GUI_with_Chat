"""Interactive graph viewer subprocess.

Spawned by graphify_gui.py to display upstream's vis.js graph.html in a
native pywebview window. Communicates with the parent over stdin/stdout
using newline-delimited JSON.

Commands accepted on stdin (one JSON object per line):
    {"cmd": "highlight", "ids": ["id1", "id2", ...]}
    {"cmd": "clear_highlight"}
    {"cmd": "fit"}
    {"cmd": "close"}

Events emitted on stdout (one JSON object per line):
    {"event": "ready"}
    {"event": "click", "id": "<node_id>"}
    {"event": "double_click", "id": "<node_id>"}
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import webview


JS_SHIM = r"""
(function() {
    if (window._gx_shim_installed) return;
    window._gx_shim_installed = true;

    if (typeof network === 'undefined') {
        console.warn('gx: network not found - shim cannot attach');
        return;
    }

    // Cache original colors so we can restore them on clear_highlight.
    var _orig_state = null;
    function _snapshot() {
        if (_orig_state) return;
        _orig_state = {};
        var nodes = network.body.data.nodes;
        nodes.forEach(function(n) {
            _orig_state[n.id] = {
                color: n.color,
                font: n.font,
                borderWidth: n.borderWidth,
            };
        });
    }

    window.gx_highlight = function(ids) {
        _snapshot();
        var picked = new Set(ids);
        var updates = [];
        network.body.data.nodes.forEach(function(n) {
            if (picked.has(n.id)) {
                updates.push({
                    id: n.id,
                    color: { background: '#ff7a3d', border: '#ffb454' },
                    borderWidth: 4,
                    font: { color: '#ffffff', size: 18 },
                });
            } else {
                updates.push({
                    id: n.id,
                    color: { background: 'rgba(80,90,110,0.18)',
                             border: 'rgba(80,90,110,0.25)' },
                    font: { color: 'rgba(200,210,230,0.25)' },
                    borderWidth: 1,
                });
            }
        });
        network.body.data.nodes.update(updates);
        if (ids.length) {
            network.fit({ nodes: ids, animation: { duration: 600 } });
        }
    };

    window.gx_clear_highlight = function() {
        if (!_orig_state) return;
        var updates = [];
        Object.keys(_orig_state).forEach(function(id) {
            updates.push(Object.assign({ id: id }, _orig_state[id]));
        });
        network.body.data.nodes.update(updates);
        _orig_state = null;
    };

    window.gx_fit = function() {
        network.fit({ animation: { duration: 400 } });
    };

    network.on('selectNode', function(params) {
        if (params.nodes && params.nodes.length && window.pywebview) {
            window.pywebview.api.on_click(params.nodes[0]);
        }
    });
    network.on('doubleClick', function(params) {
        if (params.nodes && params.nodes.length && window.pywebview) {
            window.pywebview.api.on_double_click(params.nodes[0]);
        }
    });

    if (window.pywebview) window.pywebview.api.on_ready();
})();
"""


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


class Api:
    """Surface exposed to JS as window.pywebview.api.*"""

    def on_ready(self):
        emit({"event": "ready"})

    def on_click(self, node_id):
        emit({"event": "click", "id": node_id})

    def on_double_click(self, node_id):
        emit({"event": "double_click", "id": node_id})


def main() -> int:
    if len(sys.argv) < 2:
        sys.stderr.write("usage: graphify_vis_window.py <graph.html>\n")
        return 2
    html_path = Path(sys.argv[1]).resolve()
    if not html_path.exists():
        sys.stderr.write(f"missing: {html_path}\n")
        return 2

    api = Api()
    window = webview.create_window(
        "Graphify - Interactive",
        html_path.as_uri(),
        width=1280,
        height=860,
        js_api=api,
    )

    def stdin_loop():
        for raw in sys.stdin:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            cmd = msg.get("cmd")
            if cmd == "highlight":
                ids = msg.get("ids") or []
                window.evaluate_js(
                    f"window.gx_highlight && gx_highlight("
                    f"{json.dumps(ids)})"
                )
            elif cmd == "clear_highlight":
                window.evaluate_js(
                    "window.gx_clear_highlight && gx_clear_highlight()"
                )
            elif cmd == "fit":
                window.evaluate_js("window.gx_fit && gx_fit()")
            elif cmd == "close":
                window.destroy()
                return

    def on_loaded():
        # Inject the shim. Wait briefly for vis.js physics to populate
        # the network object before defining helpers that reference it.
        window.evaluate_js(JS_SHIM)
        threading.Thread(target=stdin_loop, daemon=True).start()

    webview.start(on_loaded, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())

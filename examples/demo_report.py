"""Produce the stack-guardian doctor report shown at the top of the README, the same on any machine.

    python examples/demo_report.py

It reads examples/nodes.json (placeholder addresses only), starts a stub Ollama on 127.0.0.1 and runs the real
collector and the shipped rules against it twice with one fresh state file, printing each run's report.

- gpu-box (http://127.0.0.1:11434 in the nodes file) is answered by the stub, which listens on a free loopback port.
  It has qwen3:8b loaded, due to unload in 30 seconds.
- gpu-box-2 and home-server have placeholder tailnet addresses; they are never contacted, the connection is refused.
- nvidia-smi and tailscale are not run: fixed sample output stands in for them, so the report does not depend on
  this machine's GPUs or tailnet.
- The shipped rules only report problems, so one extra rule below reports the endpoints that answer as OK.
  Only WARN and FAIL findings are graded, so its line is printed without a grade.

tests/test_demo_report.py fails if the README block and this script's output differ.
"""
import json
import os
import sys
import tempfile
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from stack_guardian import cli, collect, core, state as sg_state  # noqa: E402
from stack_guardian import finding as F  # noqa: E402

NODES = os.path.join(ROOT, "examples", "nodes.json")
STUB_FOR = "http://127.0.0.1:11434"  # the gpu-box address in examples/nodes.json

GPU_CSV = "0, GPU-0000, NVIDIA GeForce RTX 3060, 12288, 6400\n"
APPS_CSV = "GPU-0000, 4242, ollama, 6400\n"
TAILSCALE = json.dumps({"BackendState": "Running", "Peer": {
    "a": {"HostName": "gpu-box-2", "Online": True, "CurAddr": "100.64.0.2:41641", "Relay": "ord", "TailscaleIPs": ["100.64.0.2"]},
    "b": {"HostName": "home-server", "Online": False, "CurAddr": "", "Relay": "ord", "TailscaleIPs": ["100.64.0.3"]},
}})


class StubOllama(BaseHTTPRequestHandler):
    """Just enough of the Ollama API for the collector: /api/version, /api/tags, /api/ps."""

    def do_GET(self):  # noqa: N802 - http.server's name
        expires = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(timespec="seconds")
        body = {"/api/version": {"version": "0.12.0"},
                "/api/tags": {"models": [{"name": "qwen3:8b"}, {"name": "nomic-embed-text"}]},
                "/api/ps": {"models": [{"name": "qwen3:8b", "size_vram": 6710886400, "expires_at": expires}]},
                }.get(self.path)
        data = json.dumps(body).encode("utf-8")
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        pass


def answering(snapshot, _ctx):
    """OK for every node with an endpoint that answered (the shipped rules only report problems)."""
    found = []
    for name, node in (snapshot.get("nodes") or {}).items():
        up = [o["url"] for o in node.get("ollama") or [] if not o.get("error")]
        if up:
            found.append(F.make_finding("ollama.answering", "ok", "OK", name,
                                        "%d endpoint(s) answer: %s" % (len(up), ", ".join(up))))
    return found


def report():
    """Both runs' reports as one string."""
    with open(NODES, encoding="utf-8") as fh:
        nodes = json.load(fh)
    server = ThreadingHTTPServer(("127.0.0.1", 0), StubOllama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stub = "http://127.0.0.1:%d" % server.server_address[1]

    def http_get(url, timeout=5):
        if url.startswith(STUB_FOR + "/"):
            return collect.default_http_get(stub + url[len(STUB_FOR):], timeout=timeout)
        raise ConnectionRefusedError("placeholder address %s, not contacted" % urllib.parse.urlsplit(url).netloc)

    def run(argv, timeout=15):
        if argv == collect.GPU_QUERY:
            return GPU_CSV
        if argv == collect.APPS_QUERY:
            return APPS_CSV
        if argv == collect.TAILSCALE_STATUS:
            return TAILSCALE
        return None

    checks = core.load_checks(cli.DEFAULT_CHECKS_DIR) + [("ollama.answering", answering)]
    out = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state.json")
            for n in (1, 2):
                snap = collect.snapshot(nodes, run=run, http_get=http_get, timeout_s=10.0)
                findings = core.run_all(checks, snap, {})
                history = sg_state.load(state)  # graded the way cli.doctor grades: this run counts as one sample
                for node in sorted(set(snap["nodes"]) | {f["node"] for f in findings}):
                    sg_state.record_run(history, node, findings, snap["taken_at"])
                sg_state.save(history, state)
                graded = [sg_state.grade(history, f["node"], f) for f in findings]
                for f in graded:  # only WARN and FAIL are graded, so a passing check prints without one
                    if f["level"] not in sg_state.BAD:
                        f["confidence"] = ""
                out.append("# run %d%s" % (n, " (same state file)" if n > 1 else ""))
                out.append(core.render(graded))
    finally:
        server.shutdown()
        server.server_close()
    return "\n".join(out)


if __name__ == "__main__":
    print(report())

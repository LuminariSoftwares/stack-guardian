# @job: wq1003-serve-t2-1 -- ox (code_ladder T2) try 1, probe_serve 14/14; brief + probe by O5 2026-10-03 (P43 S4)
"""Tiny read-only HTTP endpoint for a stack_guardian health snapshot.

One machine asks another for its *current* health by GETting /snapshot; the
server collects a fresh snapshot on every request, never caches, and refuses
every write verb.  Binds to loopback unless the caller explicitly opts in to a
remote bind with allow_remote=True.
"""

import http.server
import json
import sys

LOOPBACK = ("127.0.0.1", "::1", "localhost")

_READ_ONLY = {"error": "read-only: GET /snapshot or /health"}


def make_handler(get_snapshot):
    """Return a BaseHTTPRequestHandler subclass bound to get_snapshot."""

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "stack-guardian-serve"

        def _reply(self, code, obj):
            body = json.dumps(obj, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _get(self):
            path = self.path.split("?", 1)[0]
            if path == "/health":
                self._reply(200, {"ok": True})
            elif path == "/snapshot":
                try:
                    snapshot = get_snapshot()
                except Exception as exc:  # noqa: BLE001 - report, never crash the server
                    self._reply(500, {"error": "%s: %s" % (type(exc).__name__, exc)})
                else:
                    self._reply(200, snapshot)
            else:
                self._reply(404, {"error": "not found"})

        def _refuse_write(self):
            self._reply(405, dict(_READ_ONLY))

        def do_GET(self):  # noqa: N802 - name fixed by BaseHTTPRequestHandler
            self._get()

        def do_POST(self):  # noqa: N802
            self._refuse_write()

        def do_PUT(self):  # noqa: N802
            self._refuse_write()

        def do_DELETE(self):  # noqa: N802
            self._refuse_write()

        def do_PATCH(self):  # noqa: N802
            self._refuse_write()

        def log_message(self, *args):
            pass

    return Handler


def make_server(get_snapshot, host="127.0.0.1", port=0, allow_remote=False):
    """Build (but do not start) a threaded server, refusing non-loopback binds."""
    if host not in LOOPBACK and not allow_remote:
        raise ValueError("refusing to bind %s without allow_remote=True" % host)
    return http.server.ThreadingHTTPServer((host, port), make_handler(get_snapshot))


def _opt(argv, name):
    """Return the token following `name` in argv, or None when there is none."""
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return _selftest()
    nodes = _opt(argv, "--nodes")
    if nodes is None:
        print("stack-guardian-serve: usage: --nodes FILE [--port N] [--host H --allow-remote]",
              file=sys.stderr)
        return 3
    from . import collect as sg_collect

    def snap():
        with open(nodes, encoding="utf-8") as fh:
            return sg_collect.snapshot(json.load(fh))

    server = make_server(snap, _opt(argv, "--host") or "127.0.0.1",
                         int(_opt(argv, "--port") or "8788"), "--allow-remote" in argv)
    print("serving http://%s:%d/snapshot" % server.server_address[:2], flush=True)
    server.serve_forever()
    return 0


def _selftest():
    import threading
    import urllib.error
    import urllib.request
    state = {"n": 0}

    def snap():
        state["n"] += 1
        return {"schema": 1, "taken_at": "T", "nodes": {"a": {"reachable": True}}, "n": state["n"]}

    def boom():
        raise RuntimeError("collector died")

    def start(fn):
        s = make_server(fn)
        threading.Thread(target=s.serve_forever, daemon=True).start()
        return s, "http://127.0.0.1:%d" % s.server_address[1]

    def get(url, method="GET"):
        req = urllib.request.Request(url, method=method, data=b"{}" if method != "GET" else None)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.headers.get("Content-Type"), json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type"), json.loads(e.read().decode("utf-8"))

    s, base = start(snap)
    bad, bad_base = start(boom)
    try:
        def c_snapshot():
            code, ctype, body = get(base + "/snapshot")
            return code == 200 and ctype == "application/json" and body["nodes"] == {"a": {"reachable": True}}

        def c_fresh_each_call():
            a = get(base + "/snapshot")[2]["n"]
            b = get(base + "/snapshot?x=1")[2]["n"]
            return b == a + 1

        def c_health():
            return get(base + "/health")[:3:2] == (200, {"ok": True})

        def c_not_found():
            return get(base + "/etc/passwd")[0] == 404

        def c_post_refused():
            code, _, body = get(base + "/snapshot", "POST")
            return code == 405 and body["error"].startswith("read-only")

        def c_delete_refused():
            return get(base + "/snapshot", "DELETE")[0] == 405

        def c_collector_error_500():
            code, _, body = get(bad_base + "/snapshot")
            return code == 500 and body["error"] == "RuntimeError: collector died"

        def c_loopback_default():
            return s.server_address[0] == "127.0.0.1"

        def c_remote_refused():
            try:
                make_server(snap, host="0.0.0.0")  # nosec B104 - asserts the refusal; nothing binds
            except ValueError as exc:
                return "allow_remote" in str(exc)
            return False

        checks = [("snapshot", c_snapshot), ("fresh_each_call", c_fresh_each_call), ("health", c_health),
                  ("not_found", c_not_found), ("post_refused", c_post_refused), ("delete_refused", c_delete_refused),
                  ("collector_error_500", c_collector_error_500), ("loopback_default", c_loopback_default),
                  ("remote_refused", c_remote_refused)]
        passed = failed = 0
        for name, fn in checks:
            try:
                ok = bool(fn())
            except Exception as exc:  # noqa: BLE001
                ok = False
                print("FAIL %s -- raised %s: %s" % (name, type(exc).__name__, exc))
            else:
                print(("ok %s" if ok else "FAIL %s") % name)
            passed += ok
            failed += not ok
        print("serve selftest: %d checks, %d passed, %d failed" % (len(checks), passed, failed))
        return 0 if failed == 0 and checks else 1
    finally:
        s.shutdown()
        bad.shutdown()


if __name__ == "__main__":
    sys.exit(main())

"""Tiny read-only HTTP endpoint for a stack_guardian health snapshot.

One machine asks another for its *current* health by GETting /snapshot; the
server collects a fresh snapshot on every request, never caches, and refuses
every write verb.  Binds to loopback unless the caller explicitly opts in to a
remote bind with allow_remote=True.
"""

import http.server
import json
import os
import sys
import threading
import time

LOOPBACK = ("127.0.0.1", "::1", "localhost")

_READ_ONLY = {"error": "read-only: GET /snapshot, /metrics or /health"}
PROM_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _label(value):
    """Escape a Prometheus label value: backslash, double quote and newline."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class Metrics:
    """Runs the rules on each snapshot and renders Prometheus text. A finding series seen once stays in the output
    at 0 after it resolves, so a gauge drops to 0 instead of vanishing."""

    def __init__(self, checks_dir=None, ctx=None, clock=time.time):
        from . import core as sg_core
        from . import finding as sg_finding
        self._core, self._finding = sg_core, sg_finding
        self.checks_dir = checks_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "checks.d")
        self.ctx = ctx or {}
        self.clock = clock
        self._seen = {}  # (code, severity, node) -> 0/1, in first-seen order
        self._lock = threading.Lock()

    def render(self, snapshot):
        findings = self._core.run_all(self._core.load_checks(self.checks_dir), snapshot, self.ctx)
        worst = self._finding.LEVELS.index(self._finding.worst(findings))
        with self._lock:
            for key in self._seen:
                self._seen[key] = 0
            for f in findings:
                self._seen[(f.get("code"), f.get("level"), f.get("node", ""))] = 1
            series = sorted(self._seen.items(), key=lambda kv: tuple(map(str, kv[0])))
        lines = ["# HELP stack_guardian_finding 1 while a finding is raised, 0 once it has resolved.",
                 "# TYPE stack_guardian_finding gauge"]
        lines += ['stack_guardian_finding{code="%s",severity="%s",node="%s"} %d' % (_label(c), _label(s), _label(n), v)
                  for (c, s, n), v in series]
        lines += ["# HELP stack_guardian_worst_severity Worst finding of the last run: 0 OK, 1 INFO, 2 WARN, 3 FAIL.",
                  "# TYPE stack_guardian_worst_severity gauge",
                  "stack_guardian_worst_severity %d" % worst,
                  "# HELP stack_guardian_last_run_timestamp Unix time of the last run, in seconds.",
                  "# TYPE stack_guardian_last_run_timestamp gauge",
                  "stack_guardian_last_run_timestamp %s" % repr(float(self.clock()))]
        return "\n".join(lines) + "\n"


def make_handler(get_snapshot, metrics=None):
    """Return a BaseHTTPRequestHandler subclass bound to get_snapshot (and a Metrics for /metrics, if given)."""

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "stack-guardian-serve"

        def _reply(self, code, obj):
            body = json.dumps(obj, default=str).encode("utf-8")
            self.send_response(code)
            if code == 405:
                self.send_header("Allow", "GET")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _get(self):
            path = self.path.split("?", 1)[0]
            if path == "/health":
                self._reply(200, {"ok": True})
            elif path == "/metrics" and metrics is not None:
                try:
                    body = metrics.render(get_snapshot()).encode("utf-8")
                except Exception as exc:  # noqa: BLE001 - report, never crash the server
                    self._reply(500, {"error": "%s: %s" % (type(exc).__name__, exc)})
                    return
                self.send_response(200)
                self.send_header("Content-Type", PROM_CONTENT_TYPE)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
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


def make_server(get_snapshot, host="127.0.0.1", port=0, allow_remote=False, metrics=None):
    """Build (but do not start) a threaded server, refusing non-loopback binds."""
    if host not in LOOPBACK and not allow_remote:
        raise ValueError("%s is not a loopback address, and this server has no auth" % host)
    return http.server.ThreadingHTTPServer((host, port), make_handler(get_snapshot, metrics))


def _opt(argv, name):
    """Return the token following `name` in argv, or None when there is none."""
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


USAGE = ("stack-guardian-serve: usage: --nodes FILE [--ctx FILE] [--port N] [--cache SECONDS] "
         "[--host H --allow-remote]")


def cached(fn, ttl_s, clock=time.monotonic):
    """fn, reusing its last result for ttl_s seconds. ttl_s <= 0 calls fn every time (the default)."""
    if ttl_s <= 0:
        return fn
    last = {"at": None, "value": None}

    def call():
        now = clock()
        if last["at"] is None or now - last["at"] >= ttl_s:
            last["value"], last["at"] = fn(), now
        return last["value"]
    return call


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return _selftest()
    nodes_path = _opt(argv, "--nodes")
    if nodes_path is None:
        print(USAGE, file=sys.stderr)
        return 3
    from . import collect as sg_collect
    from . import nodes as sg_nodes

    try:
        sg_nodes.load(nodes_path)  # fail at startup, not on the first request
        cache_s = float(_opt(argv, "--cache") or 0)
        port = int(_opt(argv, "--port") or "8788")
    except (OSError, ValueError) as exc:
        print("stack-guardian-serve: usage: %s" % exc, file=sys.stderr)
        return 3

    def snap():
        nodes, options = sg_nodes.load(nodes_path)  # re-read: the file may have been edited since startup
        return sg_collect.snapshot(nodes, timeout_s=sg_nodes.timeout_s(options))

    ctx = None
    if _opt(argv, "--ctx"):
        with open(_opt(argv, "--ctx"), encoding="utf-8") as fh:
            ctx = json.load(fh)
    try:
        server = make_server(cached(snap, cache_s), _opt(argv, "--host") or "127.0.0.1",
                             port, "--allow-remote" in argv, metrics=Metrics(ctx=ctx))
    except ValueError as exc:  # a non-loopback bind without --allow-remote: one line, never a traceback
        print("stack-guardian-serve: usage: %s; add --allow-remote if you mean it" % exc, file=sys.stderr)
        return 3
    except OSError as exc:  # the port is taken, or the address is not this machine's
        print("stack-guardian-serve: cannot listen on %s:%d: %s" % (_opt(argv, "--host") or "127.0.0.1", port, exc),
              file=sys.stderr)
        return 3
    print("serving http://%s:%d/snapshot and /metrics%s"
          % (server.server_address[0], server.server_address[1],
             " (each snapshot reused for %gs)" % cache_s if cache_s > 0 else ""), flush=True)
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

        def c_cache_off_by_default_and_on_when_asked():
            ticks = iter([0.0, 0.1, 0.2, 5.0])
            calls = {"n": 0}

            def count():
                calls["n"] += 1
                return calls["n"]
            fresh = cached(count, 0)
            held = cached(count, 1.0, clock=lambda: next(ticks))
            return (fresh() != fresh() and held() == held() and held() != held())

        def c_remote_bind_without_allow_remote_is_usage_3(tmp=None):
            import tempfile
            with tempfile.TemporaryDirectory() as folder:
                path = os.path.join(folder, "nodes.json")
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump({"gpu-box": {"ollama": ["http://127.0.0.1:11434"]}}, fh)
                return main(["--nodes", path, "--host", "0.0.0.0", "--port", "0"]) == 3  # nosec B104 - nothing binds

        def c_remote_refused():
            try:
                make_server(snap, host="0.0.0.0")  # nosec B104 - asserts the refusal; nothing binds
            except ValueError as exc:
                return "no auth" in str(exc)
            return False

        checks = [("snapshot", c_snapshot), ("fresh_each_call", c_fresh_each_call), ("health", c_health),
                  ("not_found", c_not_found), ("post_refused", c_post_refused), ("delete_refused", c_delete_refused),
                  ("collector_error_500", c_collector_error_500), ("loopback_default", c_loopback_default),
                  ("cache_off_by_default_and_on_when_asked", c_cache_off_by_default_and_on_when_asked),
                  ("remote_bind_without_allow_remote_is_usage_3", c_remote_bind_without_allow_remote_is_usage_3),
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

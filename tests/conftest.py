"""A tiny local HTTP stub for tests that must go through the real urllib getters."""
import http.server
import json
import threading
import time

import pytest


class Stub:
    """routes: {(method, path): (status, body, delay_s[, headers])}; body is a dict/list (sent as JSON) or a str,
    headers an optional dict of extra response headers (a redirect's Location, for one)."""

    def __init__(self, routes):
        self.routes = dict(routes)
        self.requests = []  # (method, path, parsed JSON body or None)
        self.headers = []  # the request headers of each entry in requests, lower-cased
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _serve(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                stub.requests.append((method, self.path, json.loads(raw) if raw else None))
                stub.headers.append({k.lower(): v for k, v in self.headers.items()})
                status, body, delay, *extra = stub.routes.get((method, self.path), (404, {"error": "not found"}, 0))
                data = (body if isinstance(body, str) else json.dumps(body)).encode("utf-8")
                if delay:
                    time.sleep(delay)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for key, value in (extra[0] if extra else {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                self._serve("GET")

            def do_POST(self):  # noqa: N802
                self._serve("POST")

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def paths(self, method=None):
        return [p for m, p, _ in self.requests if method is None or m == method]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub():
    made = []

    def make(routes):
        s = Stub(routes)
        made.append(s)
        return s
    yield make
    for s in made:
        s.close()

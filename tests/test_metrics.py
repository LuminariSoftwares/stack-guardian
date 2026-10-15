"""Item 4: GET /metrics on serve returns Prometheus text format, checked by a small parser written from the
exposition-format rules (HELP/TYPE comments, name{label="value",...} value, \\ \" \\n escapes in label values)."""
import re
import threading
import urllib.error
import urllib.request

from stack_guardian import cli, serve

LINE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})? (-?[0-9.eE+-]+|NaN|[+-]Inf)$')
LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"(,|$)')


def parse_prometheus(text):
    """{(name, frozenset(labels)): float}; raises ValueError on any line the format does not allow."""
    types, samples = {}, {}
    for line in text.splitlines():
        if line.startswith("# HELP ") or not line:
            continue
        if line.startswith("# TYPE "):
            _, _, name, kind = line.split(" ", 3)
            types[name] = kind
            continue
        m = LINE.match(line)
        if not m or m.group(1) not in types:
            raise ValueError("bad sample line: %r" % line)
        labels, rest = {}, m.group(2) or ""
        while rest:
            lm = LABEL.match(rest)
            if not lm:
                raise ValueError("bad labels: %r" % line)
            labels[lm.group(1)] = re.sub(r'\\(.)', lambda e: "\n" if e.group(1) == "n" else e.group(1), lm.group(2))
            rest = rest[lm.end():]
        samples[(m.group(1), frozenset(labels.items()))] = float(m.group(3))
    return samples


DOWN = {"schema": 1, "taken_at": "T", "nodes": {
    "gpu-box": {"reachable": False, "ollama": [{"url": "http://127.0.0.1:9", "error": "OSError: refused", "running": []}]},
    'odd"node\\': {"reachable": True, "ollama": [], "gpu_error": "nvidia-smi unavailable"}}}
UP = {"schema": 1, "taken_at": "T", "nodes": {"gpu-box": {"reachable": True, "ollama": []}}}


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.headers.get("Content-Type"), r.read().decode("utf-8")


def _key(code, severity, node):
    return ("stack_guardian_finding", frozenset({"code": code, "severity": severity, "node": node}.items()))


def test_metrics_endpoint_prometheus_text():
    snaps = iter([DOWN, UP])
    metrics = serve.Metrics(cli.DEFAULT_CHECKS_DIR, ctx={}, clock=lambda: 1760443200.0)
    srv = serve.make_server(lambda: next(snaps), metrics=metrics)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_port
    try:
        status, ctype, body = _get(base + "/metrics")
        assert status == 200 and ctype.startswith("text/plain") and "version=0.0.4" in ctype
        first = parse_prometheus(body)
        assert first[_key("ollama.unreachable", "FAIL", "gpu-box")] == 1
        assert first[_key("gpu.unavailable", "INFO", 'odd"node\\')] == 1  # label escaping round-trips
        assert first[("stack_guardian_worst_severity", frozenset())] == 3
        assert first[("stack_guardian_last_run_timestamp", frozenset())] == 1760443200.0
        second = parse_prometheus(_get(base + "/metrics")[2])
        assert second[_key("ollama.unreachable", "FAIL", "gpu-box")] == 0  # a resolved series drops to 0, not away
        assert second[("stack_guardian_worst_severity", frozenset())] == 0
    finally:
        srv.shutdown()
        srv.server_close()


def test_metrics_404_when_not_enabled():
    srv = serve.make_server(lambda: UP)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/metrics" % srv.server_port, timeout=10)
        raise AssertionError("expected 404")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
    finally:
        srv.shutdown()
        srv.server_close()

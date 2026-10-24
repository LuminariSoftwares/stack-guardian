"""OpenAI-compatible endpoint rules for stack-guardian (LM Studio, llama.cpp llama-server, vLLM, LiteLLM).

The same finding family as the Ollama rules in 10_core.py, over the node's "openai" list: one finding per
affected node, graded by run history like every other. Loaded-model expiry and time to first token stay
Ollama-only, because /v1/models says nothing about either.
"""

import sys

import sg_finding as F


def _endpoints(snapshot):
    """(node name, endpoint) for every OpenAI-compatible endpoint in the snapshot, in node order."""
    nodes = (snapshot or {}).get("nodes") or {}
    return [(name, ep or {}) for name, node in nodes.items() for ep in ((node or {}).get("openai") or [])]


def _by_node(pairs):
    out = {}
    for name, ep in pairs:
        out.setdefault(name, []).append(ep)
    return out


def openai_unreachable(snapshot, ctx):
    out = []
    for name, eps in _by_node(_endpoints(snapshot)).items():
        bad = [(ep.get("url"), ep.get("error")) for ep in eps if ep.get("error")]
        if bad:
            out.append(F.make_finding(
                "openai.unreachable", "missing", "FAIL", name,
                "%d OpenAI-compatible endpoint(s) do not answer: %s" % (len(bad), ", ".join(str(url) for url, _ in bad)),
                evidence=["%s %s: %s" % (name, url, error) for url, error in bad],
                try_=["check the server (LM Studio, llama-server, vLLM, LiteLLM) is running on that node",
                      "check the base URL ends in /v1 and the port is right",
                      "if the server wants a key, check the variable named by api_key_env is set where stack-guardian runs"]))
    return out


def openai_no_models(snapshot, ctx):
    """WARN when an endpoint answered but lists no models: it is up and serving nothing."""
    out = []
    for name, eps in _by_node(_endpoints(snapshot)).items():
        empty = [ep.get("url") for ep in eps if not ep.get("error") and not ep.get("models")]
        if empty:
            out.append(F.make_finding(
                "openai.no_models", "partial", "WARN", name,
                "%d OpenAI-compatible endpoint(s) answer but list no models: %s" % (len(empty), ", ".join(str(url) for url in empty)),
                evidence=["%s %s: GET /models listed 0 models" % (name, url) for url in empty],
                try_=["load a model in the server (LM Studio: load one; vLLM and llama-server: start with a model)",
                      "for LiteLLM, check the proxy config lists model_name entries"]))
    return out


def openai_slow(snapshot, ctx):
    """INFO when GET /models took longer than openai_slow_ms (default 2000)."""
    limit = int((ctx or {}).get("openai_slow_ms", 2000))
    out = []
    for name, eps in _by_node(_endpoints(snapshot)).items():
        slow = [(ep.get("url"), ep.get("latency_ms")) for ep in eps
                if not ep.get("error") and isinstance(ep.get("latency_ms"), (int, float)) and ep["latency_ms"] > limit]
        if slow:
            out.append(F.make_finding(
                "openai.slow", "partial", "INFO", name,
                "%d OpenAI-compatible endpoint(s) took over %d ms to list models: %s" % (len(slow), limit, ", ".join(str(url) for url, _ in slow)),
                evidence=["%s %s: %d ms for GET /models" % (name, url, ms) for url, ms in slow],
                try_=["check nothing else is loading a model or generating on that node",
                      "check the network path to that node (a relayed tailnet path is slower)"]))
    return out


CHECKS = [
    ("openai.unreachable", openai_unreachable),
    ("openai.no_models", openai_no_models),
    ("openai.slow", openai_slow),
]


def _selftest():
    def snap(*eps):
        return {"nodes": {"studio": {"reachable": True, "openai": list(eps)}}}

    ok = {"url": "http://127.0.0.1:1234/v1", "error": None, "models": ["m"], "latency_ms": 12}

    def t_clean():
        return all(fn(snap(ok), {}) == [] for _, fn in CHECKS) and all(fn({}, {}) == [] and fn(None, None) == [] for _, fn in CHECKS)

    def t_unreachable():
        [f] = openai_unreachable(snap(ok, dict(ok, url="http://192.0.2.7:8000/v1", error="OSError: refused")), {})
        return f["level"] == "FAIL" and f["node"] == "studio" and "192.0.2.7" in f["summary"] and "refused" in f["evidence"][0]

    def t_no_models():
        [f] = openai_no_models(snap(dict(ok, models=[])), {})
        return f["level"] == "WARN" and openai_no_models(snap(dict(ok, models=[], error="OSError: x")), {}) == []

    def t_slow():
        [f] = openai_slow(snap(dict(ok, latency_ms=2500)), {})
        return f["level"] == "INFO" and openai_slow(snap(dict(ok, latency_ms=2500)), {"openai_slow_ms": 3000}) == []

    checks = [("clean", t_clean), ("unreachable", t_unreachable), ("no_models", t_no_models), ("slow", t_slow)]
    passed = 0
    for name, fn in checks:
        try:
            good = bool(fn())
        except Exception as exc:  # noqa: BLE001 - a crashed check is a failed check
            good = False
            name = "%s (raised %s: %s)" % (name, type(exc).__name__, exc)
        passed += good
        print(("ok   " if good else "FAIL ") + name)
    failed = len(checks) - passed
    print("sg_checks_openai selftest: %d checks, %d passed, %d failed" % (len(checks), passed, failed))
    return 0 if failed == 0 and checks else 1


if __name__ == "__main__":
    sys.exit(_selftest() if "--selftest" in sys.argv else 2)

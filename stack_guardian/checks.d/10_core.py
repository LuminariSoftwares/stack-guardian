"""Core 0.1 rules for stack-guardian, a health doctor for a home AI stack.

Each rule inspects a whole snapshot and returns a list with ONE finding per
failing node (empty when the stack is healthy along that axis), so every node
keeps its own streak, matches a node= filter and can be named as code@node.
"""

import sys
from datetime import datetime, timezone

import sg_finding as F


def _nodes(snapshot):
    """Return the snapshot's nodes as a list of (name, node) pairs, node None -> {}."""
    nodes = (snapshot or {}).get("nodes") or {}
    return [(name, node or {}) for name, node in nodes.items()]


def node_unreachable(snapshot, ctx):
    return [F.make_finding("node.unreachable", "missing", "FAIL", name,
                           "node did not answer the collector: %s" % node["error"],
                           evidence=["%s: %s" % (name, node["error"])],
                           try_=["check the machine is on and reachable", "raise timeout_s if the node is just slow"])
            for name, node in _nodes(snapshot) if node.get("error")]


def ollama_unreachable(snapshot, ctx):
    out = []
    for name, node in _nodes(snapshot):
        bad = [(ep.get("url"), ep.get("error")) for ep in (node.get("ollama") or []) if (ep or {}).get("error")]
        if bad:
            out.append(F.make_finding(
                "ollama.unreachable", "missing", "FAIL", name,
                "%d Ollama endpoint(s) do not answer: %s" % (len(bad), ", ".join(url for url, _ in bad)),
                evidence=["%s %s: %s" % (name, url, error) for url, error in bad],
                try_=["check the Ollama service on that node"]))
    return out


def model_not_loaded(snapshot, ctx):
    expect = (ctx or {}).get("expect") or {}
    out = []
    for name, node in _nodes(snapshot):
        want = (expect.get(name) or {}).get("model")
        if not want or not node.get("reachable"):
            continue
        running = [(m or {}).get("name") for ep in (node.get("ollama") or []) for m in ((ep or {}).get("running") or [])]
        if want not in running:
            out.append(F.make_finding("model.not_loaded", "partial", "WARN", name,
                                      "expected model not loaded: %s wants %s" % (name, want),
                                      evidence=["%s running: %s" % (name, ", ".join(running) or "nothing")]))
    return out


def _when(text):
    """An aware datetime from an ISO 8601 string (Ollama writes nanoseconds and a local offset), or None."""
    try:
        when = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def model_expiring(snapshot, ctx):
    """WARN when a loaded Ollama model unloads within model_expiring_s (default 60) of the snapshot's taken_at."""
    limit = float((ctx or {}).get("model_expiring_s", 60))
    now = _when((snapshot or {}).get("taken_at")) or datetime.now(timezone.utc)
    out = []
    for name, node in _nodes(snapshot):
        soon, blind = [], []
        for ep in node.get("ollama") or []:
            ep = ep or {}
            if ep.get("error") or ep.get("kind", "ollama") != "ollama":
                continue
            if ep.get("ps_error"):
                blind.append("%s %s: %s" % (name, ep.get("url"), ep["ps_error"]))
                continue
            for model in ep.get("running") or []:
                when = _when((model or {}).get("expires_at"))
                if when is not None and (when - now).total_seconds() <= limit:
                    soon.append((model.get("name"), int(round((when - now).total_seconds()))))
        if soon:
            out.append(F.make_finding("model.expiring", "partial", "WARN", name,
                                      "%d loaded model(s) unload within %ds: %s" % (len(soon), limit, ", ".join(m for m, _ in soon)),
                                      evidence=["%s %s: expires in %ds" % (name, model, left) for model, left in soon],
                                      try_=["send a request with a longer keep_alive", "set OLLAMA_KEEP_ALIVE on that node"]))
        elif blind:
            out.append(F.make_finding("model.expiring", "partial", "INFO", name,
                                      "cannot tell when models unload: /api/ps unavailable", evidence=blind,
                                      try_=["upgrade Ollama on that node (older releases have no /api/ps)"]))
    return out


def model_slow_ttft(snapshot, ctx):
    """INFO when a --ttft probe took longer than ttft_slow_ms (default 2000) to the first token."""
    limit = int((ctx or {}).get("ttft_slow_ms", 2000))
    out = []
    for name, node in _nodes(snapshot):
        slow = [((m or {}).get("name"), (m or {}).get("ttft_ms")) for ep in (node.get("ollama") or [])
                for m in ((ep or {}).get("running") or [])]
        slow = [(model, ms) for model, ms in slow if isinstance(ms, (int, float)) and ms > limit]
        if slow:
            out.append(F.make_finding("model.slow_ttft", "partial", "INFO", name,
                                      "%d model(s) slower than %d ms to first token: %s" % (len(slow), limit, ", ".join(m for m, _ in slow)),
                                      evidence=["%s %s: %d ms to first token" % (name, model, ms) for model, ms in slow],
                                      try_=["check the model fits in VRAM (no CPU offload)", "check nothing else is generating on that GPU"]))
    return out


def vram_unattributed(snapshot, ctx):
    limit = int((ctx or {}).get("vram_unattributed_mib", 2048))
    out = []
    for name, node in _nodes(snapshot):
        bad = [((gpu or {}).get("name"), int((gpu or {}).get("unattributed_mib") or 0)) for gpu in (node.get("gpus") or [])]
        bad = [(gpu, mib) for gpu, mib in bad if mib >= limit]
        if bad:
            out.append(F.make_finding("vram.unattributed", "partial", "WARN", name,
                                      "%d GPU(s) hold VRAM no listed process explains" % len(bad),
                                      evidence=["%s %s: %d MiB unattributed" % (name, gpu, mib) for gpu, mib in bad]))
    return out


def gpu_unavailable(snapshot, ctx):
    return [F.make_finding("gpu.unavailable", "partial", "INFO", name, "GPU status unavailable",
                           evidence=["%s: %s" % (name, node["gpu_error"])], try_=["check nvidia-smi runs on that node"])
            for name, node in _nodes(snapshot) if node.get("gpu_error")]


def _tailnet(node):
    return (node.get("tailnet") or {}) if "tailnet" in node else None


def tailnet_unavailable(snapshot, ctx):
    return [F.make_finding("tailnet.unavailable", "partial", "INFO", name, "tailnet status unavailable",
                           evidence=["%s: %s" % (name, _tailnet(node).get("error") or "tailscale gave no status")],
                           try_=["check tailscale is installed and tailscaled is running"])
            for name, node in _nodes(snapshot) if _tailnet(node) is not None and _tailnet(node).get("peer") is None]


def tailnet_peer_offline(snapshot, ctx):
    return [F.make_finding("tailnet.peer_offline", "missing", "FAIL", name, "tailnet peer offline: " + name)
            for name, node in _nodes(snapshot) if _tailnet(node) is not None and _tailnet(node).get("peer") is False]


def tailnet_relayed(snapshot, ctx):
    out = []
    for name, node in _nodes(snapshot):
        tailnet = node.get("tailnet") or {}
        if tailnet.get("peer") and tailnet.get("direct") is False:
            out.append(F.make_finding("tailnet.relayed", "partial", "INFO", name,
                                      "tailnet traffic is relayed, not direct: %s via %s" % (name, tailnet.get("derp"))))
    return out


CHECKS = [
    ("node.unreachable", node_unreachable),
    ("ollama.unreachable", ollama_unreachable),
    ("model.not_loaded", model_not_loaded),
    ("model.expiring", model_expiring),
    ("model.slow_ttft", model_slow_ttft),
    ("vram.unattributed", vram_unattributed),
    ("gpu.unavailable", gpu_unavailable),
    ("tailnet.unavailable", tailnet_unavailable),
    ("tailnet.peer_offline", tailnet_peer_offline),
    ("tailnet.relayed", tailnet_relayed),
]


def _selftest():
    def snap(**over):
        box = {"reachable": True, "latency_ms": 5, "ollama": [{"url": "http://a:11434", "error": None, "running": [{"name": "qwen3:30b"}]}],
               "tailnet": {"peer": True, "direct": True, "derp": None, "ips": []}}
        box2 = {"reachable": True, "latency_ms": 1, "ollama": [{"url": "http://n:11435", "error": None, "running": []}],
                "gpus": [{"name": "RTX 3060", "used_mib": 300, "unattributed_mib": 300}]}
        box.update(over.get("gpu-box", {}))
        box2.update(over.get("gpu-box-2", {}))
        return {"schema": 1, "taken_at": "T", "nodes": {"gpu-box": box, "gpu-box-2": box2}}

    ctx = {"expect": {"gpu-box": {"model": "qwen3:30b"}}, "vram_unattributed_mib": 2048}

    def t_clean_snapshot_no_findings():
        return all(fn(snap(), ctx) == [] for _, fn in CHECKS)

    def t_dead_endpoint_fail():
        [f] = ollama_unreachable(snap(**{"gpu-box-2": {"ollama": [{"url": "http://n:11435", "error": "OSError: refused", "running": []}]}}), ctx)
        return f["code"] == "ollama.unreachable" and f["level"] == "FAIL" and f["node"] == "gpu-box-2" and "refused" in f["evidence"][0]

    def t_model_not_loaded_warn():
        [f] = model_not_loaded(snap(**{"gpu-box": {"ollama": [{"url": "http://a:11434", "error": None, "running": [{"name": "qwen3-coder:30b"}]}]}}), ctx)
        return f["level"] == "WARN" and f["node"] == "gpu-box" and "qwen3-coder:30b" in f["evidence"][0]

    def t_unreachable_node_not_blamed_for_model():
        return model_not_loaded(snap(**{"gpu-box": {"reachable": False}}), ctx) == []

    def t_vram_threshold():
        [f] = vram_unattributed(snap(**{"gpu-box-2": {"gpus": [{"name": "RTX 4070", "used_mib": 9000, "unattributed_mib": 8000}]}}), ctx)
        return f["level"] == "WARN" and "8000 MiB" in f["evidence"][0]

    def t_tailnet_offline_and_relayed():
        [off] = tailnet_peer_offline(snap(**{"gpu-box": {"tailnet": {"peer": False, "direct": None, "derp": None, "ips": []}}}), ctx)
        [rel] = tailnet_relayed(snap(**{"gpu-box": {"tailnet": {"peer": True, "direct": False, "derp": "ord", "ips": []}}}), ctx)
        return off["level"] == "FAIL" and rel["level"] == "INFO" and "ord" in rel["summary"]

    def t_model_expiring():
        ep = {"url": "http://a:11434", "error": None, "running": [{"name": "qwen3:30b", "expires_at": "1970-01-01T00:00:45Z"}]}
        [f] = model_expiring({"taken_at": "1970-01-01T00:00:00+00:00", "nodes": {"gpu-box": {"ollama": [ep]}}}, {})
        [g] = model_expiring({"nodes": {"gpu-box": {"ollama": [dict(ep, ps_error="HTTP 404 on /api/ps")]}}}, {})
        return f["level"] == "WARN" and "45s" in f["evidence"][0] and g["level"] == "INFO"

    def t_empty_snapshot():
        return all(fn({}, {}) == [] for _, fn in CHECKS) and all(fn(None, None) == [] for _, fn in CHECKS)

    checks = [
        ("clean_snapshot_no_findings", t_clean_snapshot_no_findings),
        ("dead_endpoint_fail", t_dead_endpoint_fail),
        ("model_not_loaded_warn", t_model_not_loaded_warn),
        ("unreachable_node_not_blamed_for_model", t_unreachable_node_not_blamed_for_model),
        ("vram_threshold", t_vram_threshold),
        ("tailnet_offline_and_relayed", t_tailnet_offline_and_relayed),
        ("model_expiring", t_model_expiring),
        ("empty_snapshot", t_empty_snapshot),
    ]
    passed = 0
    for name, fn in checks:
        try:
            ok = bool(fn())
        except Exception as exc:  # noqa: BLE001 - a crashed check is a failed check
            ok = False
            name = "%s (raised %s: %s)" % (name, type(exc).__name__, exc)
        passed += ok
        print(("ok   " if ok else "FAIL ") + name)
    failed = len(checks) - passed
    print("sg_checks_core selftest: %d checks, %d passed, %d failed" % (len(checks), passed, failed))
    return 0 if failed == 0 and checks else 1


if __name__ == "__main__":
    sys.exit(_selftest() if "--selftest" in sys.argv else 2)
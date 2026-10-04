"""Core 0.1 rules for stack-guardian, a health doctor for a home AI stack.

Each rule inspects a whole snapshot and returns exactly ONE finding aggregated
over every node, or None when the stack is healthy along that axis.
"""

import sys

import sg_finding as F


def _nodes(snapshot):
    """Return the snapshot's nodes as a list of (name, node) pairs, node None -> {}."""
    nodes = (snapshot or {}).get("nodes") or {}
    return [(name, node or {}) for name, node in nodes.items()]


def ollama_unreachable(snapshot, ctx):
    bad = []
    for name, node in _nodes(snapshot):
        for ep in (node.get("ollama") or []):
            error = (ep or {}).get("error")
            if error:
                bad.append((name, ep.get("url"), error))
    if not bad:
        return None
    return F.make_finding(
        "ollama.unreachable",
        "missing",
        "FAIL",
        bad[0][0],
        "%d Ollama endpoint(s) do not answer: %s"
        % (len(bad), ", ".join("%s %s" % (name, url) for name, url, _ in bad)),
        evidence=["%s %s: %s" % (name, url, error) for name, url, error in bad],
        try_=["check the Ollama service on that node"],
    )


def model_not_loaded(snapshot, ctx):
    expect = (ctx or {}).get("expect") or {}
    bad = []
    for name, node in _nodes(snapshot):
        want = (expect.get(name) or {}).get("model")
        if not want or not node.get("reachable"):
            continue
        running = []
        for ep in (node.get("ollama") or []):
            for model in (ep or {}).get("running") or []:
                running.append((model or {}).get("name"))
        if want not in running:
            bad.append((name, want, running))
    if not bad:
        return None
    return F.make_finding(
        "model.not_loaded",
        "partial",
        "WARN",
        bad[0][0],
        "expected model not loaded: "
        + ", ".join("%s wants %s" % (name, want) for name, want, _ in bad),
        evidence=[
            "%s running: %s" % (name, ", ".join(running) or "nothing")
            for name, _, running in bad
        ],
    )


def vram_unattributed(snapshot, ctx):
    limit = int((ctx or {}).get("vram_unattributed_mib", 2048))
    bad = []
    for name, node in _nodes(snapshot):
        for gpu in (node.get("gpus") or []):
            unattributed = int((gpu or {}).get("unattributed_mib") or 0)
            if unattributed >= limit:
                bad.append((name, (gpu or {}).get("name"), unattributed))
    if not bad:
        return None
    return F.make_finding(
        "vram.unattributed",
        "partial",
        "WARN",
        bad[0][0],
        "%d GPU(s) hold VRAM no listed process explains" % len(bad),
        evidence=[
            "%s %s: %d MiB unattributed" % (name, gpu, mib) for name, gpu, mib in bad
        ],
    )


def tailnet_peer_offline(snapshot, ctx):
    bad = []
    for name, node in _nodes(snapshot):
        if "tailnet" not in node:
            continue
        tailnet = node.get("tailnet") or {}
        if not tailnet.get("peer"):
            bad.append(name)
    if not bad:
        return None
    return F.make_finding(
        "tailnet.peer_offline",
        "missing",
        "FAIL",
        bad[0],
        "tailnet peer offline: " + ", ".join(bad),
    )


def tailnet_relayed(snapshot, ctx):
    bad = []
    for name, node in _nodes(snapshot):
        tailnet = node.get("tailnet") or {}
        if tailnet.get("peer") and tailnet.get("direct") is False:
            bad.append((name, tailnet.get("derp")))
    if not bad:
        return None
    return F.make_finding(
        "tailnet.relayed",
        "partial",
        "INFO",
        bad[0][0],
        "tailnet traffic is relayed, not direct: "
        + ", ".join("%s via %s" % (name, derp) for name, derp in bad),
    )


CHECKS = [
    ("ollama.unreachable", ollama_unreachable),
    ("model.not_loaded", model_not_loaded),
    ("vram.unattributed", vram_unattributed),
    ("tailnet.peer_offline", tailnet_peer_offline),
    ("tailnet.relayed", tailnet_relayed),
]


def _selftest():
    def snap(**over):
        atlas = {"reachable": True, "latency_ms": 5, "ollama": [{"url": "http://a:11434", "error": None, "running": [{"name": "qwen3:30b"}]}],
                 "tailnet": {"peer": True, "direct": True, "derp": None, "ips": []}}
        node1 = {"reachable": True, "latency_ms": 1, "ollama": [{"url": "http://n:11435", "error": None, "running": []}],
                 "gpus": [{"name": "RTX 3060", "used_mib": 300, "unattributed_mib": 300}]}
        atlas.update(over.get("atlas", {}))
        node1.update(over.get("node1", {}))
        return {"schema": 1, "taken_at": "T", "nodes": {"atlas": atlas, "node1": node1}}

    ctx = {"expect": {"atlas": {"model": "qwen3:30b"}}, "vram_unattributed_mib": 2048}

    def t_clean_snapshot_no_findings():
        return all(fn(snap(), ctx) is None for _, fn in CHECKS)

    def t_dead_endpoint_fail():
        f = ollama_unreachable(snap(node1={"ollama": [{"url": "http://n:11435", "error": "OSError: refused", "running": []}]}), ctx)
        return f["code"] == "ollama.unreachable" and f["level"] == "FAIL" and f["node"] == "node1" and "refused" in f["evidence"][0]

    def t_model_not_loaded_warn():
        f = model_not_loaded(snap(atlas={"ollama": [{"url": "http://a:11434", "error": None, "running": [{"name": "qwen3-coder:30b"}]}]}), ctx)
        return f["level"] == "WARN" and f["node"] == "atlas" and "qwen3-coder:30b" in f["evidence"][0]

    def t_unreachable_node_not_blamed_for_model():
        return model_not_loaded(snap(atlas={"reachable": False}), ctx) is None

    def t_vram_threshold():
        f = vram_unattributed(snap(node1={"gpus": [{"name": "RTX 4070", "used_mib": 9000, "unattributed_mib": 8000}]}), ctx)
        return f["level"] == "WARN" and "8000 MiB" in f["evidence"][0]

    def t_tailnet_offline_and_relayed():
        off = tailnet_peer_offline(snap(atlas={"tailnet": {"peer": False, "direct": None, "derp": None, "ips": []}}), ctx)
        rel = tailnet_relayed(snap(atlas={"tailnet": {"peer": True, "direct": False, "derp": "ord", "ips": []}}), ctx)
        return off["level"] == "FAIL" and rel["level"] == "INFO" and "ord" in rel["summary"]

    def t_empty_snapshot():
        return all(fn({}, {}) is None for _, fn in CHECKS) and all(fn(None, None) is None for _, fn in CHECKS)

    checks = [
        ("clean_snapshot_no_findings", t_clean_snapshot_no_findings),
        ("dead_endpoint_fail", t_dead_endpoint_fail),
        ("model_not_loaded_warn", t_model_not_loaded_warn),
        ("unreachable_node_not_blamed_for_model", t_unreachable_node_not_blamed_for_model),
        ("vram_threshold", t_vram_threshold),
        ("tailnet_offline_and_relayed", t_tailnet_offline_and_relayed),
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
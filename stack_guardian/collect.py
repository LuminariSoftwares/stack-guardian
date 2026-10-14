"""sg_collect -- snapshot collectors for the stack-guardian health doctor.

Each collector probes one surface of the home AI stack: an Ollama endpoint over
HTTP, the local NVIDIA GPUs through nvidia-smi, and the tailnet through
`tailscale status --json`.  Every command runner and HTTP getter is injectable so
tests never touch the network or the GPU.  A dead endpoint is data, never a crash.
"""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from . import parsers as sg_parsers
except ImportError:  # run as a plain file next to sg_parsers.py
    import sg_parsers

GPU_QUERY = ["nvidia-smi", "--query-gpu=index,uuid,name,memory.total,memory.used", "--format=csv,noheader,nounits"]
APPS_QUERY = ["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,process_name,used_memory", "--format=csv,noheader,nounits"]
TAILSCALE_STATUS = ["tailscale", "status", "--json"]


def default_run(argv, timeout=15):
    """Run a command and return its stdout, or None if it failed or is missing."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def default_http_get(url, timeout=5):
    """GET a URL and return (status, body text)."""
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


def collect_ollama(url, http_get=None, clock=None):
    """Probe one Ollama endpoint: version, installed models, loaded models."""
    http_get = http_get or default_http_get
    clock = clock or time.monotonic
    base = url.rstrip("/")
    out = {"url": base, "kind": "ollama", "version": None, "models": [], "running": [], "latency_ms": None, "error": None}
    try:
        t0 = clock()
        status, body = http_get(base + "/api/version")
        out["latency_ms"] = int(round((clock() - t0) * 1000))
        if status != 200:
            raise ValueError("HTTP %s on /api/version" % status)
        out["version"] = json.loads(body).get("version")
        status, body = http_get(base + "/api/tags")
        if status == 200:
            out["models"] = [m["name"] for m in (json.loads(body).get("models") or []) if isinstance(m, dict) and m.get("name")]
        try:
            status, body = http_get(base + "/api/ps")
        except urllib.error.HTTPError as exc:  # urlopen raises on 4xx/5xx: an Ollama without /api/ps still answers
            status, body = exc.code, ""
        if status == 200:
            out["running"] = sg_parsers.parse_ollama_ps(body)
        else:
            out["ps_error"] = "HTTP %s on /api/ps" % status
    except Exception as exc:
        out["error"] = ("%s: %s" % (type(exc).__name__, exc))[:200]
    return out


# Model states that mean "listed but not in memory": llama.cpp router status.value, LM Studio state.
_NOT_LOADED = {"unloaded", "loading", "not-loaded", "not_loaded"}
_MAX_PER_MODEL = 16


def _openai_loaded(model):
    """False when the record says the model is not in memory; a plain OpenAI record (vLLM, llama.cpp) is loaded."""
    status = model.get("status")
    state = status.get("value") if isinstance(status, dict) else model.get("state")
    return not (isinstance(state, str) and state.lower() in _NOT_LOADED)


def collect_openai(url, http_get=None, clock=None):
    """Probe one OpenAI-compatible endpoint (LM Studio, vLLM, llama.cpp server) into the collect_ollama shape.

    GET /v1/models lists the models; GET /v1/models/<id>, when the server has it, may refine a model's state.
    A model is "running" unless its record says it is not loaded. size_vram and expires_at are unknown here.
    """
    http_get = http_get or default_http_get
    clock = clock or time.monotonic
    base = url.rstrip("/")
    out = {"url": base, "kind": "openai", "version": None, "models": [], "running": [], "latency_ms": None, "error": None}
    try:
        t0 = clock()
        status, body = http_get(base + "/v1/models")
        out["latency_ms"] = int(round((clock() - t0) * 1000))
        if status != 200:
            raise ValueError("HTTP %s on /v1/models" % status)
        data = json.loads(body).get("data") or []
        records = [m for m in data if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"]]
        for i, model in enumerate(records[:_MAX_PER_MODEL]):
            try:
                one_status, one_body = http_get(base + "/v1/models/" + urllib.parse.quote(model["id"], safe=""))
                one = json.loads(one_body) if one_status == 200 else None
            except Exception:  # noqa: BLE001 - the per-model endpoint is optional
                one = None
            if isinstance(one, dict):
                records[i] = dict(model, **one)
        out["models"] = [m["id"] for m in records]
        out["running"] = [{"name": m["id"], "size_vram": 0, "expires_at": ""} for m in records if _openai_loaded(m)]
    except Exception as exc:
        out["error"] = ("%s: %s" % (type(exc).__name__, exc))[:200]
    return out


def collect_gpus(run=None):
    """Describe the local GPUs and their attributed compute processes."""
    return collect_gpus_status(run)[0]


def collect_gpus_status(run=None):
    """(gpus, error): error is set when nvidia-smi is missing or fails, so "no GPUs" and "cannot tell" differ."""
    run = run or default_run
    gpu = run(GPU_QUERY)
    if gpu is None:
        return [], "nvidia-smi unavailable (not installed, or it failed)"
    if not gpu.strip():
        return [], None
    return sg_parsers.parse_nvsmi(gpu, run(APPS_QUERY) or ""), None


def collect_tailnet(peer_host, run=None):
    """Report whether peer_host is visible on the tailnet and how it is reached; peer None when tailscale cannot say."""
    run = run or default_run
    out = run(TAILSCALE_STATUS)
    if out is None:
        return dict(sg_parsers.UNAVAILABLE, error="tailscale status failed (tailscale missing or tailscaled down)")
    return sg_parsers.parse_tailscale(out, peer_host)


def snapshot(nodes, run=None, http_get=None, now=None, clock=None, timeout_s=10.0):
    """Build the one snapshot dict the checks read, in node input order. Every node is collected at
    the same time and a node that has not answered within timeout_s is reported down (multinode.pull)."""
    try:
        from . import multinode as sg_multinode
    except ImportError:  # run as a plain file next to the sg_* modules, like cli.py
        import multinode as sg_multinode

    def one(name, cfg):
        if cfg.get("kind") == "openai":
            ollama = [collect_openai(u, http_get=http_get, clock=clock) for u in (cfg.get("endpoints") or [])]
        else:
            ollama = [collect_ollama(u, http_get=http_get, clock=clock) for u in (cfg.get("ollama") or [])]
        ok = [o for o in ollama if o["error"] is None]
        node = {"reachable": bool(ok), "latency_ms": ok[0]["latency_ms"] if ok else None, "ollama": ollama}
        if cfg.get("local"):
            node["gpus"], gpu_error = collect_gpus_status(run=run)
            if gpu_error:
                node["gpu_error"] = gpu_error
        if cfg.get("tailnet_host"):
            node["tailnet"] = collect_tailnet(cfg["tailnet_host"], run=run)
        return node
    return sg_multinode.pull(nodes, one, timeout_s=timeout_s, now=now)


def _selftest():
    TAGS = '{"models": [{"name": "qwen3:30b"}, {"name": "nomic-embed-text"}]}'
    PS = '{"models": [{"name": "qwen3:30b", "size_vram": 19000000000, "expires_at": "2026-10-04T00:00:00Z"}]}'

    def http_ok(url, timeout=5):
        if url.endswith("/api/version"):
            return 200, '{"version": "0.34.2"}'
        if url.endswith("/api/tags"):
            return 200, TAGS
        return 200, PS

    def http_dead(url, timeout=5):
        raise OSError("connection refused")

    ticks = iter([10.0, 10.042, 20.0, 20.5, 30.0, 30.1])

    def clock():
        return next(ticks)

    def run_ok(argv, timeout=15):
        if argv[0] == "tailscale":
            return '{"Peer": {"k": {"HostName": "gpu-box", "Online": true, "CurAddr": "1.2.3.4:41641", "TailscaleIPs": ["100.64.0.10"]}}}'
        if "--query-gpu=index,uuid,name,memory.total,memory.used" in argv:
            return "0, GPU-a, RTX 3060, 12288, 9000\n1, GPU-b, RTX 4070 Ti SUPER, 16376, 300\n"
        return "GPU-a, 4242, C:\\ollama\\ollama.exe, 8800\n"

    def run_none(argv, timeout=15):
        return None

    def t_ollama_ok():
        o = collect_ollama("http://gpu-box:11434/", http_get=http_ok, clock=clock)
        return (o["url"] == "http://gpu-box:11434" and o["version"] == "0.34.2" and o["models"] == ["qwen3:30b", "nomic-embed-text"]
                and o["running"][0]["name"] == "qwen3:30b" and o["latency_ms"] == 42 and o["error"] is None)

    def t_ollama_dead():
        o = collect_ollama("http://x:11434", http_get=http_dead)
        return o["version"] is None and o["models"] == [] and o["error"].startswith("OSError: connection refused")

    def t_openai_ok():
        def http(url, timeout=5):
            if url.endswith("/v1/models"):
                return 200, '{"object": "list", "data": [{"id": "a", "status": {"value": "loaded"}}, {"id": "b", "status": {"value": "unloaded"}}]}'
            return 404, ""
        o = collect_openai("http://gpu-box:8080/", http_get=http, clock=clock)
        return o["kind"] == "openai" and o["models"] == ["a", "b"] and [m["name"] for m in o["running"]] == ["a"] and o["error"] is None

    def t_gpus():
        g = collect_gpus(run=run_ok)
        return len(g) == 2 and g[0]["attributed_mib"] == 8800 and g[0]["processes"][0]["name"] == "ollama.exe"

    def t_no_nvidia_smi():
        return collect_gpus(run=run_none) == []

    def t_tailnet():
        t = collect_tailnet("GPU-BOX", run=run_ok)
        return t["peer"] is True and t["direct"] is True and t["ips"] == ["100.64.0.10"]

    def t_tailnet_missing():
        return collect_tailnet("gpu-box", run=run_none)["peer"] is None

    def t_snapshot_shape():
        s = snapshot({"gpu-box": {"ollama": ["http://gpu-box:11434"], "tailnet_host": "gpu-box"},
                      "gpu-box-2": {"ollama": ["http://n1:11434"], "local": True}},
                     run=run_ok, http_get=lambda u, timeout=5: http_dead(u) if "n1" in u else http_ok(u), now="T")
        a, n = s["nodes"]["gpu-box"], s["nodes"]["gpu-box-2"]
        return (s["schema"] == 1 and s["taken_at"] == "T" and a["reachable"] is True and "gpus" not in a
                and a["tailnet"]["peer"] is True and n["reachable"] is False and n["latency_ms"] is None and len(n["gpus"]) == 2)

    def t_empty():
        return snapshot({}, now="T") == {"schema": 1, "taken_at": "T", "nodes": {}}

    checks = [
        ("ollama_ok", t_ollama_ok),
        ("ollama_dead", t_ollama_dead),
        ("openai_ok", t_openai_ok),
        ("gpus", t_gpus),
        ("no_nvidia_smi", t_no_nvidia_smi),
        ("tailnet", t_tailnet),
        ("tailnet_missing", t_tailnet_missing),
        ("snapshot_shape", t_snapshot_shape),
        ("empty", t_empty),
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
    print("sg_collect selftest: %d checks, %d passed, %d failed" % (len(checks), passed, failed))
    return 0 if failed == 0 and checks else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if "--nodes" in sys.argv:
        with open(sys.argv[sys.argv.index("--nodes") + 1], encoding="utf-8") as fh:
            print(json.dumps(snapshot(json.load(fh)), indent=1))
        sys.exit(0)
    print("usage: sg_collect.py --nodes NODES.json | --selftest")
    sys.exit(2)
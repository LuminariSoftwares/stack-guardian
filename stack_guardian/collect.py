"""sg_collect -- snapshot collectors for the stack-guardian health doctor.

Each collector probes one surface of the home AI stack: an Ollama endpoint over
HTTP, the local NVIDIA GPUs through nvidia-smi, and the tailnet through
`tailscale status --json`.  Every command runner and HTTP getter is injectable so
tests never touch the network or the GPU.  A dead endpoint is data, never a crash.
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

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


class _KeepCredentialsHome(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never carry an Authorization header to a different host than the one it was meant for."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            new.headers.pop("Authorization", None)
            new.unredirected_hdrs.pop("Authorization", None)
        return new


def default_http_get(url, timeout=5, headers=None):
    """GET a URL and return (status, body text). headers (a bearer token, say) go only where redirects stay on-host."""
    if not headers:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    opener = urllib.request.build_opener(_KeepCredentialsHome)
    with opener.open(urllib.request.Request(url, headers=headers), timeout=timeout) as resp:  # noqa: S310 - the URL comes from the nodes file
        return resp.status, resp.read().decode("utf-8", errors="replace")


def default_http_first_line(url, body, timeout=20):
    """POST body as JSON and return once the first response line (the first streamed chunk) has arrived."""
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - the URL comes from the nodes file
        resp.readline()


def _keep_alive(expires_at, wall):
    """The keep_alive that leaves a model's unload time where it was: -1 when it is pinned (Ollama reports a
    far-future expires_at), else the seconds it has left; None when expires_at does not parse."""
    try:
        when = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    left = (when - wall()).total_seconds()
    return -1 if left > 365 * 86400 else "%ds" % max(1, int(left))


def probe_ttft(base, running, post_first=None, clock=None, wall=None):
    """Send a 1-token streamed generation to each loaded model and set its ttft_ms (or ttft_error). Costs a
    generation, so it only runs when asked for (--ttft)."""
    post_first = post_first or default_http_first_line
    clock = clock or time.perf_counter  # ms measurements: Windows' monotonic clock ticks every ~16 ms
    wall = wall or (lambda: datetime.now(timezone.utc))
    for model in running:
        body = {"model": model.get("name"), "prompt": "hi", "stream": True, "options": {"num_predict": 1}}
        keep_alive = _keep_alive(model.get("expires_at"), wall)
        if keep_alive is not None:
            body["keep_alive"] = keep_alive
        try:
            t0 = clock()
            post_first(base + "/api/generate", body)
            model["ttft_ms"] = int(round((clock() - t0) * 1000))
        except Exception as exc:  # noqa: BLE001 - a failed probe is data, never a crash
            model["ttft_error"] = ("%s: %s" % (type(exc).__name__, exc))[:200]


def collect_ollama(url, http_get=None, clock=None, ttft=False, post_first=None):
    """Probe one Ollama endpoint: version, installed models, loaded models (and, with ttft, time to first token)."""
    http_get = http_get or default_http_get
    clock = clock or time.perf_counter  # ms measurements: Windows' monotonic clock ticks every ~16 ms
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
        if ttft:
            probe_ttft(base, out["running"], post_first=post_first, clock=clock)
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
    clock = clock or time.perf_counter  # ms measurements: Windows' monotonic clock ticks every ~16 ms
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


_MAX_MODELS = 200


def collect_openai_models(url, http_get=None, clock=None, api_key_env=None, environ=None):
    """Probe one OpenAI-compatible endpoint by its base URL (".../v1"): GET <base>/models.

    Records whether it answered, how long that took and the model ids it lists; which of them are loaded is not
    something the OpenAI API says, so there is no "running" list here (loaded-model rules are Ollama-only).
    api_key_env names the environment variable holding a bearer token. Its value is sent as an Authorization header
    and never stored: not in this dict, not in an error text.
    """
    http_get = http_get or default_http_get
    clock = clock or time.perf_counter  # ms measurements: Windows' monotonic clock ticks every ~16 ms
    base = url.rstrip("/")
    out = {"url": base, "kind": "openai", "models": [], "latency_ms": None, "error": None}
    try:
        headers = {}
        if api_key_env:
            key = (environ if environ is not None else os.environ).get(api_key_env, "")
            if not key.strip():
                raise ValueError("api_key_env %s is not set in this environment" % api_key_env)
            headers = {"Authorization": "Bearer " + key.strip()}
        t0 = clock()
        try:
            status, body = http_get(base + "/models", **({"headers": headers} if headers else {}))
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and not base.endswith("/v1"):
                raise ValueError("HTTP Error 404 on %s/models; an OpenAI-compatible base URL usually ends in /v1" % base) from None
            raise
        out["latency_ms"] = int(round((clock() - t0) * 1000))
        if status != 200:
            raise ValueError("HTTP %s on /models" % status)
        listed = json.loads(body)
        if not isinstance(listed, dict) or not isinstance(listed.get("data"), list):
            raise ValueError('/models did not answer with an OpenAI model list ({"data": [...]})')
        ids = [m["id"][:200] for m in listed["data"] if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"]]
        out["models"] = ids[:_MAX_MODELS]
        if len(ids) > _MAX_MODELS:
            out["models_total"] = len(ids)
    except Exception as exc:
        out["error"] = ("%s: %s" % (type(exc).__name__, exc))[:200]
    return out


def _why_missing(program, absent, failed, which=None):
    """Why a helper program gave nothing: it is not installed, or it ran and failed. The two need different fixes."""
    which = which or shutil.which
    return absent if which(program) is None else failed


def collect_gpus(run=None):
    """Describe the local GPUs and their attributed compute processes."""
    return collect_gpus_status(run)[0]


def collect_gpus_status(run=None):
    """(gpus, error): error is set when nvidia-smi is missing or fails, so "no GPUs" and "cannot tell" differ."""
    return _collect_gpus(run)[:2]


def _collect_gpus(run=None, which=None):
    """(gpus, error, unreadable): unreadable holds one note per nvidia-smi line the parser could not read."""
    run = run or default_run
    gpu = run(GPU_QUERY)
    if gpu is None:
        return [], _why_missing("nvidia-smi",
                                "nvidia-smi is not on PATH (NVIDIA GPUs only: this is how stack-guardian reads them)",
                                "nvidia-smi is on PATH but failed (driver trouble, or no NVIDIA GPU here)",
                                which=which), []
    if not gpu.strip():
        return [], None, []
    gpus, unreadable = sg_parsers.parse_nvsmi_lines(gpu, run(APPS_QUERY) or "")
    return gpus, None, unreadable


def collect_tailnet(peer_host, run=None, which=None):
    """Report whether peer_host is visible on the tailnet and how it is reached; peer None when tailscale cannot say."""
    run = run or default_run
    out = run(TAILSCALE_STATUS)
    if out is None:
        return dict(sg_parsers.UNAVAILABLE,
                    error=_why_missing("tailscale",
                                       "tailscale is not on PATH (this machine is not on a tailnet)",
                                       "tailscale status failed (tailscaled is not running, or this node is logged out)",
                                       which=which))
    return sg_parsers.parse_tailscale(out, peer_host)


def snapshot(nodes, run=None, http_get=None, now=None, clock=None, timeout_s=10.0, ttft=False, post_first=None):
    """Build the one snapshot dict the checks read, in node input order. Every node is collected at
    the same time and a node that has not answered within timeout_s is reported down (multinode.pull).
    ttft=True also probes each Ollama model's time to first token (a real 1-token generation)."""
    try:
        from . import multinode as sg_multinode
    except ImportError:  # run as a plain file next to the sg_* modules, like cli.py
        import multinode as sg_multinode

    def one(name, cfg):
        if cfg.get("kind") == "openai":
            ollama = [collect_openai(u, http_get=http_get, clock=clock) for u in (cfg.get("endpoints") or [])]
        else:
            ollama = [collect_ollama(u, http_get=http_get, clock=clock, ttft=ttft, post_first=post_first)
                      for u in (cfg.get("ollama") or [])]
        openai = [collect_openai_models(u, http_get=http_get, clock=clock, api_key_env=cfg.get("api_key_env"))
                  for u in (cfg.get("openai") or [])]
        ok = [o for o in ollama + openai if o["error"] is None]
        node = {"reachable": bool(ok), "latency_ms": ok[0]["latency_ms"] if ok else None, "ollama": ollama}
        if "openai" in cfg:
            node["openai"] = openai
        if cfg.get("local"):
            node["gpus"], gpu_error, unreadable = _collect_gpus(run=run)
            if gpu_error:
                node["gpu_error"] = gpu_error
            if unreadable:
                node["gpu_unreadable"] = unreadable
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
        gpus, error = collect_gpus_status(run=run_none)
        absent = _why_missing("nvidia-smi", "absent", "failed", which=lambda _p: None)
        present = _why_missing("nvidia-smi", "absent", "failed", which=lambda _p: "/usr/bin/nvidia-smi")
        return gpus == [] and "nvidia-smi" in error and (absent, present) == ("absent", "failed")

    def t_unreadable_gpu_line_is_kept():
        def run_na(argv, timeout=15):
            if "--query-gpu=index,uuid,name,memory.total,memory.used" in argv:
                return "0, GPU-a, RTX 3060, 12288, 9000\n1, GPU-b, RTX 4070, [N/A], [N/A]\n"
            return ""
        snap = snapshot({"gpu-box": {"local": True}}, run=run_na, now="T")
        node = snap["nodes"]["gpu-box"]
        return len(node["gpus"]) == 1 and len(node.get("gpu_unreadable") or []) == 1

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
        ("unreadable_gpu_line_is_kept", t_unreadable_gpu_line_is_kept),
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
"""Item 5: --ttft sends a 1-token generation to each Ollama endpoint's loaded model and records ttft_ms; the shipped
INFO rule model.slow_ttft fires above ctx ttft_slow_ms (default 2000). Never runs by default."""
import json
import subprocess
import sys

from stack_guardian import cli, collect, core

PS = {"models": [{"name": "qwen3:8b", "size_vram": 6000000000, "expires_at": "2318-01-01T00:00:00Z"}]}
CHUNK = json.dumps({"model": "qwen3:8b", "response": "Hi", "done": False}) + "\n"


def _ollama(stub, delay):
    return stub({("GET", "/api/version"): (200, {"version": "0.12.0"}, 0),
                 ("GET", "/api/tags"): (200, {"models": [{"name": "qwen3:8b"}]}, 0),
                 ("GET", "/api/ps"): (200, PS, 0),
                 ("POST", "/api/generate"): (200, CHUNK, delay)})


def _slow(snapshot, ctx=None):
    found = core.run_all(core.load_checks(cli.DEFAULT_CHECKS_DIR), snapshot, ctx or {})
    return [f for f in found if f["code"] == "model.slow_ttft"]


def test_ttft_records_ms_and_slow_rule(stub):
    s = _ollama(stub, delay=0.4)
    snap = collect.snapshot({"gpu-box": {"ollama": [s.url]}}, now="T", ttft=True)
    [model] = snap["nodes"]["gpu-box"]["ollama"][0]["running"]
    assert 400 <= model["ttft_ms"] < 5000, model
    [(_, _, body)] = [r for r in s.requests if r[0] == "POST"]
    assert body["model"] == "qwen3:8b" and body["stream"] is True and body["options"]["num_predict"] == 1
    assert body["keep_alive"] == -1  # a model pinned with keep_alive -1 stays pinned after the probe
    assert _slow(snap) == []  # 2000 ms default
    [f] = _slow(snap, {"ttft_slow_ms": 200})
    assert (f["level"], f["node"]) == ("INFO", "gpu-box") and "qwen3:8b" in f["summary"]


def test_ttft_never_runs_by_default(stub):
    s = _ollama(stub, delay=0)
    snap = collect.snapshot({"gpu-box": {"ollama": [s.url]}}, now="T")
    assert s.paths("POST") == [] and "ttft_ms" not in snap["nodes"]["gpu-box"]["ollama"][0]["running"][0]


def test_ttft_error_is_recorded_not_raised(stub):
    s = stub({("GET", "/api/version"): (200, {"version": "0.12.0"}, 0),
              ("GET", "/api/tags"): (200, {"models": [{"name": "qwen3:8b"}]}, 0),
              ("GET", "/api/ps"): (200, PS, 0),
              ("POST", "/api/generate"): (500, {"error": "out of memory"}, 0)})
    ep = collect.snapshot({"gpu-box": {"ollama": [s.url]}}, now="T", ttft=True)["nodes"]["gpu-box"]["ollama"][0]
    assert ep["error"] is None and ep["running"][0]["ttft_error"].startswith("HTTPError")


def test_ttft_flag_needs_live_nodes(tmp_path):
    snap = tmp_path / "s.json"
    snap.write_text(json.dumps({"schema": 1, "nodes": {}}), encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "stack_guardian", "doctor", "--snapshot", str(snap), "--ttft"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 3 and "--ttft" in r.stderr

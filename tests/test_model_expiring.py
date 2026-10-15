"""Item 2: model.expiring WARN when a loaded model's /api/ps expires_at is within N seconds (default 60) of the
snapshot's taken_at (the frozen clock); INFO when /api/ps is unavailable."""
from stack_guardian import cli, collect, core

NOW = "2026-10-14T12:00:00+00:00"
PS = {"models": [
    # Ollama writes expires_at with nanoseconds and a local offset (docs: "2024-06-04T14:38:31.83753-07:00")
    {"name": "qwen3:8b", "size_vram": 6000000000, "expires_at": "2026-10-14T07:00:30.123456789-05:00"},
    {"name": "qwen3:30b", "size_vram": 19000000000, "expires_at": "2026-10-14T12:10:00Z"},
    {"name": "nomic-embed-text", "size_vram": 1, "expires_at": "2318-01-01T00:00:00Z"}]}


def _ollama(stub, ps_status=200):
    return stub({("GET", "/api/version"): (200, {"version": "0.12.0"}, 0),
                 ("GET", "/api/tags"): (200, {"models": [{"name": m["name"]} for m in PS["models"]]}, 0),
                 ("GET", "/api/ps"): (ps_status, PS if ps_status == 200 else {"error": "not found"}, 0)})


def _expiring(snapshot, ctx=None):
    found = core.run_all(core.load_checks(cli.DEFAULT_CHECKS_DIR), snapshot, ctx or {})
    return [f for f in found if f["code"] == "model.expiring"]


def test_model_expiring_warn_within_default_60s(stub):
    s = _ollama(stub)
    snap = collect.snapshot({"gpu-box": {"ollama": [s.url]}}, now=NOW)
    [f] = _expiring(snap)
    assert (f["level"], f["node"]) == ("WARN", "gpu-box")
    assert "qwen3:8b" in f["summary"] and "qwen3:30b" not in f["summary"]
    assert any("30s" in e for e in f["evidence"]), f["evidence"]


def test_model_expiring_threshold_from_ctx(stub):
    s = _ollama(stub)
    snap = collect.snapshot({"gpu-box": {"ollama": [s.url]}}, now=NOW)
    [f] = _expiring(snap, {"model_expiring_s": 900})
    assert "qwen3:8b" in f["summary"] and "qwen3:30b" in f["summary"] and "nomic" not in f["summary"]
    assert _expiring(snap, {"model_expiring_s": 10}) == []


def test_model_expiring_info_when_ps_unavailable(stub):
    s = _ollama(stub, ps_status=404)
    snap = collect.snapshot({"gpu-box": {"ollama": [s.url]}}, now=NOW)
    ep = snap["nodes"]["gpu-box"]["ollama"][0]
    assert ep["error"] is None and ep["ps_error"] == "HTTP 404 on /api/ps"
    [f] = _expiring(snap)
    assert f["level"] == "INFO" and "/api/ps" in f["summary"]

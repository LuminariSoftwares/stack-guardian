"""Item 1: a node with kind "openai" (LM Studio, vLLM, llama.cpp server) is collected from /v1/models into the
same shape the Ollama collector produces, so every shipped rule works unchanged.

Response shapes assumed below:
- vLLM: VERIFIED against vllm/entrypoints/openai/models/serving.py (ModelCard id/root/max_model_len/permission,
  object "model", owned_by "vllm"). vLLM serves only the model(s) it has loaded.
- llama.cpp server: VERIFIED against tools/server/README.md. A plain server lists exactly one loaded model with a
  "meta" object; router mode lists every known model with "status": {"value": "loaded" | "loading" | "unloaded"}.
- LM Studio: NOT VERIFIED (its docs site was not reachable from the build machine). Assumed the plain OpenAI list
  shape with owned_by "organization_owner", and a per-model GET /v1/models/<id> carrying "state":
  "loaded" | "not-loaded" like its /api/v0 endpoints.
- /v1/models/<id> follows the OpenAI spec (one model object); a server without it answers 404 and is skipped.
"""
from stack_guardian import cli, collect, core

LMSTUDIO = {"object": "list", "data": [
    {"id": "qwen2.5-7b-instruct", "object": "model", "owned_by": "organization_owner"},
    {"id": "nomic-embed-text-v1.5", "object": "model", "owned_by": "organization_owner"}]}
LMSTUDIO_ONE = {
    "qwen2.5-7b-instruct": {"id": "qwen2.5-7b-instruct", "object": "model", "state": "loaded"},
    "nomic-embed-text-v1.5": {"id": "nomic-embed-text-v1.5", "object": "model", "state": "not-loaded"}}
VLLM = {"object": "list", "data": [
    {"id": "Qwen/Qwen3-8B", "object": "model", "created": 1760400000, "owned_by": "vllm", "root": "Qwen/Qwen3-8B",
     "parent": None, "max_model_len": 32768,
     "permission": [{"id": "modelperm-1", "object": "model_permission", "allow_sampling": True}]}]}
LLAMACPP = {"object": "list", "data": [
    {"id": "../models/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf", "object": "model", "created": 1735142223,
     "owned_by": "llamacpp", "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
     "meta": {"n_vocab": 128256, "n_ctx_train": 131072, "size": 4912898304}}]}
LLAMACPP_ROUTER = {"object": "list", "data": [
    {"id": "small", "object": "model", "status": {"value": "loaded", "args": ["llama-server", "-ctx", "4096"]}},
    {"id": "big", "object": "model", "status": {"value": "unloaded"}}]}


def _ep(snapshot, node="gpu-box"):
    [ep] = snapshot["nodes"][node]["ollama"]
    return ep


def _snap(url, **kw):
    return collect.snapshot({"gpu-box": {"kind": "openai", "endpoints": [url]}}, now="T", **kw)


def _shipped(snapshot, ctx=None):
    return [(f["code"], f["node"], f["level"]) for f in
            core.run_all(core.load_checks(cli.DEFAULT_CHECKS_DIR), snapshot, ctx or {})]


def test_openai_lmstudio_shape_uses_per_model_state(stub):
    routes = {("GET", "/v1/models"): (200, LMSTUDIO, 0)}
    routes.update({("GET", "/v1/models/" + k): (200, v, 0) for k, v in LMSTUDIO_ONE.items()})
    s = stub(routes)
    snap = _snap(s.url)
    ep = _ep(snap)
    assert snap["nodes"]["gpu-box"]["reachable"] is True
    assert ep["kind"] == "openai" and ep["error"] is None and isinstance(ep["latency_ms"], int)
    assert ep["models"] == ["qwen2.5-7b-instruct", "nomic-embed-text-v1.5"]
    assert ep["running"] == [{"name": "qwen2.5-7b-instruct", "size_vram": 0, "expires_at": ""}]


def test_openai_vllm_shape_without_per_model_endpoint(stub):
    s = stub({("GET", "/v1/models"): (200, VLLM, 0)})  # /v1/models/<id> answers 404: skipped, not an error
    ep = _ep(_snap(s.url))
    assert ep["error"] is None and ep["models"] == ["Qwen/Qwen3-8B"]
    assert [m["name"] for m in ep["running"]] == ["Qwen/Qwen3-8B"]
    assert "/v1/models/Qwen%2FQwen3-8B" in s.paths("GET")  # the id is URL-quoted, slashes included


def test_openai_llamacpp_plain_and_router_shapes(stub):
    plain = stub({("GET", "/v1/models"): (200, LLAMACPP, 0)})
    router = stub({("GET", "/v1/models"): (200, LLAMACPP_ROUTER, 0)})
    assert [m["name"] for m in _ep(_snap(plain.url))["running"]] == ["../models/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"]
    ep = _ep(_snap(router.url))
    assert ep["models"] == ["small", "big"] and [m["name"] for m in ep["running"]] == ["small"]


def test_openai_node_runs_shipped_rules_unchanged(stub):
    s = stub({("GET", "/v1/models"): (200, VLLM, 0)})
    snap = _snap(s.url)
    assert _shipped(snap, {"expect": {"gpu-box": {"model": "Qwen/Qwen3-8B"}}}) == []
    assert _shipped(snap, {"expect": {"gpu-box": {"model": "other"}}}) == [("model.not_loaded", "gpu-box", "WARN")]


def test_openai_errors_behave_like_ollama(stub):
    bad = stub({("GET", "/v1/models"): (500, {"error": "boom"}, 0), ("GET", "/api/version"): (500, {"error": "boom"}, 0)})
    ep = _ep(_snap(bad.url))
    assert ep["error"] == "HTTPError: HTTP Error 500: Internal Server Error" and ep["models"] == [] and ep["running"] == []
    assert ep["error"] == collect.collect_ollama(bad.url)["error"]  # same error text as the Ollama collector
    dead = _snap("http://127.0.0.1:9")
    assert _ep(dead)["error"].startswith(("URLError", "ConnectionRefusedError", "OSError"))
    assert dead["nodes"]["gpu-box"]["reachable"] is False
    assert _shipped(dead) == [("ollama.unreachable", "gpu-box", "FAIL")]
    junk = stub({("GET", "/v1/models"): (200, "not json", 0)})
    assert _ep(_snap(junk.url))["error"].startswith("JSONDecodeError")


def test_openai_slow_node_is_node_unreachable(stub):
    slow = stub({("GET", "/v1/models"): (200, VLLM, 2.0)})
    snap = _snap(slow.url, timeout_s=0.3)
    assert snap["nodes"]["gpu-box"]["error"] == "timeout after 0.3s"
    assert _shipped(snap) == [("node.unreachable", "gpu-box", "FAIL")]
